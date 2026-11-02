"""Independent online analyses built on top of decoded EEG samples.

This module deliberately has no BLE or driver dependency.  Applications can
feed it ``EEGSample`` batches from a callback, a file replay, or another API.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional

import numpy as np

from .constants import SAMPLE_RATE
from .packet import EEGSample


class GazeAnalyzer:
    """Calibrated, low-latency up/down gaze classifier.

    With Fp1/Fp2 only, this is a coarse vertical EOG proxy, not a clinical eye
    tracker.  Calibration is required because cap fit and REF/BIAS placement
    change the polarity and amplitude between sessions.
    """

    LABELS = ("neutral", "up", "down")

    def __init__(
        self,
        channels: Iterable[str],
        sample_rate: int = SAMPLE_RATE,
        window_seconds: float = 0.4,
        buffer_seconds: float = 3.0,
    ):
        self.channels = list(channels)
        self.sample_rate = sample_rate
        self.window_size = max(10, int(window_seconds * sample_rate))
        self.buffer_size = max(self.window_size, int(buffer_seconds * sample_rate))
        self._values: Deque[float] = deque(maxlen=self.buffer_size)
        self._timestamps: Deque[float] = deque(maxlen=self.buffer_size)
        self._lock = threading.RLock()
        self._calibration: Dict[str, np.ndarray] = {}
        self._calibrating: Optional[str] = None
        self._calibration_started: Optional[float] = None
        self._calibration_duration = 2.0
        self._calibration_values: List[float] = []

    @property
    def available(self) -> bool:
        return "Fp1" in self.channels and "Fp2" in self.channels

    def reset(self) -> None:
        with self._lock:
            self._values.clear()
            self._timestamps.clear()
            self._calibration.clear()
            self._calibration_values = []
            self._calibrating = None
            self._calibration_started = None
            self._calibration_values = []

    def set_channels(self, channels: Iterable[str]) -> None:
        with self._lock:
            self.channels = list(channels)
            self._values.clear()
            self._timestamps.clear()
            self._calibration.clear()

    def push_samples(self, samples: Iterable[EEGSample]) -> None:
        """Consume decoded samples without changing the BLE driver."""
        if not self.available:
            return
        with self._lock:
            for sample in samples:
                fp1 = float(sample.channels.get("Fp1", 0.0))
                fp2 = float(sample.channels.get("Fp2", 0.0))
                # Common-mode frontal activity is the usable vertical EOG proxy.
                # A common-mode frontal signal is a usable vertical EOG proxy.
                # Remove the slow session baseline when extracting features;
                # REF/BIAS placement otherwise dominates the absolute level.
                value = (fp1 + fp2) / 2.0
                self._values.append(value)
                self._timestamps.append(float(sample.timestamp))
                if self._calibrating:
                    self._calibration_values.append(value)
            if self._calibrating and self._calibration_started is not None:
                if time.monotonic() - self._calibration_started >= self._calibration_duration:
                    self._finish_calibration_locked()

    def start_calibration(self, label: str, seconds: float = 2.0) -> None:
        if label not in self.LABELS:
            raise ValueError(f"label must be one of: {', '.join(self.LABELS)}")
        if not 0.5 <= seconds <= 10.0:
            raise ValueError("calibration seconds must be between 0.5 and 10")
        if not self.available:
            raise ValueError("Gaze analysis requires Fp1 and Fp2 channels")
        with self._lock:
            self._calibrating = label
            self._calibration_started = time.monotonic()
            self._calibration_duration = float(seconds)
            self._calibration_values = []

    def stop_calibration(self) -> Dict[str, object]:
        with self._lock:
            if self._calibrating:
                self._finish_calibration_locked()
            return self.calibration_status()

    def _finish_calibration_locked(self) -> None:
        label = self._calibrating
        values = self._calibration_values
        if label and len(values) >= max(10, self.window_size // 2):
            self._calibration[label] = self._features(np.asarray(values))
        self._calibrating = None
        self._calibration_started = None
        self._calibration_values = []

    def _features(self, values: np.ndarray) -> np.ndarray:
        # Keep the level: it is what separates up/down after session
        # calibration. The second feature rejects noisy, unstable windows.
        return np.array(
            [
                float(np.median(values)),
                float(np.percentile(values, 90) - np.percentile(values, 10)),
            ]
        )

    def calibration_status(self) -> Dict[str, object]:
        return {
            "ready": all(label in self._calibration for label in self.LABELS),
            "labels": sorted(self._calibration),
            "active": self._calibrating,
            "samples": len(self._calibration_values) if self._calibrating else 0,
        }

    def classify(self) -> Dict[str, object]:
        with self._lock:
            result = {"label": "unknown", "confidence": 0.0, "ready": False, **self.calibration_status()}
            if not self.available or len(self._values) < self.window_size:
                result["reason"] = "not_enough_data_or_missing_Fp1_Fp2"
                return result
            current = self._features(np.asarray(list(self._values)[-self.window_size:]))
            if len(self._calibration) < 3:
                result["reason"] = "calibration_required"
                return result
            distances = {label: float(np.linalg.norm(current - ref)) for label, ref in self._calibration.items()}
            ordered = sorted(distances.items(), key=lambda item: item[1])
            best, best_distance = ordered[0]
            second_distance = ordered[1][1]
            confidence = max(0.0, min(1.0, (second_distance - best_distance) / (second_distance + 1e-6)))
            result.update({"label": best, "confidence": round(confidence, 3), "ready": True, "features": current.tolist()})
            return result
