"""Wspólne narzędzia dla wszystkich zadań i analiz.

Każda sesja ma jeden katalog i jeden prefiks nazw plików:

    recordings/ZADANIE_PSEUDONIM_DATA/
        ZADANIE_PSEUDONIM_DATA.csv            EEG (µV) + kolumna ``trigger``
        ZADANIE_PSEUDONIM_DATA_events.csv     lista triggerów (czas LSL, próbka)
        ZADANIE_PSEUDONIM_DATA_behavior.csv   dane zachowania z zadania
        ZADANIE_PSEUDONIM_DATA_meta.json      wszystkie metadane sesji
        ZADANIE_PSEUDONIM_DATA_task.log       wyjście procesu zadania

Czas EEG i czas triggerów pochodzą z tego samego zegara ``local_clock()``
(``pylsl.local_clock`` – zegar monotoniczny systemu), więc nie wymagają synchronizacji.
"""

from __future__ import annotations

import csv
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
TASKS_DIR = Path(__file__).resolve().parent

# Pseudonim trafia do nazwy pliku, więc dopuszczamy tylko litery, cyfry i "-".
# Podkreślenie "_" jest separatorem pól w nazwie i nie może wystąpić w pseudonimie.
PSEUDONYM_RE = re.compile(r"^[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż-]{1,40}$")

# Wynik procesu zadania, który rozumie panel WWW.
EXIT_OK = 0
EXIT_ABORTED = 2


def _load_pylsl():
    """pylsl wymaga biblioteki liblsl. Na macOS i Windows jest w pakiecie, na Linuksie
    trzeba ją doinstalować (README). Bez niej działa wszystko poza strumieniami LSL.
    """
    try:
        import pylsl
        pylsl.local_clock()
        return pylsl
    except Exception:  # ImportError albo RuntimeError "LSL binary library file was not found"
        return None


PYLSL = _load_pylsl()


def local_clock() -> float:
    """Wspólny zegar EEG i triggerów (sekundy, monotoniczny).

    Bez liblsl używamy ``time.monotonic()``. Panel i zadania działają w tym samym
    środowisku Pythona, więc albo wszystkie procesy mają liblsl, albo wszystkie przechodzą
    na ``time.monotonic()`` – czasy EEG i triggerów są zawsze z jednego zegara. (Na Linuksie
    oba zegary to CLOCK_MONOTONIC; na macOS różnią się przesunięciem, stąd ta zasada.)
    """
    return PYLSL.local_clock() if PYLSL else time.monotonic()


def validate_pseudonym(pseudonym: str) -> str:
    pseudonym = (pseudonym or "").strip()
    if not PSEUDONYM_RE.match(pseudonym):
        raise ValueError(
            "ID/pseudonim może zawierać tylko litery, cyfry i '-' (bez spacji i '_' ), max 40 znaków."
        )
    return pseudonym


def session_name(task_code: str, pseudonym: str, when: datetime | None = None) -> str:
    """Nazwa sesji w formacie ZADANIE_PSEUDONIM_DATA, np. FLANKER_osoba01_20261030-183005."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{task_code}_{validate_pseudonym(pseudonym)}_{stamp}"


class SessionPaths:
    """Jednoznaczne ścieżki wszystkich plików jednej sesji."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.name = self.dir.name

    def _file(self, suffix: str) -> Path:
        return self.dir / f"{self.name}{suffix}"

    @property
    def eeg(self) -> Path:
        return self._file(".csv")

    @property
    def eeg_partial(self) -> Path:
        # Plik roboczy zapisywany na żywo; po zakończeniu sesji zamieniany na .csv z triggerami.
        return self._file("_eeg.part.csv")

    @property
    def events(self) -> Path:
        return self._file("_events.csv")

    @property
    def behavior(self) -> Path:
        return self._file("_behavior.csv")

    @property
    def meta(self) -> Path:
        return self._file("_meta.json")

    @property
    def log(self) -> Path:
        return self._file("_task.log")

    def output(self, suffix: str) -> Path:
        """Plik wynikowy analizy, np. output('_erp.png')."""
        return self._file(suffix)

    def read_meta(self) -> dict[str, Any]:
        if not self.meta.exists():
            return {}
        return json.loads(self.meta.read_text(encoding="utf-8"))


