"""Uruchamianie zadań z panelu WWW wraz z automatycznym zapisem EEG.

Przebieg jednej sesji:
1. Panel tworzy katalog ``recordings/ZADANIE_PSEUDONIM_DATA/`` i plik _meta.json.
2. Włącza zapis EEG (callback sterownika) – każda próbka dostaje czas
   ``local_clock`` (``pylsl.local_clock``).
3. Uruchamia proces zadania (pygame, pełny ekran). Zadanie zapisuje triggery
   w _events.csv tym samym zegarem ``local_clock``.
4. Po zakończeniu zadania zapis trwa jeszcze chwilę (ogon), potem jest
   zatrzymywany, a triggery są wpisywane do kolumny ``trigger`` pliku EEG.

Zadania są wykrywane dynamicznie: każdy katalog ``tasks/<nazwa>/`` z plikami
``task.json`` i ``task.py`` (oraz opcjonalnie ``analysis.py``).
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import signal
import subprocess
import sys
import time
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from tasks.common import (
    EXIT_ABORTED,
    PROJECT_ROOT,
    RECORDINGS_DIR,
    TASKS_DIR,
    SessionPaths,
    find_sessions,
    local_clock,
    session_name,
    validate_pseudonym,
)

from .brainaccess_halo import SAMPLE_RATE, EEGSample

logger = logging.getLogger("TaskRunner")

# Ile sekund EEG zapisujemy jeszcze po zamknięciu okna zadania (ostatnia epoka ERP).
TAIL_SECONDS = 1.5


def child_process_options() -> Dict[str, Any]:
    """Wspólne opcje procesów zadań i analiz na macOS, Linuksie i Windows."""
    # UTF-8 dla print() w procesie potomnym (Windows domyślnie używa cp1250/cp1252
    # i polskie znaki w logu kończyłyby się UnicodeEncodeError).
    options: Dict[str, Any] = {"env": {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}
    if sys.platform == "win32":
        # Osobna grupa procesów: tylko wtedy można wysłać CTRL_BREAK_EVENT do zadania.
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return options
ANALYSIS_TIMEOUT_SECONDS = 600


# --------------------------------------------------------------------------
# Wykrywanie zadań
# --------------------------------------------------------------------------

def discover_tasks() -> Dict[str, Dict[str, Any]]:
    """Czyta tasks/*/task.json przy każdym wywołaniu – nowe zadanie pojawia się bez restartu."""
    tasks: Dict[str, Dict[str, Any]] = {}
    for directory in sorted(TASKS_DIR.iterdir()):
        manifest = directory / "task.json"
        if not directory.is_dir() or not manifest.exists() or not (directory / "task.py").exists():
            continue
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("Błędny %s: %s", manifest, exc)
            continue
        info["key"] = directory.name
        info["code"] = str(info.get("code") or directory.name).upper()
        info.setdefault("name", directory.name)
        info.setdefault("params", [])
        info["has_analysis"] = (directory / "analysis.py").exists()
        tasks[directory.name] = info
    return tasks


def build_param_args(task: Dict[str, Any], values: Dict[str, Any]) -> tuple[list[str], Dict[str, Any]]:
    """Sprawdza parametry z formularza wg task.json i zamienia je na argumenty CLI."""
    args: list[str] = []
    used: Dict[str, Any] = {}
    for spec in task["params"]:
        name = spec["name"]
        raw = values.get(name, spec.get("default"))
        if raw is None or raw == "":
            continue
        caster = {"int": int, "float": float}.get(spec.get("type"), str)
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            raise ValueError(f"Parametr '{spec.get('label', name)}' ma nieprawidłową wartość: {raw}")
        if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
            raise ValueError(f"Parametr '{spec.get('label', name)}' musi być w zakresie {spec.get('min')}–{spec.get('max')}.")
        args += [f"--{name}", str(value)]
        used[name] = value
    return args, used


# --------------------------------------------------------------------------
# Zapis EEG sesji
# --------------------------------------------------------------------------

class SessionEEGWriter:
    """Zapisuje surowe (niefiltrowane) próbki EEG z czasem ``local_clock``.

    Sterownik oddaje próbki paczkami z pakietu BLE. Ostatnia próbka paczki
    dostaje czas odbioru, wcześniejsze są cofane co 1/fs – tak samo jak w
    strumieniu LSL. Dzięki temu czasy EEG i triggerów są w tej samej skali.
    """

    def __init__(self, path: Path, channels: List[str]):
        self.path = path
        self.channels = list(channels)
        self.timestamps: List[float] = []
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["lsl_time", *self.channels])
        self._step = 1.0 / SAMPLE_RATE
        self._last_flush = time.monotonic()

    def on_samples(self, samples: List[EEGSample]) -> None:
        if self._file.closed or not samples:
            return
        end = local_clock()
        start = end - self._step * (len(samples) - 1)
        if self.timestamps and start <= self.timestamps[-1]:
            # Paczka przyszła szybciej niż wynika z fs (jitter BLE) – zachowujemy monotoniczność.
            start = self.timestamps[-1] + self._step
        for index, sample in enumerate(samples):
            timestamp = start + self._step * index
            self.timestamps.append(timestamp)
            self._writer.writerow([f"{timestamp:.6f}", *(f"{sample.channels.get(ch, 0.0):.3f}" for ch in self.channels)])
        if time.monotonic() - self._last_flush > 1.0:
            self._file.flush()
            self._last_flush = time.monotonic()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


def merge_triggers(paths: SessionPaths, timestamps: List[float]) -> Dict[str, Any]:
    """Tworzy finalny plik EEG z kolumną ``trigger`` i uzupełnia _events.csv o numer próbki.

    Trigger trafia do pierwszej próbki o czasie ≥ czas triggera.
    """
    events: List[Dict[str, Any]] = []
    if paths.events.exists():
        with paths.events.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                events.append({"lsl_time": float(row["lsl_time"]), "marker": row["marker"]})

    per_sample: Dict[int, List[str]] = {}
    outside = []
    for event in events:
        index = bisect_left(timestamps, event["lsl_time"])
        if not timestamps or index >= len(timestamps) or event["lsl_time"] < timestamps[0] - 1.0 / SAMPLE_RATE:
            event["sample_index"] = ""
            event["offset_ms"] = ""
            outside.append(event["marker"])
            continue
        event["sample_index"] = index
        event["offset_ms"] = round((timestamps[index] - event["lsl_time"]) * 1000, 3)
        per_sample.setdefault(index, []).append(event["marker"])

    # Plik roboczy -> finalny CSV: sample_index, lsl_time, kanały..., trigger
    if paths.eeg_partial.exists():
        with paths.eeg_partial.open(newline="", encoding="utf-8") as source, \
                paths.eeg.open("w", newline="", encoding="utf-8") as target:
            reader = csv.reader(source)
            writer = csv.writer(target)
            header = next(reader)
            writer.writerow(["sample_index", *header, "trigger"])
            for index, row in enumerate(reader):
                writer.writerow([index, *row, "|".join(per_sample.get(index, []))])
        paths.eeg_partial.unlink()

    with paths.events.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["lsl_time", "sample_index", "offset_ms", "marker"])
        writer.writeheader()
        for event in events:
            writer.writerow({**event, "lsl_time": f"{event['lsl_time']:.6f}"})

    counts: Dict[str, int] = {}
    for event in events:
        counts[event["marker"]] = counts.get(event["marker"], 0) + 1
    return {
        "events_total": len(events),
        "events_in_eeg": len(events) - len(outside),
        "events_outside_eeg": outside,
        "markers": counts,
    }


# --------------------------------------------------------------------------
# Sesja zadania
# --------------------------------------------------------------------------

class TaskSession:
    def __init__(self, task: Dict[str, Any], pseudonym: str, params: Dict[str, Any],
                 driver, reference: str, bias: str):
        self.task = task
        self.pseudonym = validate_pseudonym(pseudonym)
        self.param_args, self.params = build_param_args(task, params)
        self.driver = driver
        self.reference = reference
        self.bias = bias
        self.name = session_name(task["code"], self.pseudonym)
        self.paths = SessionPaths(RECORDINGS_DIR / self.name)
        self.status = "starting"
        self.message = ""
        self.started = time.time()
        self.ended: Optional[float] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.writer: Optional[SessionEEGWriter] = None
        self.result: Dict[str, Any] = {}
        self._abort_requested = False

    @property
    def running(self) -> bool:
        return self.status in ("starting", "running", "finishing")

    def _write_meta(self, **extra: Any) -> None:
        meta = self.paths.read_meta()
        meta.update(extra)
        self.paths.meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    async def start(self) -> None:
        """Tworzy katalog, włącza zapis EEG i uruchamia proces zadania."""
        self.paths.dir.mkdir(parents=True, exist_ok=False)
        channels = list(self.driver.channels)
        summary = self.driver.get_summary()
        self._write_meta(
            session=self.name,
            task_code=self.task["code"],
            task_key=self.task["key"],
            task_name=self.task["name"],
            pseudonym=self.pseudonym,
            created=datetime.now().isoformat(timespec="seconds"),
            params=self.params,
            eeg_recorded=True,
            sample_rate_hz=SAMPLE_RATE,
            channels=channels,
            unit="uV",
            reference=self.reference,
            bias=self.bias,
            device=summary,
            dropped_packets_at_start=summary.get("dropped_packets"),
            clock="local_clock (pylsl.local_clock lub time.monotonic; wspólny dla EEG i triggerów)",
            timestamping="czas odbioru pakietu BLE, próbki w paczce cofane co 1/fs",
            files={
                "eeg": self.paths.eeg.name,
                "events": self.paths.events.name,
                "behavior": self.paths.behavior.name,
                "meta": self.paths.meta.name,
                "log": self.paths.log.name,
            },
            status="recording",
        )

        # 1. Zapis EEG startuje PRZED zadaniem, żeby mieć sygnał przed pierwszym bodźcem.
        self.writer = SessionEEGWriter(self.paths.eeg_partial, channels)
        self.driver.add_callback(self.writer.on_samples)

        # 2. Proces zadania – ten sam interpreter Pythona, katalog główny projektu.
        command = [sys.executable, "-u", "-m", f"tasks.{self.task['key']}.task",
                   "--session-dir", str(self.paths.dir), "--id", self.pseudonym, *self.param_args]
        self._write_meta(command=command)
        log = self.paths.log.open("w", encoding="utf-8")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command, cwd=str(PROJECT_ROOT), stdout=log, stderr=asyncio.subprocess.STDOUT,
                **child_process_options(),
            )
        except Exception:
            log.close()
            await self._finish(exit_code=None, error="Nie udało się uruchomić procesu zadania.")
            raise
        log.close()
        self.status = "running"
        asyncio.create_task(self._wait())

    async def _wait(self) -> None:
        exit_code = await self.process.wait()
        await self._finish(exit_code)

    async def _finish(self, exit_code: Optional[int], error: str = "") -> None:
        # 3. Krótki ogon nagrania po zakończeniu zadania, potem zatrzymanie zapisu.
        self.status = "finishing"
        await asyncio.sleep(TAIL_SECONDS)
        if self.writer:
            self.driver.remove_callback(self.writer.on_samples)
            self.writer.close()
        timestamps = self.writer.timestamps if self.writer else []

        # 4. Wpisanie triggerów do pliku EEG i kontrola synchronizacji.
        sync = merge_triggers(self.paths, timestamps)
        self.ended = time.time()
        if error:
            status = "error"
        elif self._abort_requested or exit_code == EXIT_ABORTED:
            status = "aborted"
        elif exit_code == 0:
            status = "completed"
        else:
            status = "error"
            error = f"Proces zadania zakończył się kodem {exit_code}. Zobacz {self.paths.log.name}."
        warnings = []
        if not timestamps:
            warnings.append("Brak próbek EEG – czy czepek przesyłał dane?")
        if sync["events_outside_eeg"]:
            warnings.append(f"{len(sync['events_outside_eeg'])} triggerów poza zakresem nagrania EEG.")
        dropped_end = self.driver.decoder.dropped_packets if self.driver else None
        start_dropped = self.paths.read_meta().get("dropped_packets_at_start") or 0
        if dropped_end is not None and dropped_end > start_dropped:
            warnings.append(f"Utracone pakiety BLE w trakcie sesji: {dropped_end - start_dropped}.")
        self.result = {
            "status": status,
            "exit_code": exit_code,
            "ended": datetime.now().isoformat(timespec="seconds"),
            "eeg_samples": len(timestamps),
            "eeg_first_lsl_time": timestamps[0] if timestamps else None,
            "eeg_last_lsl_time": timestamps[-1] if timestamps else None,
            "eeg_duration_sec": round(timestamps[-1] - timestamps[0], 3) if len(timestamps) > 1 else 0,
            "dropped_packets_at_end": dropped_end,
            "sync": sync,
            "warnings": warnings,
            "error": error,
        }
        self._write_meta(**self.result)
        self.status = status
        self.message = error or "; ".join(warnings)
        logger.info("Sesja %s zakończona: %s", self.name, status)

    async def abort(self) -> None:
        """Przerywa zadanie z panelu (dane zebrane do tej pory zostają zapisane)."""
        if self.process and self.process.returncode is None:
            self._abort_requested = True
            # Sygnał (jak Ctrl+C) pozwala zadaniu wykonać blok ``finally`` i zapisać dane zachowania.
            # Windows nie ma SIGINT dla procesów potomnych – tam działa CTRL_BREAK_EVENT.
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
            except (OSError, ValueError):
                # Np. Windows bez konsoli (pythonw) – zostaje twarde zakończenie.
                self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(self.process.wait()), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()

    def status_dict(self) -> Dict[str, Any]:
        events = 0
        if self.paths.events.exists() and self.running:
            with self.paths.events.open(encoding="utf-8") as handle:
                events = max(0, sum(1 for _ in handle) - 1)
        return {
            "session": self.name,
            "task": self.task["key"],
            "task_name": self.task["name"],
            "pseudonym": self.pseudonym,
            "status": self.status,
            "running": self.running,
            "message": self.message,
            "elapsed_sec": round((self.ended or time.time()) - self.started, 1),
            "eeg_samples": len(self.writer.timestamps) if self.writer else 0,
            "events": events if self.running else self.result.get("sync", {}).get("events_total", 0),
            "directory": str(self.paths.dir),
            "files": session_files(self.paths),
        }


# --------------------------------------------------------------------------
# Sesje i analiza
# --------------------------------------------------------------------------

def session_files(paths: SessionPaths) -> List[str]:
    if not paths.dir.exists():
        return []
    return sorted(p.name for p in paths.dir.iterdir() if p.is_file() and not p.name.endswith(".part.csv"))


def list_sessions(task_code: Optional[str] = None, pseudonym: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    result = []
    for paths in find_sessions(task_code, pseudonym)[:limit]:
        meta = paths.read_meta()
        files = session_files(paths)
        result.append({
            "session": paths.name,
            "task_code": meta.get("task_code"),
            "task_key": meta.get("task_key"),
            "pseudonym": meta.get("pseudonym"),
            "created": meta.get("created"),
            "status": meta.get("status"),
            "eeg_recorded": meta.get("eeg_recorded", False),
            "eeg_duration_sec": meta.get("eeg_duration_sec"),
            "warnings": meta.get("warnings", []),
            "files": files,
            "png": [name for name in files if name.endswith(".png")],
        })
    return result


async def run_analysis(task: Dict[str, Any], paths: SessionPaths) -> Dict[str, Any]:
    """Uruchamia tasks/<zadanie>/analysis.py dla wskazanej sesji jako osobny proces."""
    command = [sys.executable, "-u", "-m", f"tasks.{task['key']}.analysis", "--session", str(paths.dir)]
    process = await asyncio.create_subprocess_exec(
        *command, cwd=str(PROJECT_ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **child_process_options(),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=ANALYSIS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        return {"status": "error", "message": "Analiza przekroczyła limit czasu.", "session": paths.name}
    text = output.decode("utf-8", errors="replace")
    paths.output("_analysis.log").write_text(text, encoding="utf-8")
    files = session_files(paths)
    return {
        "status": "ok" if process.returncode == 0 else "error",
        "message": "" if process.returncode == 0 else text.strip().splitlines()[-1] if text.strip() else "Błąd analizy",
        "session": paths.name,
        "output": text[-4000:],
        "files": files,
        "png": [name for name in files if name.endswith(".png")],
    }
