"""Analiza sesji MUSIC: aktywność mózgu w ciszy vs przy muzyce.

    .venv/bin/python -m tasks.music.analysis --id osoba01
    .venv/bin/python -m tasks.music.analysis --session MUSIC_osoba01_20261030-183005

Każdy blok (bez pierwszych 2 s) dzielony jest na okna 2 s z krokiem 1 s. W każdym oknie
liczona jest moc pasm dla każdego kanału; kanał z artefaktem (> --artifact-ptp µV p-p albo płaski
sygnał przy nasyceniu przetwornika) jest w tym oknie pomijany. Warunki porównujemy medianą mocy z czystych okien.

Wyniki w katalogu sesji:
    *_topomap.png    mapy topograficzne (heatmapy głowy): cisza, muzyka i zmiana % w pasmach
    *_heatmap.png    kanał × pasmo: zmiana mocy muzyka vs cisza (%) i wielkość efektu d
    *_timecourse.png spektrogram całej sesji + przebieg mocy względnej α/β/θ z blokami
    *_indices.png    asymetria alfa czołowa, indeks zaangażowania, relaks vs alfa
    *_music.npz      dane numeryczne
    *_analysis.json  podsumowanie
"""

from __future__ import annotations

import argparse
import contextlib
import json
import warnings
from typing import Any

import numpy as np

from tasks.common import load_behavior, load_eeg, resolve_session, save_json

TASK_CODE = "MUSIC"
BANDS = {"delta": (1.0, 4.0), "theta": (4.0, 8.0), "alpha": (8.0, 13.0), "beta": (13.0, 30.0), "gamma": (30.0, 40.0)}
BAND_SYMBOLS = {"delta": "δ", "theta": "θ", "alpha": "α", "beta": "β", "gamma": "γ"}
CONDITIONS = ("silence", "music")
LABELS = {"silence": "cisza", "music": "muzyka"}
COLORS = {"silence": "#2563eb", "music": "#7c3aed"}

WINDOW_SECONDS = 2.0
STEP_SECONDS = 1.0
SKIP_SECONDS = 2.0
MIN_WINDOWS = 3  # minimum czystych okien na kanał i warunek, żeby pokazać wartość
FLAT_UV = 0.5     # odchylenie std poniżej tego progu (µV, po filtrze) = kanał płaski / nasycony

# Przybliżone położenia elektrod 10-20 w rzucie z góry (nos u góry, okrąg głowy r = 1
# przechodzi przez Fpz, T3, T4, Oz).
POSITIONS = {
    "Fp1": (-0.31, 0.95), "Fpz": (0.0, 1.0), "Fp2": (0.31, 0.95),
    "F7": (-0.81, 0.59), "F3": (-0.40, 0.50), "Fz": (0.0, 0.50), "F4": (0.40, 0.50), "F8": (0.81, 0.59),
    "T3": (-1.0, 0.0), "T7": (-1.0, 0.0), "C3": (-0.50, 0.0), "Cz": (0.0, 0.0), "C4": (0.50, 0.0),
    "T4": (1.0, 0.0), "T8": (1.0, 0.0),
    "T5": (-0.81, -0.59), "P7": (-0.81, -0.59), "P3": (-0.40, -0.50), "Pz": (0.0, -0.50),
    "P4": (0.40, -0.50), "T6": (0.81, -0.59), "P8": (0.81, -0.59),
    "O1": (-0.31, -0.95), "Oz": (0.0, -1.0), "O2": (0.31, -0.95),
}


# --------------------------------------------------------------------------
# Obliczenia
# --------------------------------------------------------------------------

def filter_eeg(samples: np.ndarray, fs: float) -> np.ndarray:
    """Pasmo 1–40 Hz + notch 49–51 Hz, filtracja dwukierunkowa."""
    from scipy import signal

    bandpass = signal.butter(4, [1.0, 40.0], btype="bandpass", fs=fs, output="sos")
    notch = signal.butter(2, [49.0, 51.0], btype="bandstop", fs=fs, output="sos")
    return signal.sosfiltfilt(notch, signal.sosfiltfilt(bandpass, samples, axis=0), axis=0)


