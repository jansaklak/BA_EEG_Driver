"""
Session Recorder for BrainAccess EEG data.
Saves calibrated EEG data to CSV with metadata.
Universal: supports any channel configuration.
"""

import csv
import json
import time
from pathlib import Path
from typing import List, Optional

from .constants import (
    DEFAULT_DATA_DIR,
    DEFAULT_MINI_BIAS,
    DEFAULT_MINI_MONTAGE,
    DEFAULT_MINI_REFERENCE,
    SAMPLE_RATE,
)
from .packet import EEGSample


class HaloRecorder:
    """
    Records incoming EEGSample streams to disk.
    Works with any channel configuration (HALO 4ch, MINI 8ch, etc.).
    """

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        session_name: Optional[str] = None,
        channels: Optional[List[str]] = None,
        data_dir: Optional[Path] = None,
    ):
        if output_dir is not None and data_dir is not None:
            raise ValueError("Pass only one of output_dir or data_dir")
        self.output_dir = Path(output_dir or data_dir or DEFAULT_DATA_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._channels = list(channels) if channels else None

        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        prefix = session_name or f"eeg_session_{timestamp_str}"
        self.csv_path = self.output_dir / f"{prefix}.csv"
        self.meta_path = self.output_dir / f"{prefix}_meta.json"

        self._csv_file = None
        self._csv_writer = None
        self.sample_count: int = 0
        self.start_time: Optional[float] = None

    def start(self, device_metadata: Optional[dict] = None, channels: Optional[List[str]] = None):
        """Open CSV file and write header."""
        if channels:
            self._channels = list(channels)
        elif self._channels is None and device_metadata:
            metadata_channels = device_metadata.get("channels")
            if metadata_channels:
                self._channels = list(metadata_channels)

        self.start_time = time.time()
        self.sample_count = 0

        self._csv_file = open(self.csv_path, mode="w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)

        # Build header dynamically from channel list
        default_channels = (
            DEFAULT_MINI_MONTAGE
            if (device_metadata or {}).get("model") == "MINI"
            else ["Fp1", "Fp2", "O1", "O2"]
        )
        ch_list = self._channels or list(default_channels)
        header = ["sample_index", "timestamp_epoch"] + [f"{ch}_uV" for ch in ch_list]
        self._csv_writer.writerow(header)

        # Save metadata
        meta = {
            "session_start": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sample_rate_hz": SAMPLE_RATE,
            "channels": ch_list,
            "device_info": device_metadata or {},
        }
        if (device_metadata or {}).get("model") == "MINI":
            meta["reference"] = DEFAULT_MINI_REFERENCE
            meta["bias"] = DEFAULT_MINI_BIAS
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    def write_samples(self, samples: List[EEGSample]):
        """Append sample batch to CSV."""
        if not self._csv_writer:
            return

        # Auto-detect channels from first sample if not set
        if self._channels is None and samples:
            self._channels = list(samples[0].channel_names)

        ch_list = self._channels or []

        for s in samples:
            row = [
                s.sample_index,
                f"{s.timestamp:.4f}",
            ]
            for ch in ch_list:
                row.append(f"{s.channels.get(ch, 0.0):.3f}")
            self._csv_writer.writerow(row)
            self.sample_count += 1

    def stop(self):
        """Close file and finalize recording."""
        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        duration = time.time() - (self.start_time or time.time())
        return {
            "csv_path": str(self.csv_path),
            "meta_path": str(self.meta_path),
            "total_samples": self.sample_count,
            "duration_sec": round(duration, 2),
        }
