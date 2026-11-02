"""Analiza sesji Flanker: ERP (correct vs incorrect) oraz czasy reakcji.

Wystarczy podać ID – analizowana jest najnowsza sesja FLANKER tego badanego:

    .venv/bin/python -m tasks.flanker.analysis --id osoba01
    .venv/bin/python -m tasks.flanker.analysis --session FLANKER_osoba01_20261030-183005

Wyniki trafiają do katalogu sesji:
    *_erp.png        uśrednione ERP dla każdego kanału
    *_rt.png         czasy reakcji i poprawność wg warunku
    *_erp.npz        dane numeryczne ERP
    *_analysis.json  podsumowanie (liczby epok, odrzucone epoki, parametry)
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np

from tasks.common import load_behavior, load_eeg, resolve_session, save_json

TASK_CODE = "FLANKER"


def filter_for_erp(samples: np.ndarray, fs: float) -> np.ndarray:
    """Pasmo 0,5–30 Hz + notch 49–51 Hz, filtracja dwukierunkowa (bez przesunięcia fazy)."""
    from scipy import signal

    bandpass = signal.butter(4, [0.5, 30.0], btype="bandpass", fs=fs, output="sos")
    notch = signal.butter(2, [49.0, 51.0], btype="bandstop", fs=fs, output="sos")
    return signal.sosfiltfilt(notch, signal.sosfiltfilt(bandpass, samples, axis=0), axis=0)


def remove_baseline_drift(epoch: np.ndarray, time_axis: np.ndarray, baseline_mask: np.ndarray) -> np.ndarray:
    """Odejmuje liniowy trend dopasowany do odcinka przed bodźcem (osobno dla każdego kanału)."""
    coefficients = np.polyfit(time_axis[baseline_mask], epoch[baseline_mask], deg=1)
    return epoch - (coefficients[0][None, :] * time_axis[:, None] + coefficients[1][None, :])


def artifact_channels(epoch: np.ndarray, channels: list[str], ptp_limit: float, peak_limit: float) -> list[str]:
    bad = (np.ptp(epoch, axis=0) > ptp_limit) | (np.max(np.abs(epoch), axis=0) > peak_limit)
    return [channel for channel, is_bad in zip(channels, bad) if is_bad]


def save_erp_png(path, time_axis, channels, erp_correct, erp_incorrect, counts, title) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = max(1, int(np.ceil(len(channels) / 2)))
    figure, axes = plt.subplots(rows, 2, figsize=(13, max(7, 2.5 * rows)), sharex=True, squeeze=False)
    axes_flat = axes.ravel()
    ms = time_axis * 1000
    for index, channel in enumerate(channels):
        axis = axes_flat[index]
        if erp_correct.size:
            axis.plot(ms, erp_correct[:, index], color="#2563eb", lw=1.5, label=f"Correct (n={counts['correct']})")
        if erp_incorrect.size:
            axis.plot(ms, erp_incorrect[:, index], color="#dc2626", lw=1.5, label=f"Incorrect (n={counts['incorrect']})")
        if erp_correct.size and erp_incorrect.size:
            axis.plot(ms, erp_correct[:, index] - erp_incorrect[:, index], color="#111827",
                      lw=0.9, ls="--", alpha=0.75, label="Różnica C−I")
        axis.axvline(0, color="#64748b", lw=0.8)
        axis.axhline(0, color="#cbd5e1", lw=0.8)
        axis.set_title(channel, loc="left", fontweight="bold")
        axis.grid(True, alpha=0.25)
        axis.set_ylabel("µV")
        axis.invert_yaxis()  # konwencja ERP: negatywność w górę
    for axis in axes_flat[len(channels):]:
        axis.set_visible(False)
    axes_flat[0].legend(loc="upper right", fontsize=8)
    for axis in axes[-1]:
        axis.set_xlabel("Czas od bodźca (ms)")
    figure.suptitle(title, fontsize=15, fontweight="bold")
    figure.tight_layout()
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def save_rt_png(path, trials: list[dict[str, str]], title: str) -> dict[str, Any]:
    """Wykres czasów reakcji (poprawne odpowiedzi) i poprawności dla warunków zgodny/niezgodny."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    conditions = ["congruent", "incongruent"]
    colors = {"congruent": "#2563eb", "incongruent": "#ea580c"}
    stats: dict[str, Any] = {}
    figure, (ax_rt, ax_acc) = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={"width_ratios": [2, 1]})
    for position, condition in enumerate(conditions):
        subset = [t for t in trials if t["condition"] == condition]
        rts = np.array([float(t["reaction_time_sec"]) * 1000 for t in subset
                        if t["result"] == "correct" and t["reaction_time_sec"]])
        accuracy = (sum(t["result"] == "correct" for t in subset) / len(subset) * 100) if subset else 0.0
        stats[condition] = {
            "trials": len(subset),
            "accuracy_pct": round(accuracy, 1),
            "rt_mean_ms": round(float(rts.mean()), 1) if rts.size else None,
            "rt_median_ms": round(float(np.median(rts)), 1) if rts.size else None,
            "timeouts": sum(t["result"] == "timeout" for t in subset),
        }
        if rts.size:
            jitter = np.random.default_rng(0).uniform(-0.12, 0.12, rts.size)
            ax_rt.scatter(position + jitter, rts, s=14, alpha=0.5, color=colors[condition])
            ax_rt.boxplot(rts, positions=[position], widths=0.4, showfliers=False)
        ax_acc.bar(position, accuracy, color=colors[condition], alpha=0.8)
        ax_acc.text(position, accuracy + 1, f"{accuracy:.0f}%", ha="center")
    for axis in (ax_rt, ax_acc):
        axis.set_xticks([0, 1], ["zgodne", "niezgodne"])
        axis.grid(axis="y", alpha=0.25)
    ax_rt.set_ylabel("Czas reakcji (ms)")
    ax_rt.set_title("Czasy reakcji (tylko poprawne)")
    ax_acc.set_ylim(0, 110)
    ax_acc.set_ylabel("Poprawność (%)")
    ax_acc.set_title("Poprawność")
    if stats["congruent"]["rt_mean_ms"] and stats["incongruent"]["rt_mean_ms"]:
        stats["flanker_effect_ms"] = round(stats["incongruent"]["rt_mean_ms"] - stats["congruent"]["rt_mean_ms"], 1)
        figure.text(0.5, -0.02, f"Efekt flankera (niezgodne − zgodne): {stats['flanker_effect_ms']} ms",
                    ha="center")
    figure.suptitle(title, fontsize=14, fontweight="bold")
    figure.tight_layout()
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)
    return stats