def find_sessions(task_code: str | None = None, pseudonym: str | None = None,
                  recordings_dir: Path = RECORDINGS_DIR) -> list[SessionPaths]:
    """Zwraca sesje (od najnowszej) pasujące do zadania i/lub pseudonimu."""
    if not recordings_dir.exists():
        return []
    sessions = []
    for directory in recordings_dir.iterdir():
        paths = SessionPaths(directory)
        if not directory.is_dir() or not paths.meta.exists():
            continue
        meta = paths.read_meta()
        if task_code and meta.get("task_code") != task_code:
            continue
        if pseudonym and meta.get("pseudonym") != pseudonym:
            continue
        sessions.append(paths)
    # Data jest w nazwie, ale sortujemy po metadanych, żeby nie zależeć od formatu nazwy.
    sessions.sort(key=lambda p: p.read_meta().get("created", ""), reverse=True)
    return sessions


def resolve_session(task_code: str, pseudonym: str | None = None,
                    session: str | None = None) -> SessionPaths:
    """Wybiera sesję do analizy: jawnie podany katalog albo najnowszą sesję danego ID."""
    if session:
        path = Path(session)
        if not path.is_absolute() and not path.exists():
            path = RECORDINGS_DIR / session
        paths = SessionPaths(path)
        if not paths.meta.exists():
            raise FileNotFoundError(f"Brak metadanych sesji: {paths.meta}")
        return paths
    if not pseudonym:
        raise ValueError("Podaj ID (pseudonim) albo katalog sesji.")
    sessions = find_sessions(task_code, validate_pseudonym(pseudonym))
    if not sessions:
        raise FileNotFoundError(f"Nie znaleziono sesji {task_code} dla ID '{pseudonym}' w {RECORDINGS_DIR}")
    return sessions[0]


