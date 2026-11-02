"""
BrainAccess EEG DSP & Feature Extraction Pipeline.
Provides real-time DC removal, 50 Hz notch filtering, band power calculation, and artifact detection.
Universal: channel list is configurable — supports HALO (4ch) and MINI (8ch).
"""

import os
import time
from typing import Any, Dict, List, Optional, Tuple
import warnings
import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import butter, find_peaks, iirnotch, sosfilt, sosfilt_zi, tf2sos, welch

from .constants import BANDS, CHANNELS, SAMPLE_RATE

# Lazy / safe import of FOOOF
warnings.filterwarnings("ignore", category=DeprecationWarning, module="fooof.*")
try:
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib_cache")
    from fooof import FOOOF
    FOOOF_AVAILABLE = True
except Exception:
    FOOOF = None
    FOOOF_AVAILABLE = False

class HaloDSP:
    """
    Real-time signal processing engine for BrainAccess EEG.
    Supports any number of channels — configured via `channels` parameter.
    """

    def __init__(
        self,
        buffer_seconds: float = 4.0,
        window_seconds: float = 1.0,
        filters_enabled: bool = True,
        highpass_hz: float = 0.5,
        lowpass_hz: float = 45.0,
        notch_enabled: bool = True,
        channels: Optional[List[str]] = None,
    ):
        self.channels = list(channels) if channels else list(CHANNELS)
        self.sample_rate = SAMPLE_RATE
        self.buffer_size = int(buffer_seconds * SAMPLE_RATE)
        self.window_size = int(window_seconds * SAMPLE_RATE)
        self.filters_enabled = filters_enabled
        self.highpass_hz = highpass_hz
        self.lowpass_hz = lowpass_hz
        self.notch_enabled = notch_enabled

        # Ring buffers for each channel
        self.raw_buffers: Dict[str, np.ndarray] = {ch: np.zeros(0, dtype=np.float64) for ch in self.channels}
        self.filtered_buffers: Dict[str, np.ndarray] = {ch: np.zeros(0, dtype=np.float64) for ch in self.channels}

        # Cache for FOOOF parameterization to avoid CPU overload during 10 Hz websocket pushes
        self._fooof_cache: Optional[Dict[str, Any]] = None
        self._fooof_last_time: float = 0.0

        self._build_filters()

    def set_channels(self, channels: List[str]):
        """Reconfigure DSP for a different set of channels (e.g. after device detection)."""
        self.channels = list(channels)
        self.raw_buffers = {ch: np.zeros(0, dtype=np.float64) for ch in self.channels}
        self.filtered_buffers = {ch: np.zeros(0, dtype=np.float64) for ch in self.channels}
        self._build_filters()

    def _build_filters(self):
        """Build preview filters from the current settings."""
        # DC / slow drift removal: High-pass exponential moving average filter
        # tau = 0.5 s gives cutoff ≈ 0.3 Hz
        tau = 1.0 / (2.0 * np.pi * max(self.highpass_hz, 0.01))
        self._hp_alpha: float = float(np.exp(-1.0 / (SAMPLE_RATE * tau)))
        self._hp_state: Dict[str, Optional[float]] = {ch: None for ch in self.channels}

        # 50 Hz Notch filter (EU mains)
        b_notch, a_notch = iirnotch(50.0, Q=30.0, fs=SAMPLE_RATE)
        self._notch_sos = tf2sos(b_notch, a_notch)
        self._notch_zi: Dict[str, Optional[np.ndarray]] = {ch: None for ch in self.channels}

        # 0.5 - 45 Hz Bandpass filter
        self._bandpass_sos = butter(
            4,
            [max(self.highpass_hz, 0.01), min(self.lowpass_hz, SAMPLE_RATE / 2 - 1)],
            btype="bandpass",
            fs=SAMPLE_RATE,
            output="sos",
        )
        self._bandpass_zi: Dict[str, Optional[np.ndarray]] = {ch: None for ch in self.channels}

        # Mains hum (50 Hz) buffer for impedance/noise estimation
        self.mains_buffers: Dict[str, np.ndarray] = {ch: np.zeros(0, dtype=np.float64) for ch in self.channels}

    def configure_filters(
        self,
        *,
        enabled: bool,
        highpass_hz: float,
        lowpass_hz: float,
        notch_enabled: bool,
    ):
        """Update preview filtering and reset state to avoid mixed filter history."""
        if not 0.0 <= highpass_hz < lowpass_hz < SAMPLE_RATE / 2:
            raise ValueError("Filter limits must satisfy 0 <= highpass < lowpass < Nyquist.")
        self.filters_enabled = enabled
        self.highpass_hz = highpass_hz
        self.lowpass_hz = lowpass_hz
        self.notch_enabled = notch_enabled
        self._build_filters()
        self.reset()

    def reset(self):
        """Reset internal filter states and buffers."""
        for ch in self.channels:
            self.raw_buffers[ch] = np.zeros(0, dtype=np.float64)
            self.filtered_buffers[ch] = np.zeros(0, dtype=np.float64)
            self.mains_buffers[ch] = np.zeros(0, dtype=np.float64)
            self._hp_state[ch] = None
            self._notch_zi[ch] = None
            self._bandpass_zi[ch] = None
        self._fooof_cache = None
        self._fooof_last_time = 0.0

    def push_sample(self, ch_values: Dict[str, float]):
        """Push a single multichannel sample into DSP pipeline."""
        for ch in self.channels:
            val = ch_values.get(ch, 0.0)
            self.push_chunk(ch, np.array([val], dtype=np.float64))

    def push_chunk(self, ch: str, chunk: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Filter a chunk of samples for a specific channel.
        Returns (filtered_chunk, mains_50hz_chunk).
        """
        if len(chunk) == 0:
            return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)

        # Ensure channel buffers exist (handles dynamic channel addition)
        if ch not in self.raw_buffers:
            self.raw_buffers[ch] = np.zeros(0, dtype=np.float64)
            self.filtered_buffers[ch] = np.zeros(0, dtype=np.float64)
            self.mains_buffers[ch] = np.zeros(0, dtype=np.float64)
            self._hp_state[ch] = None
            self._notch_zi[ch] = None
            self._bandpass_zi[ch] = None

        # 1. Update raw buffer
        self.raw_buffers[ch] = np.concatenate([self.raw_buffers[ch], chunk])
        if len(self.raw_buffers[ch]) > self.buffer_size:
            self.raw_buffers[ch] = self.raw_buffers[ch][-self.buffer_size:]

        if not self.filters_enabled:
            self.filtered_buffers[ch] = self.raw_buffers[ch].copy()
            return chunk.copy(), np.zeros_like(chunk)

        # 2. Step 1: High-pass filter (remove DC baseline / electrode polarization)
        if self._hp_state[ch] is None:
            self._hp_state[ch] = float(np.median(chunk))

        lp = self._hp_state[ch]
        a = self._hp_alpha
        hp_chunk = np.empty_like(chunk)
        for i, x in enumerate(chunk):
            lp = a * lp + (1.0 - a) * x
            hp_chunk[i] = x - lp
        self._hp_state[ch] = lp

        # 3. Step 2: 50 Hz Notch filter (Europe/Poland mains line hum)
        if self._notch_zi[ch] is None:
            self._notch_zi[ch] = sosfilt_zi(self._notch_sos) * hp_chunk[0]
        if self.notch_enabled:
            notched, self._notch_zi[ch] = sosfilt(self._notch_sos, hp_chunk, zi=self._notch_zi[ch])
        else:
            notched = hp_chunk

        # Isolate 50 Hz mains hum component
        mains_chunk = hp_chunk - notched
        self.mains_buffers[ch] = np.concatenate([self.mains_buffers[ch], mains_chunk])
        if len(self.mains_buffers[ch]) > self.buffer_size:
            self.mains_buffers[ch] = self.mains_buffers[ch][-self.buffer_size:]

        # 4. Step 3: 0.5 - 45 Hz Bandpass filter (true physiological EEG band)
        if self._bandpass_zi[ch] is None:
            self._bandpass_zi[ch] = sosfilt_zi(self._bandpass_sos) * notched[0]
        filtered, self._bandpass_zi[ch] = sosfilt(self._bandpass_sos, notched, zi=self._bandpass_zi[ch])

        # 5. Append to filtered buffer
        self.filtered_buffers[ch] = np.concatenate([self.filtered_buffers[ch], filtered])
        if len(self.filtered_buffers[ch]) > self.buffer_size:
            self.filtered_buffers[ch] = self.filtered_buffers[ch][-self.buffer_size:]

        return filtered, mains_chunk

    def has_enough_data(self) -> bool:
        """Check if all channels have at least 1 full FFT window."""
        return all(len(self.filtered_buffers[ch]) >= self.window_size for ch in self.channels)

    def get_band_powers(self) -> Dict[str, Dict[str, float]]:
        """
        Compute Welch Power Spectral Density (PSD) band powers in µV² for each channel.
        """
        results: Dict[str, Dict[str, float]] = {}
        for ch in self.channels:
            buf = self.filtered_buffers[ch]
            if len(buf) < self.window_size:
                results[ch] = {band: 0.0 for band in BANDS}
                continue

            window = buf[-self.window_size:]
            nperseg = min(128, len(window))
            freqs, psd = welch(window, fs=SAMPLE_RATE, nperseg=nperseg)

            ch_bands = {}
            for band_name, (f_low, f_high) in BANDS.items():
                mask = (freqs >= f_low) & (freqs <= f_high)
                power = float(np.mean(psd[mask])) if np.any(mask) else 0.0
                ch_bands[band_name] = max(0.0, power)
            results[ch] = ch_bands

        return results

    def get_psd(
        self,
        max_frequency: float = 100.0,
        use_filtered: bool = True,
        min_seconds: float = 2.0,
    ) -> Tuple[List[float], List[float]]:
        """
        Return the channel-median Welch PSD (µV²/Hz) up to ``max_frequency``.

        Compared with a single 1 s periodogram of the raw signal this is much
        smoother and more stable between frames:
          * the whole buffer (>= ``min_seconds``, ideally 4 s) is used, split into
            1 s Hann segments with 50 % overlap -> 3-7 averaged segments,
          * linear detrend removes DC and slow drift that otherwise leak into
            the low-frequency bins,
          * the filtered stream is used when the preview filters are on, so the
            50 Hz hum and electrode polarisation do not dominate the plot,
          * the median over channels keeps one badly attached electrode from
            dragging the whole curve up.
        """
        spectra = []
        frequencies = None
        use_filtered = use_filtered and self.filters_enabled
        if use_filtered:
            # above the preview low-pass the filtered spectrum is just roll-off
            max_frequency = min(max_frequency, self.lowpass_hz)

        nperseg = int(self.sample_rate)  # 1 s segments -> 1 Hz resolution
        for ch in self.channels:
            source = self.filtered_buffers[ch] if use_filtered else self.raw_buffers[ch]
            if len(source) < int(min_seconds * self.sample_rate):
                continue

            freqs, psd = welch(
                source,
                fs=self.sample_rate,
                window="hann",
                nperseg=min(nperseg, len(source)),
                noverlap=min(nperseg, len(source)) // 2,
                detrend="linear",
            )
            mask = freqs <= max_frequency
            if frequencies is None:
                frequencies = freqs[mask]
            spectra.append(np.nan_to_num(psd[mask], nan=0.0, posinf=0.0, neginf=0.0))

        if frequencies is None or not spectra:
            return [], []

        median_psd = np.median(np.asarray(spectra), axis=0)
        return frequencies.tolist(), np.maximum(median_psd, 0.0).tolist()

    def get_fooof(
        self,
        max_frequency: float = 45.0,
        use_filtered: bool = True,
        min_seconds: float = 2.0,
        cache_ttl_seconds: float = 1.0,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Parametrize neural power spectra using FOOOF (Fitting Oscillations & One-Over-F).
        Fits FOOOF model individually per channel and aggregates via channel median.

        Returns:
            Dict with:
                - frequencies: list of frequency values (Hz)
                - psd: regular median PSD (µV²/Hz)
                - psd_flat: flattened median PSD (PSD - aperiodic background) (µV²/Hz)
                - model_info: {
                    "available": bool,
                    "fitted": bool,
                    "r_squared": float | None,
                    "error": float | None,
                    "n_peaks": int,
                    "channels_fitted": int,
                    "details": str,
                }
        """
        now = time.monotonic()
        if not force and self._fooof_cache is not None:
            if (now - self._fooof_last_time) < max(0.05, cache_ttl_seconds):
                return self._fooof_cache

        # Check library availability
        if not FOOOF_AVAILABLE or FOOOF is None:
            res = {
                "frequencies": [],
                "psd": [],
                "psd_flat": [],
                "model_info": {
                    "available": False,
                    "fitted": False,
                    "r_squared": None,
                    "error": None,
                    "n_peaks": 0,
                    "channels_fitted": 0,
                    "details": "Brak zainstalowanej biblioteki fooof",
                },
            }
            self._fooof_cache = res
            self._fooof_last_time = now
            return res

        use_filtered = use_filtered and self.filters_enabled
        min_samples = int(min_seconds * self.sample_rate)

        # Check data availability
        active_channels = [
            ch for ch in self.channels
            if len(self.filtered_buffers[ch] if use_filtered else self.raw_buffers[ch]) >= min_samples
        ]
        if not active_channels:
            res = {
                "frequencies": [],
                "psd": [],
                "psd_flat": [],
                "model_info": {
                    "available": True,
                    "fitted": False,
                    "r_squared": None,
                    "error": None,
                    "n_peaks": 0,
                    "channels_fitted": 0,
                    "details": f"Zbyt mało danych do parametryzacji (< {min_seconds:.1f} s)",
                },
            }
            self._fooof_cache = res
            self._fooof_last_time = now
            return res

        f_min = max(1.0, self.highpass_hz if use_filtered else 1.0)
        f_max = min(max_frequency, self.lowpass_hz if use_filtered else max_frequency, 45.0)
        if f_min >= f_max:
            f_min, f_max = 1.0, min(max_frequency, 45.0)

        channel_psds: List[np.ndarray] = []
        channel_flats: List[np.ndarray] = []
        channel_r2: List[float] = []
        channel_errors: List[float] = []
        channel_npeaks: List[int] = []
        fitted_freqs: Optional[np.ndarray] = None

        nperseg = int(self.sample_rate)

        for ch in active_channels:
            source = self.filtered_buffers[ch] if use_filtered else self.raw_buffers[ch]
            freqs, psd = welch(
                source,
                fs=self.sample_rate,
                window="hann",
                nperseg=min(nperseg, len(source)),
                noverlap=min(nperseg, len(source)) // 2,
                detrend="linear",
            )
            mask = (freqs >= f_min) & (freqs <= f_max)
            f_band = freqs[mask]
            psd_band = np.nan_to_num(psd[mask], nan=1e-12, posinf=1e-12, neginf=1e-12)
            psd_band = np.maximum(psd_band, 1e-12)

            if len(f_band) < 4:
                continue

            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore")
                    fm = FOOOF(
                        peak_width_limits=[1.0, 8.0],
                        max_n_peaks=6,
                        min_peak_height=0.05,
                        verbose=False,
                    )
                    fm.fit(f_band, psd_band, freq_range=[float(f_band[0]), float(f_band[-1])])
                    if not fm.has_model:
                        continue

                    if fitted_freqs is None:
                        fitted_freqs = fm.freqs

                    # fm.power_spectrum is log10(PSD), fm._ap_fit is log10(aperiodic)
                    psd_ch = 10.0 ** fm.power_spectrum
                    ap_ch = 10.0 ** fm._ap_fit
                    # Flattened PSD: PSD - aperiodic background
                    flat_ch = np.maximum(0.0, psd_ch - ap_ch)

                    channel_psds.append(psd_ch)
                    channel_flats.append(flat_ch)
                    channel_r2.append(float(fm.r_squared_))
                    channel_errors.append(float(fm.error_))
                    channel_npeaks.append(int(fm.n_peaks_))
            except Exception:
                continue

        if not channel_psds or fitted_freqs is None:
            res = {
                "frequencies": [],
                "psd": [],
                "psd_flat": [],
                "model_info": {
                    "available": True,
                    "fitted": False,
                    "r_squared": None,
                    "error": None,
                    "n_peaks": 0,
                    "channels_fitted": 0,
                    "details": "Nie udało się dopasować modelu FOOOF",
                },
            }
            self._fooof_cache = res
            self._fooof_last_time = now
            return res

        median_psd = np.median(np.asarray(channel_psds), axis=0)
        median_flat = np.median(np.asarray(channel_flats), axis=0)
        median_r2 = float(np.median(channel_r2))
        median_error = float(np.median(channel_errors))
        median_npeaks = int(round(float(np.median(channel_npeaks))))

        res = {
            "frequencies": fitted_freqs.tolist(),
            "psd": np.maximum(median_psd, 0.0).tolist(),
            "psd_flat": np.maximum(median_flat, 0.0).tolist(),
            "model_info": {
                "available": True,
                "fitted": True,
                "r_squared": round(median_r2, 4),
                "error": round(median_error, 4),
                "n_peaks": median_npeaks,
                "channels_fitted": len(channel_psds),
                "details": f"Dopasowano ({len(channel_psds)}/{len(self.channels)} kan., R²={median_r2:.2f})",
            },
        }
        self._fooof_cache = res
        self._fooof_last_time = now
        return res

    get_fooof_psd = get_fooof

    def detect_eye_blinks(self) -> int:
        """
        Detect blink artifact count in the most recent 1 second.

        MINI montages often expose F7/FC5 instead of Fp1/Fp2, so use all
        frontal positions that are present and merge peaks occurring at the
        same time across channels into one blink. Detection is based on the
        local median/MAD rather than zero; electrode offsets and slow drift
        must not hide a blink.
        """
        frontal_candidates = {
            "Fp1", "Fp2", "Fpz", "AF7", "AF3", "AFz", "AF4", "AF8",
            "F7", "F5", "F3", "F1", "Fz", "F2", "F4", "F6", "F8",
            "FT7", "FC5", "FC3", "FC1", "FCz", "FC2", "FC4", "FC6", "FT8",
            "EEG1", "EEG2",
        }
        event_positions = []
        for ch in self.channels:
            if ch not in frontal_candidates:
                continue
            if ch not in self.filtered_buffers:
                continue
            buf = self.raw_buffers[ch]
            if len(buf) < self.window_size:
                continue
            window = buf[-self.window_size:]
            # Use a short local median baseline on raw data. The filtered
            # stream can contain startup transients and a blink itself is
            # intentionally suppressed by the EEG band-pass.
            baseline = median_filter(window, size=51, mode="nearest")
            centered = window - baseline
            mad = 1.4826 * np.median(np.abs(centered))
            if mad < 0.5:
                continue
            threshold = max(25.0, 3.0 * mad)
            peaks, _ = find_peaks(
                np.abs(centered),
                height=threshold,
                prominence=max(10.0, 1.5 * mad),
                distance=max(25, self.sample_rate // 4),
            )
            event_positions.extend(int(p) for p in peaks)

        # One blink appears on several frontal channels. Cluster detections
        # within 120 ms so the UI reports one event, not one per channel.
        event_positions.sort()
        merged = []
        for position in event_positions:
            if not merged or position - merged[-1] > int(self.sample_rate * 0.12):
                merged.append(position)
        return len(merged)

    def get_signal_quality(self) -> Dict[str, Dict]:
        """
        Evaluate realistic EEG signal quality per channel using multi-metric analysis:
          - eeg_rms: clean EEG amplitude in µV (ideal: 5 - 25 µV)
          - mains_50hz: 50 Hz power-line interference in µV (ideal: < 3 µV)
          - snr_db: Signal-to-Noise Ratio in dB
          - crest_factor: Peak/RMS ratio (high = spiky artifacts)
          - score_pct: 0 - 100% composite quality index
          - status: descriptive status in Polish
          - advice: actionable tip for the user
        """
        quality = {}
        for ch in self.channels:
            buf_filt = self.filtered_buffers[ch]
            buf_mains = self.mains_buffers[ch]
            buf_raw = self.raw_buffers[ch]

            if len(buf_filt) < self.sample_rate:
                quality[ch] = {
                    "eeg_rms": 0.0,
                    "mains_50hz": 0.0,
                    "snr_db": 0.0,
                    "crest_factor": 0.0,
                    "score_pct": 0,
                    "status": "Inicjalizacja...",
                    "status_code": "init",
                    "advice": "Zbieranie próbek...",
                }
                continue

            win_filt = buf_filt[-self.sample_rate:]
            win_mains = buf_mains[-self.sample_rate:] if len(buf_mains) >= self.sample_rate else np.zeros_like(win_filt)
            win_raw = buf_raw[-self.sample_rate:] if len(buf_raw) >= self.sample_rate else win_filt

            eeg_rms = float(np.sqrt(np.mean(win_filt ** 2)))
            mains_rms = float(np.sqrt(np.mean(win_mains ** 2)))

            # --- Metric 1: Crest Factor (Peak-to-RMS ratio) ---
            # Clean EEG: ~3-5, Spiky artifacts/noise: >6
            peak_val = float(np.max(np.abs(win_filt))) if eeg_rms > 0.1 else 0.0
            crest_factor = peak_val / eeg_rms if eeg_rms > 0.1 else 0.0

            # --- Metric 2: SNR (EEG power vs mains noise power) ---
            eeg_power = eeg_rms ** 2
            mains_power = mains_rms ** 2
            if mains_power > 0.01 and eeg_power > 0.01:
                snr_db = float(10.0 * np.log10(eeg_power / mains_power))
            else:
                snr_db = 40.0 if eeg_rms > 1.0 else 0.0

            # --- Metric 3: Spectral Flatness (Wiener entropy) ---
            # Flat spectrum = broadband noise, peaked spectrum = real EEG rhythms
            # Values: 0 = pure tone, 1 = white noise. Good EEG: 0.2-0.6
            try:
                nperseg_q = min(128, len(win_filt))
                freqs_q, psd_q = welch(win_filt, fs=SAMPLE_RATE, nperseg=nperseg_q)
                # Focus on 1-45 Hz range
                mask_q = (freqs_q >= 1.0) & (freqs_q <= 45.0)
                psd_band = psd_q[mask_q]
                if len(psd_band) > 2 and np.all(psd_band > 0):
                    geo_mean = np.exp(np.mean(np.log(psd_band)))
                    arith_mean = np.mean(psd_band)
                    spectral_flatness = float(geo_mean / arith_mean) if arith_mean > 0 else 1.0
                else:
                    spectral_flatness = 1.0
            except Exception:
                spectral_flatness = 0.5

            # --- Composite Quality Scoring ---
            score = 100.0

            # 1. Flatline / Disconnected check
            if eeg_rms < 1.0:
                score = 0.0
                status = "Brak sygnału (Odłączona)"
                status_code = "flatline"
                advice = "Sprawdź czy opaska dotyka skóry."
            else:
                # 2. 50 Hz mains hum penalty (strict: clean dry electrode should be <5 µV)
                if mains_rms > 3.0:
                    penalty_50hz = min(45.0, (mains_rms - 3.0) * 2.5)
                    score -= penalty_50hz

                # 3. Excessive amplitude penalty (clean EEG RMS ~5-25 µV)
                if eeg_rms > 25.0:
                    penalty_amplitude = min(30.0, (eeg_rms - 25.0) * 1.5)
                    score -= penalty_amplitude

                # 4. Crest factor penalty (spiky signal = artifact)
                if crest_factor > 5.0:
                    penalty_crest = min(20.0, (crest_factor - 5.0) * 5.0)
                    score -= penalty_crest

                # 5. Spectral flatness penalty (>0.7 = too flat = noise-dominated)
                if spectral_flatness > 0.65:
                    penalty_flat = min(25.0, (spectral_flatness - 0.65) * 70.0)
                    score -= penalty_flat

                # 6. Very low amplitude (weak contact)
                if 1.0 <= eeg_rms < 3.0:
                    score -= 20.0

                score = max(0.0, min(100.0, score))

                # --- Status determination based on dominant problem ---
                if score >= 75.0:
                    status = "Doskonały (Czyste EEG)"
                    status_code = "excellent"
                    advice = "Idealny styk elektrody."
                elif score >= 55.0:
                    status = "Dobry (Prawidłowy)"
                    status_code = "good"
                    advice = "Sygnał stabilny, drobne zakłócenia."
                elif score >= 35.0:
                    # Determine main problem
                    if mains_rms > 8.0:
                        status = "Średni — szum sieci 50 Hz"
                        status_code = "mains_noise"
                        advice = "Wysoki opór styku. Zwilż elektrodę lub rozsuń włosy pod nią."
                    elif crest_factor > 6.0:
                        status = "Średni — artefakty impulsowe"
                        status_code = "spike_artifact"
                        advice = "Wykryto impulsowe zakłócenia. Nie ruszaj się i nie dotykaj opaski."
                    elif eeg_rms > 30.0:
                        status = "Średni — napięcie mięśni (EMG)"
                        status_code = "muscle_artifact"
                        advice = "Rozluźnij mięśnie czoła i szczęki."
                    else:
                        status = "Średni — zakłócony sygnał"
                        status_code = "moderate_noise"
                        advice = "Popraw ułożenie opaski i sprawdź kontakt."
                elif score >= 15.0:
                    if mains_rms > 15.0:
                        status = "Słaby — silny szum 50 Hz"
                        status_code = "bad_contact"
                        advice = "Elektroda leży na włosach. Rozsuń włosy palcami pod elektrodą."
                    else:
                        status = "Słaby — silne artefakty"
                        status_code = "noisy"
                        advice = "Popraw ułożenie opaski i zachowaj nieruchomą pozycję."
                else:
                    status = "Bardzo słaby — brak kontaktu"
                    status_code = "no_contact"
                    advice = "Brak kontaktu z elektrodą. Zdejmij i załóż opaskę ponownie."

            quality[ch] = {
                "rms": round(eeg_rms, 1),
                "eeg_rms": round(eeg_rms, 1),
                "mains_50hz": round(mains_rms, 1),
                "snr_db": round(snr_db, 1),
                "crest_factor": round(crest_factor, 1),
                "spectral_flatness": round(spectral_flatness, 3),
                "score_pct": int(score),
                "status": status,
                "status_code": status_code,
                "advice": advice,
            }
        return quality