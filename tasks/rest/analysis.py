"""Analiza sesji REST: widmo EEG i moc pasm dla oczu otwartych vs zamkniętych.

    .venv/bin/python -m tasks.rest.analysis --id osoba01
    .venv/bin/python -m tasks.rest.analysis --session REST_osoba01_20261030-183005

Wyniki w katalogu sesji:
    *_spectrum.png   średnie widmo (open vs closed) + moc pasm wg kanału
    *_alpha.png      stosunek mocy alfa closed/open dla każdego kanału
    *_rest.npz       dane numeryczne
    *_analysis.json  podsumowanie
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np

from tasks.common import load_behavior, load_eeg, resolve_session, save_json

TASK_CODE = "REST"
BANDS = {"delta": (1.0, 4.0), "theta": (4.0, 8.0), "alpha": (8.0, 13.0), "beta": (13.0, 30.0), "gamma": (30.0, 40.0)}
COLORS = {"open": "#2563eb", "closed": "#dc2626"}
LABELS = {"open": "oczy otwarte", "closed": "oczy zamknięte"}


def bandpass(samples: np.ndarray, fs: float) -> np.ndarray:
    from scipy import signal

    sos = signal.butter(4, [1.0, 40.0], btype="bandpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, samples, axis=0)


def welch(segment: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    from scipy import signal

    # Stałe okno 2 s (rozdzielczość 0,5 Hz) – wszystkie bloki mają tę samą oś częstotliwości.
    return signal.welch(segment, fs=fs, axis=0, nperseg=int(fs * 2))


def band_powers(frequencies: np.ndarray, spectrum: np.ndarray) -> np.ndarray:
    """Całka widma w każdym paśmie -> tablica [pasma x kanały] w µV²."""
    rows = []
    for low, high in BANDS.values():
        mask = (frequencies >= low) & (frequencies < high)
        rows.append(np.trapezoid(spectrum[mask], frequencies[mask], axis=0))
    return np.vstack(rows)


def save_spectrum_png(path, frequencies, spectra, powers, channels, counts, title) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(13, 10), constrained_layout=True)
    for state in ("open", "closed"):
        if state in spectra:
            axes[0].plot(frequencies, spectra[state].mean(axis=(0, 2)), color=COLORS[state],
                         label=f"{LABELS[state]} (bloków: {counts[state]})")
    for low, high in BANDS.values():
        axes[0].axvline(low, color="#e2e8f0", lw=0.8)
    axes[0].set_yscale("log")
    axes[0].set_xlim(1, 40)
    # Zakres osi Y tylko z pasma 1–40 Hz (poza nim filtr daje wartości bliskie zera).
    visible = (frequencies >= 1) & (frequencies <= 40)
    values = np.concatenate([spectra[s].mean(axis=(0, 2))[visible] for s in spectra])
    axes[0].set_ylim(values.min() * 0.5, values.max() * 2)
    axes[0].set_xlabel("Częstotliwość (Hz)")
    axes[0].set_ylabel("PSD (µV²/Hz)")
    axes[0].set_title("Średnie widmo (wszystkie kanały)")
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    # Grupy słupków: kanał × pasmo, open i closed obok siebie.
    band_names = list(BANDS)
    x = np.arange(len(channels))
    group = 0.85 / len(band_names)
    for b_index, band in enumerate(band_names):
        for s_index, state in enumerate(("open", "closed")):
            if state in powers:
                offset = -0.425 + b_index * group + s_index * group / 2
                axes[1].bar(x + offset, powers[state][b_index], width=group / 2, align="edge",
                            color=COLORS[state], alpha=0.35 + 0.13 * b_index,
                            label=LABELS[state] if b_index == 0 else None)
    axes[1].set_xticks(x, channels)
    axes[1].set_yscale("log")
    axes[1].set_ylabel("Moc (µV²)")
    axes[1].set_title("Moc pasm wg kanału (w każdej grupie: δ θ α β γ, jaśniej → ciemniej)")
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend()
    figure.suptitle(title, fontsize=15, fontweight="bold")
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def save_alpha_png(path, channels, ratio, title) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(10, 4.5))
    colors = ["#16a34a" if value > 1 else "#94a3b8" for value in ratio]
    axis.bar(channels, ratio, color=colors)
    axis.axhline(1.0, color="#475569", lw=1, ls="--")
    for index, value in enumerate(ratio):
        axis.text(index, value, f"{value:.2f}", ha="center", va="bottom", fontsize=9)
    axis.set_ylabel("Moc alfa: zamknięte / otwarte")
    axis.set_title("Reaktywność alfa (Berger) — > 1 oznacza wzrost alfy przy zamkniętych oczach")
    axis.grid(axis="y", alpha=0.25)
    figure.suptitle(title, fontsize=13, fontweight="bold")
    figure.tight_layout()
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def analyze(paths) -> dict[str, Any]:
    meta = paths.read_meta()
    title_id = f"{meta.get('pseudonym', '?')} · {paths.name}"

    # 1. EEG i granice bloków (oba w czasie LSL – bez dodatkowej synchronizacji).
    eeg = load_eeg(paths)
    samples, timestamps, channels, fs = eeg["samples"], eeg["timestamps"], eeg["channels"], eeg["fs"]
    blocks = [row for row in load_behavior(paths) if row["state"] in ("open", "closed")]
    filtered = bandpass(samples, fs)

    # 2. Widmo Welcha dla każdego bloku (pomijamy pierwsze 2 s – czas na zmianę stanu oczu).
    spectra: dict[str, list[np.ndarray]] = {"open": [], "closed": []}
    powers: dict[str, list[np.ndarray]] = {"open": [], "closed": []}
    frequencies = np.array([])
    skipped = []
    for block in blocks:
        start = float(block["start_lsl_time"]) + 2.0
        end = float(block["end_lsl_time"])
        segment = filtered[(timestamps >= start) & (timestamps <= end)]
        if len(segment) < int(fs * 2):
            skipped.append(int(block["block"]))
            continue
        frequencies, spectrum = welch(segment, fs)
        spectra[block["state"]].append(spectrum)
        powers[block["state"]].append(band_powers(frequencies, spectrum))

    counts = {state: len(values) for state, values in spectra.items()}
    if not any(counts.values()):
        raise ValueError("Żaden blok nie ma ≥ 2 s danych EEG – sprawdź, czy zapis EEG obejmował zadanie.")
    spectrum_arrays = {s: np.stack(v) for s, v in spectra.items() if v}
    power_arrays = {s: np.mean(np.stack(v), axis=0) for s, v in powers.items() if v}

    # 3. Wykresy PNG i dane.
    spectrum_png = paths.output("_spectrum.png")
    save_spectrum_png(spectrum_png, frequencies, spectrum_arrays, power_arrays, channels, counts,
                      f"REST — oczy otwarte vs zamknięte ({title_id})")
    outputs = {"spectrum_png": str(spectrum_png)}
    alpha_ratio = None
    if "open" in power_arrays and "closed" in power_arrays:
        alpha_index = list(BANDS).index("alpha")
        alpha_ratio = power_arrays["closed"][alpha_index] / np.maximum(power_arrays["open"][alpha_index], 1e-12)
        alpha_png = paths.output("_alpha.png")
        save_alpha_png(alpha_png, channels, alpha_ratio, f"REST ({title_id})")
        outputs["alpha_png"] = str(alpha_png)
    npz = paths.output("_rest.npz")
    np.savez_compressed(
        npz, frequencies=frequencies, channels=np.asarray(channels), bands=np.asarray(list(BANDS)),
        **{f"spectrum_{s}": v for s, v in spectrum_arrays.items()},
        **{f"power_{s}": v for s, v in power_arrays.items()},
    )
    outputs["npz"] = str(npz)

    summary = {
        "session": paths.name,
        "pseudonym": meta.get("pseudonym"),
        "sample_rate_hz": fs,
        "channels": channels,
        "blocks_available": len(blocks),
        "blocks_used": counts,
        "blocks_skipped": skipped,
        "bands_hz": BANDS,
        "band_power_uV2": {s: {band: dict(zip(channels, np.round(v[i], 3).tolist()))
                                for i, band in enumerate(BANDS)} for s, v in power_arrays.items()},
        "alpha_closed_open_ratio": dict(zip(channels, np.round(alpha_ratio, 3).tolist())) if alpha_ratio is not None else None,
        "outputs": outputs,
    }
    save_json(paths.output("_analysis.json"), summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Analiza REST: oczy otwarte vs zamknięte")
    parser.add_argument("--id", help="ID (pseudonim) – analizuje najnowszą sesję tego badanego")
    parser.add_argument("--session", help="Konkretny katalog sesji (nazwa lub ścieżka)")
    args = parser.parse_args()
    try:
        paths = resolve_session(TASK_CODE, args.id, args.session)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Analizuję sesję: {paths.dir}")
    print(json.dumps(analyze(paths), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