def create_standalone_session(task_code: str, pseudonym: str, task_key: str,
                              params: dict[str, Any]) -> SessionPaths:
    """Tworzy katalog sesji, gdy zadanie uruchomiono z terminala, a nie z panelu.

    W tym trybie nie ma zapisu EEG – powstaną tylko triggery i dane zachowania.
    """
    name = session_name(task_code, pseudonym)
    paths = SessionPaths(RECORDINGS_DIR / name)
    paths.dir.mkdir(parents=True, exist_ok=False)
    paths.meta.write_text(json.dumps({
        "session": name,
        "task_code": task_code,
        "task_key": task_key,
        "pseudonym": pseudonym,
        "created": datetime.now().isoformat(timespec="seconds"),
        "params": params,
        "eeg_recorded": False,
        "note": "Zadanie uruchomione poza panelem WWW – brak zapisu EEG.",
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths

# --------------------------------------------------------------------------
# Triggery (markery) zapisywane przez zadanie
# --------------------------------------------------------------------------

class MarkerLog:
    """Zapisuje triggery do pliku _events.csv i (opcjonalnie) do strumienia LSL.

    Czas triggera to ``local_clock()`` – ten sam zegar, którym panel WWW
    oznacza próbki EEG. Każdy wiersz jest od razu zapisywany na dysk (flush),
    więc przerwanie zadania nie gubi triggerów.
    """
    def __init__(self, path: Path, lsl_name: str | None = None, source_id: str | None = None):
        self.path = path
        self.file = open(path, "w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.writer.writerow(["lsl_time", "marker"])
        self.file.flush()
        self.lsl_outlet = None
        if lsl_name and PYLSL:
            info = PYLSL.StreamInfo(lsl_name, "markers", 1, 0.0, "string", source_id or "brainaccess")
            self.lsl_outlet = PYLSL.StreamOutlet(info)
        self.start_time = local_clock()

    def now(self) -> float:
        return local_clock()

    def push(self, label: str) -> float:
        ts = local_clock()
        self.writer.writerow([f"{ts:.6f}", label])
        self.file.flush()
        if self.lsl_outlet:
            self.lsl_outlet.push_sample([label])
        return ts

    def close(self) -> None:
        self.file.close()

class TaskAborted(RuntimeError):
    pass

def check_abort(pygame, event) -> None:
    if event.type == pygame.QUIT:
        raise TaskAborted()
    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
        raise TaskAborted()

def open_fullscreen(title: str):
    import pygame
    pygame.init()
    pygame.display.set_caption(title)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    return pygame, screen

def wait_for_space(pygame, screen, lines: list[str]):
    font = pygame.font.Font(None, 48)
    def draw():
        screen.fill((0, 0, 0))
        y = screen.get_height() // 2 - (len(lines) * 30)
        for line in lines:
            img = font.render(line, True, (255, 255, 255))
            rect = img.get_rect(center=(screen.get_width() // 2, y))
            screen.blit(img, rect)
            y += 60
        pygame.display.flip()
    draw()
    while True:
        for event in pygame.event.get():
            check_abort(pygame, event)
            if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE:
                return
        pygame.time.wait(10)

def add_session_arguments(parser: argparse.ArgumentParser) -> None:
    """Dodaje wspólne argumenty sesji obsługiwane przez panel WWW i terminal."""
    parser.add_argument("--id", type=str, required=True, help="Participant identifier")
    parser.add_argument("--target", type=str, default=None, help="Device address (optional)")
    # Panel WWW przekazuje --session-dir z gotowym katalogiem sesji (z _meta.json).
    parser.add_argument("--session-dir", type=str, default=None,
                        help="Katalog sesji utworzony przez panel WWW (nie twórz nowego)")

def prepare_session(args, task_code: str, task_key: str, params: dict) -> SessionPaths:
    """Jeśli panel podał --session-dir, używamy tego katalogu; inaczej tworzymy nowy."""
    session_dir = getattr(args, "session_dir", None)
    if session_dir:
        # Panel WWW już utworzył katalog i _meta.json – używamy go.
        return SessionPaths(Path(session_dir))
    pseudonym = args.id
    if getattr(args, "session", None):
        return resolve_session(task_code, pseudonym, args.session)
    else:
        return create_standalone_session(task_code, pseudonym, task_key, params)

def write_rows(path: Path, fields: list[str], rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

def load_behavior(paths: SessionPaths) -> list[dict[str, str]]:
    if not paths.behavior.exists():
        raise FileNotFoundError(f"Brak pliku danych zachowania: {paths.behavior}")

    with paths.behavior.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_eeg(paths: SessionPaths) -> dict[str, Any]:
    if not paths.eeg.exists():
        raise FileNotFoundError(f"Brak pliku EEG: {paths.eeg}")

    meta = paths.read_meta()
    sample_rate = float(meta.get("sample_rate_hz", 0))
    if sample_rate <= 0:
        raise ValueError(f"Brak poprawnej częstotliwości próbkowania w {paths.meta}")

    with paths.eeg.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        channels = [
            name for name in fieldnames
            if name not in {"sample_index", "lsl_time", "trigger"}
        ]
        if "lsl_time" not in fieldnames or not channels:
            raise ValueError(f"Niepoprawny nagłówek pliku EEG: {paths.eeg}")

        rows = list(reader)

    if not rows:
        raise ValueError(f"Plik EEG jest pusty: {paths.eeg}")

    timestamps = np.asarray([float(row["lsl_time"]) for row in rows], dtype=float)
    samples = np.asarray(
        [[float(row[channel]) for channel in channels] for row in rows],
        dtype=float,
    )

    return {
        "samples": samples,
        "timestamps": timestamps,
        "channels": channels,
        "fs": sample_rate,
    }


def save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Obiekt nie może być zapisany jako JSON: {type(value).__name__}")
