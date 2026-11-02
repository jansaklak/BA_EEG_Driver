"""Źródło EEG (plik na żywo / LSL) i dekoder SSVEP (CCA) dla zadania SSVEP.

Zadanie nie łączy się z opaską — EEG zapisuje panel. Dane bierzemy z:

  1. pliku roboczego sesji `*_eeg.part.csv` (panel dopisuje do niego próbki na
     żywo; czasy `lsl_time` są z tego samego zegara co triggery), albo
  2. strumienia LSL typu "EEG" (gdy panel/sterownik go wystawia).

Przepływ danych:

    plik / LSL → EegStream.pull() → filtr 5–45 Hz + notch 50 Hz
               → bufor → EegStream.window() → SsvepDecoder.scores()

SsvepDecoder liczy dla każdej częstotliwości migania kanoniczną korelację (CCA)
między oknem EEG a sygnałami referencyjnymi sin/cos (podstawowa + harmoniczne).
Kwadrat o najwyższej korelacji = ten, na który patrzy badany. Nie wymaga
kalibracji.
"""

from __future__ import annotations

import math
import os
import re
import time
from pathlib import Path

import numpy as np

try:
    from scipy import signal as _signal
except ImportError:  # bez scipy działamy na prostszym odtrendowaniu okna
    _signal = None


# ───────────────────────────── CCA ─────────────────────────────

def reference_signals(freq: float, fs: float, n: int, harmonics: int) -> np.ndarray:
    """Macierz (n, 2·harmonics): sin/cos dla f, 2f, ..."""
    t = np.arange(n) / fs
    cols = []
    for h in range(1, harmonics + 1):
        cols.append(np.sin(2 * math.pi * h * freq * t))
        cols.append(np.cos(2 * math.pi * h * freq * t))
    return np.column_stack(cols)


def _orthonormal_basis(a: np.ndarray) -> np.ndarray:
    """Ortonormalna baza przestrzeni kolumn (odporna na zerowe/zdublowane kanały)."""
    u, s, _ = np.linalg.svd(a, full_matrices=False)
    if s.size == 0 or s[0] <= 1e-12:
        return u[:, :0]
    return u[:, s > s[0] * 1e-6]