def analyze(paths, tmin: float = -0.2, tmax: float = 0.8, clean: bool = True,
            artifact_ptp: float = 300.0, artifact_peak: float = 250.0) -> dict[str, Any]:
    meta = paths.read_meta()
    trials = load_behavior(paths)
    title_id = f"{meta.get('pseudonym', '?')} · {paths.name}"

    # 1. Zachowanie: czasy reakcji i poprawność (nie wymaga EEG).
    rt_png = paths.output("_rt.png")
    behavior_stats = save_rt_png(rt_png, trials, f"Flanker — zachowanie ({title_id})")
    summary: dict[str, Any] = {
        "session": paths.name,
        "pseudonym": meta.get("pseudonym"),
        "behavior": behavior_stats,
        "outputs": {"rt_png": str(rt_png)},
    }

    if not paths.eeg.exists():
        summary["erp"] = "brak pliku EEG – pominięto analizę ERP"
        save_json(paths.output("_analysis.json"), summary)
        return summary

    # 2. Wczytanie EEG (czas LSL, ten sam zegar co triggery) i filtracja offline.
    eeg = load_eeg(paths)
    samples, timestamps, channels, fs = eeg["samples"], eeg["timestamps"], eeg["channels"], eeg["fs"]
    if clean:
        samples = filter_for_erp(samples, fs)

    # 3. Wycinanie epok wokół STIMULUS_ONSET dla prób correct/incorrect.
    time_axis = np.arange(tmin, tmax, 1.0 / fs)
    baseline_mask = time_axis < 0.0
    epochs: dict[str, list[np.ndarray]] = {"correct": [], "incorrect": []}
    rejected: list[dict[str, Any]] = []
    outside_recording = 0
    for trial in trials:
        if trial["result"] not in epochs:
            continue  # timeouty pomijamy
        onset = float(trial["stimulus_lsl_time"])
        relative = timestamps - onset
        if relative[0] > tmin or relative[-1] < tmax:
            outside_recording += 1
            continue
        epoch = np.column_stack([np.interp(time_axis, relative, samples[:, ch]) for ch in range(samples.shape[1])])
        if clean:
            # 4. Korekcja dryfu i automatyczne odrzucanie artefaktów.
            epoch = remove_baseline_drift(epoch, time_axis, baseline_mask)
            bad = artifact_channels(epoch, channels, artifact_ptp, artifact_peak)
            if bad:
                rejected.append({"trial": int(trial["trial"]), "result": trial["result"], "channels": bad})
                continue
        else:
            epoch = epoch - epoch[baseline_mask].mean(axis=0, keepdims=True)
        epochs[trial["result"]].append(epoch)

    # 5. Uśrednianie i zapis wyników.
    empty = np.empty((0, len(channels)))
    averages = {key: np.mean(values, axis=0) if values else empty for key, values in epochs.items()}
    counts = {key: len(values) for key, values in epochs.items()}
    erp_png = paths.output("_erp.png")
    erp_npz = paths.output("_erp.npz")
    np.savez_compressed(
        erp_npz, time=time_axis, channels=np.asarray(channels),
        erp_correct=averages["correct"], erp_incorrect=averages["incorrect"],
        erp_difference=averages["correct"] - averages["incorrect"] if all(counts.values()) else empty,
    )
    save_erp_png(erp_png, time_axis, channels, averages["correct"], averages["incorrect"], counts,
                 f"Flanker ERP — correct vs incorrect ({title_id})")
    summary["erp"] = {
        "sample_rate_hz": fs,
        "channels": channels,
        "epoch_s": [tmin, tmax],
        "epochs_used": counts,
        "epochs_rejected": len(rejected),
        "epochs_outside_recording": outside_recording,
        "rejected": rejected,
        "preprocessing": {
            "enabled": clean,
            "bandpass_hz": [0.5, 30.0] if clean else None,
            "notch_hz": [49.0, 51.0] if clean else None,
            "baseline": "linear_detrend" if clean else "mean",
            "artifact_ptp_uV": artifact_ptp if clean else None,
            "artifact_peak_uV": artifact_peak if clean else None,
        },
    }
    summary["outputs"].update({"erp_png": str(erp_png), "erp_npz": str(erp_npz)})
    save_json(paths.output("_analysis.json"), summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Analiza Flanker: ERP + czasy reakcji")
    parser.add_argument("--id", help="ID (pseudonim) – analizuje najnowszą sesję tego badanego")
    parser.add_argument("--session", help="Konkretny katalog sesji (nazwa lub ścieżka)")
    parser.add_argument("--tmin", type=float, default=-0.2)
    parser.add_argument("--tmax", type=float, default=0.8)
    parser.add_argument("--raw", action="store_true", help="Bez filtracji i odrzucania artefaktów")
    parser.add_argument("--artifact-ptp", type=float, default=300.0)
    parser.add_argument("--artifact-peak", type=float, default=250.0)
    args = parser.parse_args()
    try:
        paths = resolve_session(TASK_CODE, args.id, args.session)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Analizuję sesję: {paths.dir}")
    summary = analyze(paths, args.tmin, args.tmax, clean=not args.raw,
                      artifact_ptp=args.artifact_ptp, artifact_peak=args.artifact_peak)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
