"""
BrainAccess EEG — Lab Streaming Layer (LSL) Outlet.
Universal: channel count and names are determined by the connected device profile.
"""

import logging
import csv
from pathlib import Path
import time
from typing import List, Optional

from .constants import SAMPLE_RATE
from .driver import HaloDriver
from .packet import EEGSample

logger = logging.getLogger("BrainAccessLSL")


def _pylsl():
    """Import przy użyciu – na Linuksie bez liblsl reszta sterownika nadal działa."""
    try:
        import pylsl
        return pylsl
    except Exception as exc:
        raise RuntimeError(
            "Brak biblioteki liblsl potrzebnej do strumienia LSL. Linux: zainstaluj pakiet liblsl "
            "(https://github.com/sccn/liblsl/releases) albo `conda install -c conda-forge liblsl`."
        ) from exc


class HaloLSLStreamer:
    """
    Connects to a BrainAccess EEG device and exposes an LSL EEG outlet.
    Channel count adapts to the device profile (HALO=4, MINI=8, etc.).
    """

    def __init__(
        self,
        driver: HaloDriver,
        stream_name: Optional[str] = None,
        recording_path: Optional[Path] = None,
    ):
        self.driver = driver
        self._lsl = _pylsl()
        ch_names = driver.channels
        model = driver.profile.model_name

        self.stream_name = stream_name or f"BrainAccess-{model}"

        # Create LSL StreamInfo with dynamic channel count
        self.info = self._lsl.StreamInfo(
            name=self.stream_name,
            type="EEG",
            channel_count=len(ch_names),
            nominal_srate=SAMPLE_RATE,
            channel_format="float32",
            source_id=f"brainaccess_{model.lower()}_macos",
        )

        # Set up channel metadata in XML
        channels_xml = self.info.desc().append_child("channels")
        for ch_name in ch_names:
            ch_elem = channels_xml.append_child("channel")
            ch_elem.append_child_value("label", ch_name)
            ch_elem.append_child_value("unit", "microvolts")
            ch_elem.append_child_value("type", "EEG")

        self.outlet = None
        self._ch_names = ch_names
        self._recording_path = Path(recording_path) if recording_path else None
        self._recording_file = None
        self._recording_writer = None

    def start_lsl(self):
        """Initialize the LSL outlet and attach callback to driver."""
        self.outlet = self._lsl.StreamOutlet(self.info)
        if self._recording_path:
            self._recording_path.parent.mkdir(parents=True, exist_ok=True)
            self._recording_file = self._recording_path.open("w", newline="", encoding="utf-8")
            self._recording_writer = csv.writer(self._recording_file)
            self._recording_writer.writerow(["sample_index", "lsl_timestamp", *self._ch_names])
        self.driver.add_callback(self._on_samples)
        logger.info("LSL Stream '%s' is broadcasting on local network (%d channels).",
                     self.stream_name, len(self._ch_names))

    def stop_lsl(self):
        """Detach callback and close outlet."""
        self.driver.remove_callback(self._on_samples)
        self.outlet = None
        if self._recording_file:
            self._recording_file.close()
        self._recording_file = None
        self._recording_writer = None
        logger.info("LSL Stream '%s' stopped.", self.stream_name)

    def _on_samples(self, samples: List[EEGSample]):
        if not self.outlet or not samples:
            return
        # Build chunk: list of [ch0_uV, ch1_uV, ...] per sample
        chunk = [[s.channels.get(ch, 0.0) for ch in self._ch_names] for s in samples]
        # The driver callback receives BLE packets in batches. Backdate each
        # sample from the callback time so marker timestamps align with the
        # sample timeline instead of the whole batch sharing one timestamp.
        end_time = self._lsl.local_clock()
        step = 1.0 / SAMPLE_RATE
        start_time = end_time - step * (len(samples) - 1)
        timestamps = [start_time + step * index for index in range(len(samples))]
        # pylsl calls this argument ``timestamp`` even when it contains one
        # timestamp per sample.
        self.outlet.push_chunk(chunk, timestamp=timestamps)
        if self._recording_writer:
            for sample, timestamp in zip(samples, timestamps):
                self._recording_writer.writerow([
                    sample.sample_index,
                    f"{timestamp:.9f}",
                    *[sample.channels.get(ch, 0.0) for ch in self._ch_names],
                ])
            self._recording_file.flush()