def cca_correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Największa kanoniczna korelacja między X (n, kanały) i Y (n, referencje)."""
    qx = _orthonormal_basis(x - x.mean(axis=0))
    if qx.shape[1] == 0:
        return 0.0
    qy = _orthonormal_basis(y - y.mean(axis=0))
    s = np.linalg.svd(qx.T @ qy, compute_uv=False)
    return float(min(s[0], 1.0)) if s.size else 0.0


class SsvepDecoder:
    """Dekoder CCA: {kierunek: częstotliwość} → {kierunek: korelacja}."""

    def __init__(self, freqs: dict[str, float], fs: float, harmonics: int = 2):
        self.freqs = dict(freqs)
        self.fs = float(fs)
        self.harmonics = harmonics

    def scores(self, x: np.ndarray) -> dict[str, float]:
        n = x.shape[0]
        return {
            d: cca_correlation(x, reference_signals(f, self.fs, n, self.harmonics))
            for d, f in self.freqs.items()
        }


# ─────────────────────── wybór kanałów / filtr ───────────────────────

_OCCIPITAL = re.compile(r"(?i)^(po?|o)(z|\d+)")  # O1, Oz, PO7, P3, Pz ...


def pick_channels(labels: list[str]) -> list[int]:
    """Kanały potyliczno-ciemieniowe (O*, PO*, P*); jeśli brak etykiet — wszystkie."""
    idx = [i for i, label in enumerate(labels) if _OCCIPITAL.match(label.strip())]
    return idx or list(range(len(labels)))


def _build_sos(fs: float, mains: float = 50.0, low: float = 5.0, high: float = 45.0):
    high = min(high, 0.45 * fs)
    sos = _signal.butter(4, [low, high], btype="bandpass", fs=fs, output="sos")
    if mains < 0.5 * fs:
        b, a = _signal.iirnotch(mains, 30.0, fs)
        sos = np.vstack([sos, _signal.tf2sos(b, a)])
    return sos


def _detrend(x: np.ndarray) -> np.ndarray:
    n = x.shape[0]
    a = np.column_stack([np.ones(n), np.linspace(-1.0, 1.0, n)])
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    return x - a @ coef


# ───────────────────────── czytnik pliku na żywo ─────────────────────────

class _LiveCsvReader:
    """Dogrywa nowe wiersze z pliku CSV, który inny proces właśnie zapisuje.

    Oczekiwany nagłówek jak w pliku EEG sesji: `lsl_time`, kanały (µV), opcjonalnie
    `sample_index`/`trigger`. Niepełna ostatnia linia jest odkładana do następnego
    odczytu. Odczyt jest ograniczony do ~20 razy na sekundę.
    """

    MAX_BACKLOG = 200_000   # bajtów; starsze dane pomijamy (ok. 10 s)
    META_COLUMNS = {"sample_index", "lsl_time", "trigger"}

    def __init__(self, path):
        self.path = Path(path)
        self._f = None
        self._carry = b""
        self._last_read = 0.0
        self.header: list[str] | None = None
        self.labels: list[str] = []
        self._ts_col = 0
        self._ch_cols: list[int] = []

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def _open(self) -> bool:
        if self._f is None:
            if not self.path.exists():
                return False
            self._f = open(self.path, "rb")
        if self.header is None:
            line = self._f.readline()
            if not line.endswith(b"\n"):        # nagłówek jeszcze niepełny
                self._f.seek(0)
                return False
            names = [n.strip() for n in line.decode("utf-8-sig").split(",")]
            if "lsl_time" not in names:
                raise ValueError(f"brak kolumny lsl_time w nagłówku: {names}")
            self._ts_col = names.index("lsl_time")
            self._ch_cols = [i for i, n in enumerate(names) if n not in self.META_COLUMNS]
            self.labels = [names[i] for i in self._ch_cols]
            self.header = names
            size = os.fstat(self._f.fileno()).st_size
            if size - self._f.tell() > self.MAX_BACKLOG:   # pomiń stare zaległości
                self._f.seek(size - self.MAX_BACKLOG)
                self._f.readline()
        return True

    def read(self):
        """(próbki (k, kanały), czasy lsl (k,)) — nowe wiersze od ostatniego odczytu."""
        empty = (np.zeros((0, len(self._ch_cols))), np.zeros(0))
        now = time.perf_counter()
        if now - self._last_read < 0.05 or not self._open():
            return empty
        self._last_read = now
        chunk = self._f.read()
        if not chunk:
            return empty
        data = self._carry + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self._carry = data
            return empty
        self._carry = data[cut + 1:]
        n_cols = len(self.header)
        rows = []
        for line in data[:cut].decode("utf-8", "replace").split("\n"):
            parts = line.strip().split(",")
            if len(parts) != n_cols:
                continue
            try:
                rows.append([float(v) if v else math.nan for v in parts])
            except ValueError:
                continue
        if not rows:
            return empty
        arr = np.asarray(rows, dtype=float)
        return arr[:, self._ch_cols], arr[:, self._ts_col]


# ───────────────────────────── strumień EEG ─────────────────────────────

class EegStream:
    """Czyta próbki (z pliku na żywo, LSL albo symulatora), filtruje i trzyma bufor.

    `read()` zwraca `(próbki (k, kanały), czasy|None)`. `pull()` trzeba wołać
    często (co klatkę) — także poza fazą migania, żeby stan filtra był ciągły.

    `mark(t)` zaczyna okno analizy: dla źródeł z czasami (plik) obejmuje tylko
    próbki z czasem ≥ t, więc opóźnienie zapisu nie miesza w oknie danych sprzed
    migania; dla pozostałych — próbki, które dotrą po wywołaniu.
    """

    def __init__(self, read, fs: float, labels: list[str], description: str,
                 channels: list[int] | None = None, mains: float = 50.0,
                 keep_sec: float = 8.0, timestamped: bool = False,
                 clock=None, stale_after: float = 1.0, kind: str = "lsl"):
        self._read = read
        self.fs = float(fs)
        self.labels = list(labels)
        self.description = description
        self.kind = kind
        self.timestamped = timestamped
        self.stale_after = stale_after
        self._clock = clock
        self.channels = list(channels) if channels else pick_channels(self.labels)
        self.used_labels = [self.labels[i] for i in self.channels]
        self.all_channels_used = len(self.channels) == len(self.labels)
        self._keep = int(keep_sec * self.fs)
        self._buf = np.zeros((0, len(self.channels)))
        self._t_min: float | None = None
        self._last_data = time.perf_counter()
        self._last_ts: float | None = None
        self._sos = _build_sos(self.fs, mains) if _signal is not None else None
        self._zi = None
        if self._sos is None:
            print("UWAGA: brak scipy — filtr pasmowy wyłączony (tylko odtrendowanie okna).")

    # --- plik roboczy sesji (zapisywany na żywo przez panel) ---
    @classmethod
    def from_live_csv(cls, path, fs_hint: float | None = None, timeout: float = 5.0,
                      channels: list[int] | None = None, clock=None) -> "EegStream | None":
        reader = _LiveCsvReader(path)
        deadline = time.perf_counter() + timeout
        xs, tss, n = [], [], 0
        while time.perf_counter() < deadline:
            try:
                x, ts = reader.read()
            except Exception as exc:  # noqa: BLE001
                print(f"UWAGA: nie umiem czytać {path}: {exc}")
                reader.close()
                return None
            if len(ts):
                xs.append(x)
                tss.append(ts)
                n += len(ts)
            if n >= 50:
                break
            time.sleep(0.05)
        if n < 50:
            print(f"Plik EEG sesji: brak napływających danych ({path}).")
            reader.close()
            return None
        x0, ts0 = np.vstack(xs), np.concatenate(tss)

        lag = (clock() - ts0[-1]) if clock else 0.0
        if clock and lag > 5.0:
            print(f"Plik EEG sesji nie jest świeży (najnowsza próbka sprzed {lag:.1f} s) — pomijam.")
            reader.close()
            return None

        fs = float(fs_hint) if fs_hint and fs_hint > 0 else 0.0
        if fs <= 0:
            span = ts0[-1] - ts0[0]
            fs = (len(ts0) - 1) / span if span > 0 else 0.0
            if fs <= 0:
                print("Plik EEG sesji: nie mogę ustalić częstotliwości próbkowania.")
                reader.close()
                return None
            fs = round(fs, 1)
            print(f"Częstotliwość próbkowania oszacowana z czasów próbek: {fs:g} Hz")

        pending = [(x0, ts0)]

        def read():
            if pending:
                return pending.pop()
            return reader.read()

        desc = f"EEG z pliku sesji ({len(reader.labels)} kanałów, {fs:g} Hz)"
        return cls(read, fs, reader.labels, desc, channels, timestamped=True,
                   clock=clock, stale_after=3.0, kind="file")

    # --- strumień LSL ---
    @classmethod
    def connect(cls, name: str | None = None, timeout: float = 5.0,
                channels: list[int] | None = None) -> "EegStream | None":
        try:
            import pylsl
        except ImportError:
            print("UWAGA: brak biblioteki pylsl — nie mogę czytać EEG z LSL.")
            return None

        infos = []
        try:
            prop, value = ("name", name) if name else ("type", "EEG")
            infos = pylsl.resolve_byprop(prop, value, minimum=1, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"UWAGA: resolve_byprop nie zadziałało: {exc}")

        if not infos:  # plan B: przejrzyj wszystkie strumienie
            all_streams = pylsl.resolve_streams(1.0)
            print("Strumienie LSL widoczne w sieci:",
                  [(s.name(), s.type(), s.channel_count()) for s in all_streams] or "brak")
            for s in ([] if name else all_streams):  # jawnie podana nazwa → bez zgadywania
                n, t = s.name().lower(), s.type().lower()
                if "marker" in n or t in ("markers", "marker"):
                    continue
                if t == "eeg" or "eeg" in n or "brainaccess" in n:
                    infos.append(s)
        if not infos:
            return None

        info = infos[0]
        n_ch = info.channel_count()
        fs = info.nominal_srate()
        if fs <= 0:
            print("UWAGA: strumień bez nominalnej częstotliwości — zakładam 250 Hz.")
            fs = 250.0

        inlet = pylsl.StreamInlet(info, max_buflen=30)

        # etykiety kanałów są dopiero w pełnym opisie strumienia (inlet.info())
        labels = []
        try:
            ch = inlet.info(timeout=5.0).desc().child("channels").child("channel")
            for i in range(n_ch):
                labels.append(ch.child_value("label") or f"ch{i + 1}")
                ch = ch.next_sibling()
        except Exception:  # noqa: BLE001
            labels = [f"ch{i + 1}" for i in range(n_ch)]

        def read():
            samples, _ = inlet.pull_chunk(timeout=0.0, max_samples=4096)
            return np.asarray(samples, dtype=float).reshape(-1, n_ch), None

        desc = f"EEG z LSL: {info.name()} ({n_ch} kanałów, {fs:g} Hz)"
        return cls(read, fs, labels, desc, channels, kind="lsl")

    # --- przetwarzanie ---
    def pull(self) -> int:
        x, ts = self._read()
        if x is None or len(x) == 0:
            return 0
        x = np.asarray(x, dtype=float)[:, self.channels]
        ok = np.isfinite(x).all(axis=1)
        if ts is not None:
            ts = np.asarray(ts, dtype=float)
            ok &= np.isfinite(ts)
            ts = ts[ok]
        x = x[ok]
        if not len(x):
            return 0
        if self._sos is not None:
            if self._zi is None:
                self._zi = _signal.sosfilt_zi(self._sos)[:, :, None] * x[0][None, None, :]  # (sekcje, 2, kanały)
            x, self._zi = _signal.sosfilt(self._sos, x, axis=0, zi=self._zi)
        self._last_data = time.perf_counter()
        if ts is not None:
            self._last_ts = float(ts[-1])
            if self._t_min is not None:
                x = x[ts >= self._t_min]
        self._buf = np.vstack([self._buf, x])[-self._keep:]
        return len(x)

    def mark(self, t: float | None = None) -> None:
        """Początek nowego okna analizy (t — czas zegara triggerów, jeśli znany)."""
        self._buf = np.zeros((0, len(self.channels)))
        self._t_min = t if self.timestamped else None

    def window(self, seconds: float, min_seconds: float) -> np.ndarray | None:
        """Ostatnie `seconds` próbek od `mark()`; None, jeśli jest ich < min_seconds."""
        if len(self._buf) < int(min_seconds * self.fs):
            return None
        x = self._buf[-int(seconds * self.fs):]
        return _detrend(x) if self._sos is None else x

    def age(self) -> float:
        """Jak stare są najnowsze dane (s): wg ich czasów (plik) lub momentu odbioru."""
        if self.timestamped and self._clock is not None and self._last_ts is not None:
            return self._clock() - self._last_ts
        return time.perf_counter() - self._last_data


# ───────────────────────────── symulator ─────────────────────────────

class SimulatedEeg:
    """Syntetyczne EEG: szum + dryf + sieć 50 Hz + SSVEP na częstotliwości `gaze`.

    Do testów bez opaski. `gaze` = kierunek kwadratu, na który „patrzy" badany
    (albo None — wtedy sam szum).
    """

    labels = ["O1", "O2"]

    def __init__(self, freqs: dict[str, float], fs: float = 250.0,
                 amplitude: float = 1.5, seed: int | None = None):
        self.freqs = dict(freqs)
        self.fs = fs
        self.amplitude = amplitude
        self.gaze: str | None = None
        self._rng = np.random.default_rng(seed)
        self._t0 = time.perf_counter()
        self._n = 0

    def read(self):
        due = int((time.perf_counter() - self._t0) * self.fs)
        k = due - self._n
        if k <= 0:
            return np.zeros((0, 2)), None
        t = np.arange(self._n, due) / self.fs
        self._n = due
        x = self._rng.normal(0.0, 3.0, (k, 2))
        x += (20 * np.sin(2 * math.pi * 0.3 * t) + 4 * np.sin(2 * math.pi * 50 * t))[:, None]
        if self.gaze in self.freqs:
            f = self.freqs[self.gaze]
            sig = (np.sin(2 * math.pi * f * t + 0.7)
                   + 0.4 * np.sin(2 * math.pi * 2 * f * t + 0.3))
            x += self.amplitude * sig[:, None] * np.array([1.0, 0.8])
        return x, None

    def as_stream(self) -> EegStream:
        return EegStream(self.read, self.fs, self.labels,
                         "SYMULACJA (brak danych EEG)", kind="sim")