def window_spectra(filtered: np.ndarray, timestamps: np.ndarray, fs: float, start: float, end: float,
                   artifact_ptp: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Okna 2 s z krokiem 1 s w przedziale [start, end].

    Zwraca: częstotliwości, widma [okna x f x kanały] (NaN dla kanałów z artefaktem),
    środki okien (czas LSL), maskę artefaktów [okna x kanały].
    """
    from scipy import signal

    size = int(round(WINDOW_SECONDS * fs))
    first = int(np.searchsorted(timestamps, start))
    last = int(np.searchsorted(timestamps, end))
    starts = range(first, last - size + 1, int(round(STEP_SECONDS * fs)))
    spectra, centers, bad = [], [], []
    frequencies = np.array([])
    for index in starts:
        segment = filtered[index:index + size]
        # Okno Welcha 1 s z 50% nakładaniem -> rozdzielczość 1 Hz, mniejsza wariancja.
        frequencies, spectrum = signal.welch(segment, fs=fs, axis=0, nperseg=int(fs))
        # Artefakt: za duża amplituda albo płaski sygnał (elektroda w nasyceniu przetwornika).
        is_bad = (np.ptp(segment, axis=0) > artifact_ptp) | (np.std(segment, axis=0) < FLAT_UV)
        spectrum[:, is_bad] = np.nan
        spectra.append(spectrum)
        bad.append(is_bad)
        centers.append(timestamps[index + size // 2])
    if not spectra:
        return frequencies, np.empty((0, 0, filtered.shape[1])), np.array([]), np.empty((0, filtered.shape[1]), bool)
    return frequencies, np.stack(spectra), np.asarray(centers), np.vstack(bad)


def band_powers(frequencies: np.ndarray, spectra: np.ndarray) -> np.ndarray:
    """Moc pasm w µV² -> [okna x pasma x kanały]."""
    result = []
    for low, high in BANDS.values():
        mask = (frequencies >= low) & (frequencies < high)
        result.append(np.trapezoid(spectra[:, mask], frequencies[mask], axis=1))
    return np.stack(result, axis=1)


@contextlib.contextmanager
def quiet():
    """Wycisza ostrzeżenia o pustych/NaN wycinkach (kanały odrzucone jako artefakt)."""
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


def cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    pooled = np.sqrt(((len(a) - 1) * a.var(ddof=1) + (len(b) - 1) * b.var(ddof=1)) / (len(a) + len(b) - 2))
    return float((a.mean() - b.mean()) / pooled) if pooled > 0 else float("nan")


def condition_medians(powers: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Mediana mocy z czystych okien warunku -> [pasma x kanały]; NaN gdy za mało okien."""
    selected = powers[mask]
    with quiet():
        median = np.nanmedian(selected, axis=0) if len(selected) else np.full(powers.shape[1:], np.nan)
    enough = np.sum(np.isfinite(selected[:, 0, :]), axis=0) >= MIN_WINDOWS if len(selected) else False
    return np.where(enough, median, np.nan)


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Wykresy
# --------------------------------------------------------------------------

def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def draw_topomap(axis, channels: list[str], values: np.ndarray, cmap: str, vmin: float, vmax: float):
    """Interpolowana mapa głowy (RBF thin-plate) na podstawie wartości w elektrodach."""
    from matplotlib.patches import Circle, Polygon
    from scipy.interpolate import RBFInterpolator

    known = [(c, v) for c, v in zip(channels, values) if c in POSITIONS and np.isfinite(v)]
    image = None
    if len(known) >= 3:
        points = np.array([POSITIONS[c] for c, _ in known])
        grid = np.linspace(-1.0, 1.0, 120)
        xx, yy = np.meshgrid(grid, grid)
        inside = xx ** 2 + yy ** 2 <= 1.0
        interpolator = RBFInterpolator(points, np.array([v for _, v in known]), kernel="thin_plate_spline",
                                       smoothing=1e-3)
        zz = np.full(xx.shape, np.nan)
        zz[inside] = interpolator(np.column_stack([xx[inside], yy[inside]]))
        image = axis.imshow(zz, extent=(-1, 1, -1, 1), origin="lower", cmap=cmap, vmin=vmin, vmax=vmax,
                            interpolation="bilinear")
        axis.contour(xx, yy, zz, levels=6, colors="k", linewidths=0.4, alpha=0.35)
    axis.add_patch(Circle((0, 0), 1.0, fill=False, lw=1.5, color="#1e293b"))
    axis.add_patch(Polygon([(-0.1, 0.99), (0, 1.12), (0.1, 0.99)], closed=False, fill=False, lw=1.5, color="#1e293b"))
    for side in (-1, 1):
        axis.add_patch(Polygon([(side * 0.99, 0.12), (side * 1.07, 0.08), (side * 1.07, -0.12), (side * 0.99, -0.16)],
                               closed=False, fill=False, lw=1.5, color="#1e293b"))
    for channel, value in zip(channels, values):
        if channel not in POSITIONS:
            continue
        x, y = POSITIONS[channel]
        clean = np.isfinite(value)
        axis.plot(x, y, "o" if clean else "x", ms=4, color="k" if clean else "#dc2626")
        axis.text(x, y - 0.11, channel, ha="center", va="top", fontsize=6.5, color="#0f172a")
    axis.set_xlim(-1.15, 1.15)
    axis.set_ylim(-1.2, 1.2)
    axis.set_aspect("equal")
    axis.axis("off")
    return image


def save_topomap_png(path, channels, medians, change_pct, title) -> None:
    plt = _pyplot()
    band_names = list(BANDS)
    figure, axes = plt.subplots(3, len(band_names), figsize=(3.1 * len(band_names), 9.6), squeeze=False)
    for col, band in enumerate(band_names):
        b = band_names.index(band)
        # Wiersze 1–2: log10 mocy, wspólna skala dla ciszy i muzyki w danym paśmie.
        logs = {c: np.log10(medians[c][b]) for c in CONDITIONS}
        finite = np.concatenate([v[np.isfinite(v)] for v in logs.values()])
        vmin, vmax = (finite.min(), finite.max()) if finite.size else (0, 1)
        for row, condition in enumerate(CONDITIONS):
            image = draw_topomap(axes[row, col], channels, logs[condition], "viridis", vmin, vmax)
            axes[row, col].set_title(f"{BAND_SYMBOLS[band]} {band} {BANDS[band][0]:g}–{BANDS[band][1]:g} Hz\n"
                                     f"{LABELS[condition]}" if row == 0 else LABELS[condition], fontsize=10)
            if image is not None and row == 1:
                bar = figure.colorbar(image, ax=axes[:2, col], shrink=0.6, pad=0.02)
                bar.set_label("log10 moc (µV²)", fontsize=8)
        # Wiersz 3: zmiana %, skala symetryczna wokół zera.
        values = change_pct[b]
        limit = np.nanmax(np.abs(values)) if np.isfinite(values).any() else 1.0
        image = draw_topomap(axes[2, col], channels, values, "RdBu_r", -limit, limit)
        axes[2, col].set_title("zmiana: muzyka vs cisza", fontsize=10)
        if image is not None:
            bar = figure.colorbar(image, ax=axes[2, col], shrink=0.8, pad=0.02)
            bar.set_label("%", fontsize=8)
    figure.suptitle(title, fontsize=14, fontweight="bold")
    figure.text(0.5, 0.005, "Mapy interpolowane z kilku elektrod – kształt między elektrodami jest orientacyjny. "
                "Czerwony × = za mało czystych okien.", ha="center", fontsize=8, color="#475569")
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def save_heatmap_png(path, channels, change_pct, effect_d, rel_change_pct, title) -> None:
    plt = _pyplot()
    band_labels = [f"{BAND_SYMBOLS[b]} {b}" for b in BANDS]
    figure, axes = plt.subplots(1, 2, figsize=(14, 0.55 * len(channels) + 3), constrained_layout=True)
    panels = [
        (change_pct, "Moc bezwzględna: zmiana % (muzyka vs cisza)\nw komórce: % i wielkość efektu d"),
        (rel_change_pct, "Moc względna (pasmo / 1–40 Hz): zmiana %\nmniej wrażliwa na kontakt elektrod"),
    ]
    for axis, (matrix, panel_title) in zip(axes, panels):
        data = matrix.T  # kanały w wierszach
        limit = np.nanmax(np.abs(data)) if np.isfinite(data).any() else 1.0
        image = axis.imshow(np.ma.masked_invalid(data), cmap="RdBu_r", vmin=-limit, vmax=limit, aspect="auto")
        axis.set_xticks(range(len(band_labels)), band_labels)
        axis.set_yticks(range(len(channels)), channels)
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                if not np.isfinite(data[i, j]):
                    axis.text(j, i, "brak", ha="center", va="center", fontsize=8, color="#94a3b8")
                    continue
                text = f"{data[i, j]:+.0f}%"
                if matrix is change_pct and np.isfinite(effect_d[j, i]):
                    text += f"\nd={effect_d[j, i]:+.2f}"
                strong = abs(data[i, j]) > 0.6 * limit
                axis.text(j, i, text, ha="center", va="center", fontsize=8, color="white" if strong else "#0f172a")
        axis.set_title(panel_title, fontsize=11)
        figure.colorbar(image, ax=axis, shrink=0.8).set_label("%")
    figure.suptitle(title, fontsize=14, fontweight="bold")
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def save_timecourse_png(path, spec_times, spec_freqs, spec_db, win_times, rel_power, blocks, t0, title) -> None:
    plt = _pyplot()
    figure, axes = plt.subplots(2, 1, figsize=(14, 8.5), sharex=True, constrained_layout=True,
                                gridspec_kw={"height_ratios": [1.3, 1]})
    # Spektrogram: dB względem mediany każdej częstotliwości z bloków.
    if spec_db.size:
        finite = spec_db[np.isfinite(spec_db)]
        limit = np.percentile(np.abs(finite), 95) if finite.size else 3.0
        mesh = axes[0].pcolormesh(spec_times - t0, spec_freqs, np.ma.masked_invalid(spec_db.T), cmap="RdBu_r",
                                  vmin=-limit, vmax=limit, shading="nearest")
        figure.colorbar(mesh, ax=axes[0], pad=0.01).set_label("dB vs mediana sesji")
    axes[0].set_ylabel("Częstotliwość (Hz)")
    axes[0].set_ylim(1, 40)
    axes[0].set_title("Spektrogram (średnia czystych kanałów); poza blokami = zakładanie/zdejmowanie słuchawek i oceny")

    for band, color in (("theta", "#f59e0b"), ("alpha", "#16a34a"), ("beta", "#dc2626")):
        series = rel_power[band]
        axes[1].plot(win_times - t0, series, color=color, lw=0.6, alpha=0.35)
        # Mediana krocząca 5 okien dla czytelności.
        smooth = np.array([np.nanmedian(series[max(0, i - 2):i + 3]) if np.isfinite(series[max(0, i - 2):i + 3]).any()
                           else np.nan for i in range(len(series))])
        axes[1].plot(win_times - t0, smooth, color=color, lw=1.8, label=f"{BAND_SYMBOLS[band]} {band}")
    axes[1].set_ylabel("Moc względna (pasmo / 1–40 Hz)")
    axes[1].set_xlabel("Czas od początku zapisu (s)")
    axes[1].grid(alpha=0.25)

    for block in blocks:
        start, end = float(block["start_lsl_time"]) - t0, float(block["end_lsl_time"]) - t0
        for axis in axes:
            axis.axvspan(start, end, color=COLORS[block["condition"]], alpha=0.10 if axis is axes[1] else 0.0)
            axis.axvline(start, color=COLORS[block["condition"]], lw=1.2)
            axis.axvline(end, color=COLORS[block["condition"]], lw=1.2, ls=":")
        axes[0].text((start + end) / 2, 39, LABELS[block["condition"]], ha="center", va="top", fontsize=9,
                     color="white", fontweight="bold",
                     bbox={"boxstyle": "round,pad=0.2", "fc": COLORS[block["condition"]], "ec": "none"})
    handles, labels = axes[1].get_legend_handles_labels()
    from matplotlib.patches import Patch
    handles += [Patch(color=COLORS[c], alpha=0.3) for c in CONDITIONS]
    labels += [f"blok: {LABELS[c]}" for c in CONDITIONS]
    axes[1].legend(handles, labels, loc="upper right", fontsize=8, ncol=5)
    figure.suptitle(title, fontsize=14, fontweight="bold")
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


def save_indices_png(path, block_stats, title) -> None:
    plt = _pyplot()
    figure, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    numbers = [b["block"] for b in block_stats]
    colors = [COLORS[b["condition"]] for b in block_stats]

    panels = [
        ("frontal_alpha_asymmetry", "Asymetria alfa czołowa ln(F4) − ln(F3)\n> 0: relatywnie większa aktywność lewej "
                                    "półkuli\n(wiązana z nastawieniem „zbliżającym”, pozytywnymi emocjami)"),
        ("engagement_index", "Indeks zaangażowania β / (α + θ)\nwyżej = większe pobudzenie / uwaga"),
    ]
    for axis, (key, panel_title) in zip(axes[:2], panels):
        values = [b[key] if b[key] is not None else np.nan for b in block_stats]
        axis.bar(numbers, values, color=colors)
        for condition in CONDITIONS:
            condition_values = [v for v, b in zip(values, block_stats) if b["condition"] == condition and np.isfinite(v)]
            if condition_values:
                axis.axhline(np.mean(condition_values), color=COLORS[condition], ls="--", lw=1.2,
                             label=f"średnia: {LABELS[condition]}")
        axis.axhline(0, color="#94a3b8", lw=0.8)
        axis.set_xticks(numbers)
        axis.set_xlabel("Blok")
        axis.set_title(panel_title, fontsize=10)
        axis.grid(axis="y", alpha=0.25)
        if axis.get_legend_handles_labels()[0]:
            axis.legend(fontsize=8)

    axis = axes[2]
    for b in block_stats:
        if b["relax_rating"] is None or b["alpha_relative"] is None:
            continue
        axis.scatter(b["relax_rating"], b["alpha_relative"], s=80, color=COLORS[b["condition"]], zorder=3)
        label = f"{b['block']}" + (f" (♥{b['liking_rating']:.0f})" if b["liking_rating"] is not None else "")
        axis.annotate(label, (b["relax_rating"], b["alpha_relative"]), textcoords="offset points",
                      xytext=(6, 4), fontsize=8)
    for condition in CONDITIONS:
        axis.scatter([], [], color=COLORS[condition], label=LABELS[condition])
    axis.set_xlim(0.5, 5.5)
    axis.set_xticks(range(1, 6))
    axis.set_xlabel("Ocena relaksu (1 = wcale, 5 = bardzo)")
    axis.set_ylabel("Moc względna alfa (średnia kanałów)")
    axis.set_title("Relaks deklarowany vs alfa\nnumer bloku, ♥ = ocena muzyki", fontsize=10)
    axis.grid(alpha=0.25)
    axis.legend(fontsize=8)
    figure.suptitle(title, fontsize=14, fontweight="bold")
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)


# --------------------------------------------------------------------------
# Analiza
# --------------------------------------------------------------------------

def analyze(paths, artifact_ptp: float = 200.0) -> dict[str, Any]:
    from scipy import stats

    meta = paths.read_meta()
    title_id = f"{meta.get('pseudonym', '?')} · {paths.name}"

    # 1. EEG i granice bloków (oba w czasie LSL – bez dodatkowej synchronizacji).
    eeg = load_eeg(paths)
    samples, timestamps, channels, fs = eeg["samples"], eeg["timestamps"], eeg["channels"], eeg["fs"]
    blocks = [row for row in load_behavior(paths) if row["condition"] in CONDITIONS]
    filtered = filter_eeg(samples, fs)
    band_names = list(BANDS)

    # 2. Okna 2 s w każdym bloku (pomijamy pierwsze 2 s bloku).
    frequencies = np.array([])
    all_powers, all_rel, all_conditions, all_blocks, all_times, all_bad = [], [], [], [], [], []
    for block in blocks:
        frequencies, spectra, centers, bad = window_spectra(
            filtered, timestamps, fs, float(block["start_lsl_time"]) + SKIP_SECONDS,
            float(block["end_lsl_time"]), artifact_ptp)
        if not len(centers):
            continue
        powers = band_powers(frequencies, spectra)
        total = powers.sum(axis=1, keepdims=True)
        all_powers.append(powers)
        all_rel.append(powers / total)
        all_conditions += [block["condition"]] * len(centers)
        all_blocks += [int(block["block"])] * len(centers)
        all_times.append(centers)
        all_bad.append(bad)
    if not all_powers:
        raise ValueError("Żaden blok nie ma ≥ 2 s danych EEG – sprawdź, czy zapis EEG obejmował zadanie.")
    powers = np.concatenate(all_powers)        # [okna x pasma x kanały]
    relative = np.concatenate(all_rel)
    conditions = np.asarray(all_conditions)
    block_ids = np.asarray(all_blocks)
    win_times = np.concatenate(all_times)
    bad = np.vstack(all_bad)

    # 3. Porównanie warunków: mediany, zmiana %, wielkość efektu na log10 mocy.
    masks = {c: conditions == c for c in CONDITIONS}
    medians = {c: condition_medians(powers, masks[c]) for c in CONDITIONS}
    rel_medians = {c: condition_medians(relative, masks[c]) for c in CONDITIONS}
    with quiet():
        change_pct = (medians["music"] / medians["silence"] - 1.0) * 100.0
        rel_change_pct = (rel_medians["music"] / rel_medians["silence"] - 1.0) * 100.0
        log_powers = np.log10(powers)
    effect_d = np.full(change_pct.shape, np.nan)
    p_values = np.full(change_pct.shape, np.nan)
    for b in range(len(band_names)):
        for ch in range(len(channels)):
            music, silence = log_powers[masks["music"], b, ch], log_powers[masks["silence"], b, ch]
            if np.isfinite(change_pct[b, ch]):
                effect_d[b, ch] = cohens_d(music, silence)
                p_values[b, ch] = stats.mannwhitneyu(music[np.isfinite(music)], silence[np.isfinite(silence)]).pvalue

    # 4. Wskaźniki na blok: asymetria alfa, zaangażowanie, alfa względna + oceny.
    alpha, beta, theta = (band_names.index(name) for name in ("alpha", "beta", "theta"))
    block_stats = []
    for block in blocks:
        selected = block_ids == int(block["block"])
        with quiet():
            block_median = np.nanmedian(powers[selected], axis=0) if selected.any() else None
            rel_median = np.nanmedian(relative[selected], axis=0) if selected.any() else None
        faa = engagement = alpha_rel = None
        if block_median is not None:
            if "F3" in channels and "F4" in channels:
                value = np.log(block_median[alpha, channels.index("F4")]) - np.log(block_median[alpha, channels.index("F3")])
                faa = float(value) if np.isfinite(value) else None
            with quiet():
                value = np.nanmean(block_median[beta] / (block_median[alpha] + block_median[theta]))
                engagement = float(value) if np.isfinite(value) else None
                value = np.nanmean(rel_median[alpha])
                alpha_rel = float(value) if np.isfinite(value) else None
        block_stats.append({
            "block": int(block["block"]),
            "condition": block["condition"],
            "windows": int(selected.sum()),
            "frontal_alpha_asymmetry": faa,
            "engagement_index": engagement,
            "alpha_relative": alpha_rel,
            "relax_rating": to_float(block.get("relax_rating")),
            "liking_rating": to_float(block.get("liking_rating")),
        })

    # 5. Spektrogram całej sesji (także przejścia między blokami).
    session_start = float(blocks[0]["start_lsl_time"]) - 15.0
    session_end = float(blocks[-1]["end_lsl_time"]) + 5.0
    spec_freqs, spec, spec_times, _ = window_spectra(filtered, timestamps, fs, session_start, session_end, artifact_ptp)
    spec_db = np.empty((0, 0))
    if spec.size:
        in_blocks = np.zeros(len(spec_times), bool)
        for block in blocks:
            in_blocks |= (spec_times >= float(block["start_lsl_time"])) & (spec_times <= float(block["end_lsl_time"]))
        with quiet():
            mean_spectrum = np.nanmean(spec, axis=2)  # średnia czystych kanałów
            # Punkt odniesienia: mediana z bloków (przejścia ze słuchawkami nie zawyżają skali).
            reference = np.nanmedian(mean_spectrum[in_blocks] if in_blocks.any() else mean_spectrum, axis=0)
            spec_db = 10 * np.log10(mean_spectrum / reference)
    with quiet():
        rel_series = {band: np.nanmean(relative[:, band_names.index(band)], axis=1) for band in ("theta", "alpha", "beta")}

    # 6. Wykresy PNG i dane.
    outputs = {}
    outputs["topomap_png"] = str(paths.output("_topomap.png"))
    save_topomap_png(outputs["topomap_png"], channels, medians, change_pct,
                     f"MUSIC — mapy aktywności: cisza vs muzyka ({title_id})")
    outputs["heatmap_png"] = str(paths.output("_heatmap.png"))
    save_heatmap_png(outputs["heatmap_png"], channels, change_pct, effect_d, rel_change_pct,
                     f"MUSIC — zmiana mocy pasm przy muzyce ({title_id})")
    outputs["timecourse_png"] = str(paths.output("_timecourse.png"))
    save_timecourse_png(outputs["timecourse_png"], spec_times, spec_freqs, spec_db, win_times, rel_series, blocks,
                        timestamps[0], f"MUSIC — przebieg sesji ({title_id})")
    outputs["indices_png"] = str(paths.output("_indices.png"))
    save_indices_png(outputs["indices_png"], block_stats, f"MUSIC — wskaźniki w blokach ({title_id})")
    npz = paths.output("_music.npz")
    np.savez_compressed(
        npz, channels=np.asarray(channels), bands=np.asarray(band_names), window_times=win_times,
        window_conditions=conditions, window_blocks=block_ids, window_band_power=powers,
        window_relative_power=relative, window_artifact=bad,
        median_silence=medians["silence"], median_music=medians["music"],
        change_pct=change_pct, relative_change_pct=rel_change_pct, cohens_d=effect_d, p_mannwhitney=p_values,
    )
    outputs["npz"] = str(npz)

    def table(matrix: np.ndarray, digits: int) -> dict[str, dict[str, float | None]]:
        return {band: {ch: (round(float(matrix[b, c]), digits) if np.isfinite(matrix[b, c]) else None)
                       for c, ch in enumerate(channels)} for b, band in enumerate(band_names)}

    windows_total = {c: int(masks[c].sum()) for c in CONDITIONS}
    clean_pct = {ch: round(100.0 * float(1 - bad[:, i].mean()), 1) for i, ch in enumerate(channels)}
    summary = {
        "session": paths.name,
        "pseudonym": meta.get("pseudonym"),
        "sample_rate_hz": fs,
        "channels": channels,
        "blocks": len(blocks),
        "windows": windows_total,
        "clean_windows_pct": clean_pct,
        "bands_hz": BANDS,
        "artifact_ptp_uV": artifact_ptp,
        "median_power_uV2": {c: table(medians[c], 3) for c in CONDITIONS},
        "change_pct_music_vs_silence": table(change_pct, 1),
        "relative_change_pct_music_vs_silence": table(rel_change_pct, 1),
        "cohens_d_log_power": table(effect_d, 2),
        "p_mannwhitney_uncorrected": table(p_values, 4),
        "blocks_detail": block_stats,
        "note": "Okna 2 s nie są niezależne – p i d traktuj orientacyjnie (jedna osoba, kilka bloków).",
        "outputs": outputs,
    }
    if min(clean_pct.values()) < 50:
        summary["warning"] = ("Część kanałów ma < 50% czystych okien – sprawdź kontakt elektrod: "
                              + ", ".join(ch for ch, pct in clean_pct.items() if pct < 50))
    save_json(paths.output("_analysis.json"), summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Analiza MUSIC: cisza vs muzyka")
    parser.add_argument("--id", help="ID (pseudonim) – analizuje najnowszą sesję tego badanego")
    parser.add_argument("--session", help="Konkretny katalog sesji (nazwa lub ścieżka)")
    parser.add_argument("--artifact-ptp", type=float, default=200.0,
                        help="Próg artefaktu p-p w oknie 2 s (µV, po filtrze 1–40 Hz)")
    args = parser.parse_args()
    try:
        paths = resolve_session(TASK_CODE, args.id, args.session)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Analizuję sesję: {paths.dir}")
    print(json.dumps(analyze(paths, args.artifact_ptp), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
