"""
BrainAccess EEG — BLE Driver (bleak: CoreBluetooth / WinRT / BlueZ).
Universal: auto-detects HALO / MINI and configures channels dynamically.
Works on macOS (Apple Silicon and Intel), Windows 10/11 and Linux.
"""

import asyncio
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from .constants import (
    BRAINACCESS_NAME_MARKERS,
    CHR_BATTERY_LEVEL,
    CHR_FIRMWARE_REV,
    CHR_HARDWARE_REV,
    CHR_MODEL_NUMBER,
    CHR_SERIAL_NUMBER,
    CHR_MANUFACTURER,
    CHR_STREAM_CMD,
    CHR_STREAM_CONFIG,
    CHR_STREAM_DATA,
    CHR_STREAM_STATUS,
    DEFAULT_PROFILE,
    DeviceProfile,
    PROFILE_HALO,
    PROFILE_MINI,
    SAMPLE_RATE,
    SVC_CONTROL,
    detect_profile,
)
from .dsp import HaloDSP
from .packet import EEGPacketDecoder, EEGSample

logger = logging.getLogger("BrainAccessEEG")


class HaloDriver:
    """
    Asynchronous and thread-safe BLE driver for BrainAccess EEG headsets.
    Auto-detects device model (HALO / MINI) and configures channel mapping.
    """

    def __init__(self, enable_dsp: bool = True, profile: Optional[DeviceProfile] = None):
        self._client: Optional[BleakClient] = None
        self._device: Optional[BLEDevice] = None
        self._connected: bool = False
        self._streaming: bool = False

        # Device profile (auto-detected on connect, or set manually)
        self.profile: DeviceProfile = profile or DEFAULT_PROFILE

        # Device metadata
        self.device_name: Optional[str] = None
        self.device_address: Optional[str] = None
        self.model_number: str = "Unknown"
        self.firmware_revision: str = "Unknown"
        self.hardware_revision: str = "Unknown"
        self.serial_number: str = "Unknown"
        self.manufacturer: str = "Neurotechnology"
        self.battery_level: Optional[int] = None

        # Data handling — decoder uses profile's channel mapping
        self.decoder = EEGPacketDecoder(profile=self.profile)
        self._enable_dsp = enable_dsp
        self.dsp = HaloDSP(channels=self.profile.channels) if enable_dsp else None
        self.callbacks: List[Callable[[List[EEGSample]], None]] = []

        # Thread-safe sample queue for synchronous consumers
        self._queue: asyncio.Queue = asyncio.Queue()
        self._lock = threading.Lock()

        # Internal loop management for sync wrappers
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        # Performance statistics
        self.packet_count: int = 0
        self.sample_count: int = 0
        self.start_time: Optional[float] = None

    def _apply_profile(self, profile: DeviceProfile):
        """Apply a device profile: update decoder, DSP, and channel config."""
        self.profile = profile
        self.decoder.set_profile(profile)
        if self.dsp:
            self.dsp.set_channels(profile.channels)
        logger.info(
            "Device profile: %s (%d channels: %s)",
            profile.model_name,
            profile.channel_count,
            ", ".join(profile.channels),
        )

    @property
    def channels(self) -> List[str]:
        """Active EEG channel names for the connected device."""
        return list(self.profile.channels)

    def set_channel_mapping(self, mapping: List[str] | Dict[int, str]):
        """
        Dynamically map electrode numbers (0..N-1) to scalp 10-20 position names.
        e.g. {0: "Cz", 1: "Fp1", 2: "Fp2", 3: "F3", ...} or ["Cz", "Fp1", ...]
        """
        if isinstance(mapping, dict):
            new_channels = list(self.profile.channels)
            for idx_k, name in mapping.items():
                try:
                    idx = int(idx_k)
                    if 0 <= idx < len(new_channels):
                        new_channels[idx] = str(name).strip()
                except (ValueError, TypeError):
                    pass
        elif isinstance(mapping, (list, tuple)):
            new_channels = [str(name).strip() for name in mapping]
        else:
            raise ValueError("Mapping must be list or dict of channel names")

        new_profile = DeviceProfile(
            model_name=self.profile.model_name,
            electrode_count=self.profile.electrode_count,
            channels=new_channels,
            channel_byte_indices=list(range(len(new_channels))),
            stream_config=self.profile.stream_config,
            has_accelerometer=self.profile.has_accelerometer,
        )
        self.profile = new_profile
        self.decoder.set_channel_names(new_channels)
        if self.dsp:
            self.dsp.set_channels(new_channels)
        logger.info("Updated channel mapping: %s", ", ".join(new_channels))

    # -------------------------------------------------------------------------
    # Scanning
    # -------------------------------------------------------------------------
    @classmethod
    async def scan(cls, timeout: float = 6.0) -> List[Dict]:
        """
        Scan for nearby BrainAccess EEG devices.
        Returns a list of dicts with name, address/UUID, rssi, and whether it matched.
        """
        logger.info("Scanning for Bluetooth LE devices (%.1f s)...", timeout)
        devices_dict: Dict[str, tuple] = {}

        def detection_callback(dev, adv):
            devices_dict[dev.address] = (dev, adv)

        scanner = BleakScanner(detection_callback=detection_callback)
        await scanner.start()
        await asyncio.sleep(timeout)
        await scanner.stop()

        target_uuid = SVC_CONTROL.lower()
        results = []

        for addr, (dev, adv) in devices_dict.items():
            local_name = adv.local_name or dev.name or ""
            service_uuids = [s.lower() for s in (adv.service_uuids or [])]
            rssi = adv.rssi if hasattr(adv, "rssi") else getattr(dev, "rssi", None)

            is_brainaccess = False
            match_reason = ""
            if any(kw in local_name.upper() for kw in BRAINACCESS_NAME_MARKERS):
                is_brainaccess = True
                match_reason = "Name match"
            elif target_uuid in service_uuids:
                is_brainaccess = True
                match_reason = "GATT Service UUID match"

            # Detect device model from name
            detected_profile = detect_profile(local_name) if is_brainaccess else None

            results.append({
                "name": local_name or "(unnamed)",
                "address": addr,
                "rssi": rssi,
                "is_halo": is_brainaccess,          # backward compat key
                "is_brainaccess": is_brainaccess,
                "match_reason": match_reason,
                "device": dev,
                "model": detected_profile.model_name if detected_profile else None,
            })

        # Sort so BrainAccess candidates and highest RSSI come first
        results.sort(key=lambda x: (not x["is_brainaccess"], -(x["rssi"] or -999)))
        return results

    # -------------------------------------------------------------------------
    # Connection
    # -------------------------------------------------------------------------
    async def connect(self, target = None, timeout: float = 12.0) -> bool:
        """
        Connect to a BrainAccess EEG device.
        If `target` is not specified, auto-discovers the nearest BrainAccess device.
        `target` can be a BLEDevice, BLE UUID/address string, or device name.
        """
        if self._connected and self._client and self._client.is_connected:
            logger.info("Already connected to %s", self.device_address)
            return True

        selected_device = None

        if isinstance(target, BLEDevice):
            selected_device = target
        elif target:
            # Try to resolve directly by address first
            try:
                selected_device = await BleakScanner.find_device_by_address(target, timeout=3.0)
            except Exception:
                pass

        if not selected_device:
            candidates = await self.scan(timeout=5.0)
            if target:
                matching = [
                    d for d in candidates
                    if target.lower() == d["address"].lower() or target.lower() in d["name"].lower()
                ]
                if matching:
                    selected_device = matching[0]["device"]
            else:
                ba_devices = [d for d in candidates if d["is_brainaccess"]]
                if ba_devices:
                    selected_device = ba_devices[0]["device"]

        if not selected_device:
            raise ConnectionError(
                f"Czepek BrainAccess ({target or 'auto'}) nie został znaleziony w zasięgu. "
                "Upewnij się, że urządzenie jest włączone i widoczne przez Bluetooth."
            )

        self._device = selected_device
        self.device_name = selected_device.name or "BrainAccess EEG"
        self.device_address = selected_device.address
        logger.info("Connecting to device: %s (%s)...", self.device_name, self.device_address)

        self._client = BleakClient(selected_device, timeout=timeout)
        await self._client.connect()

        if not self._client.is_connected:
            raise ConnectionError(
                f"Failed to establish BLE connection to {self.device_name} "
                f"({self.device_address})"
            )

        self._connected = True
        self.device_address = self._client.address
        self.device_name = getattr(selected_device, "name", None) or "BrainAccess EEG"

        logger.info("Connected to %s (%s)", self.device_name, self.device_address)

        # Auto-detect device profile from name
        detected = detect_profile(self.device_name)
        self._apply_profile(detected)

        # Read standard Device Info and Battery
        await self._query_device_metadata()
        return True

    async def _query_device_metadata(self):
        """Read standard GATT characteristics for device info and battery."""
        for char_uuid, attr_name in [
            (CHR_MODEL_NUMBER, "model_number"),
            (CHR_FIRMWARE_REV, "firmware_revision"),
            (CHR_HARDWARE_REV, "hardware_revision"),
            (CHR_SERIAL_NUMBER, "serial_number"),
            (CHR_MANUFACTURER, "manufacturer"),
        ]:
            try:
                raw_bytes = await self._client.read_gatt_char(char_uuid)
                setattr(self, attr_name, raw_bytes.decode("utf-8", errors="replace").strip("\x00"))
            except Exception:
                pass

        try:
            bat_bytes = await self._client.read_gatt_char(CHR_BATTERY_LEVEL)
            if bat_bytes:
                self.battery_level = int(bat_bytes[0])
        except Exception:
            pass

        logger.info(
            "Device: %s (%s) | Firmware: %s | Serial: %s | Battery: %s%% | Channels: %d",
            self.profile.model_name,
            self.model_number,
            self.firmware_revision,
            self.serial_number,
            f"{self.battery_level}" if self.battery_level is not None else "N/A",
            self.profile.channel_count,
        )

    # -------------------------------------------------------------------------
    # Streaming
    # -------------------------------------------------------------------------
    async def start_stream(self, callback: Optional[Callable[[List[EEGSample]], None]] = None):
        """
        Start real-time EEG streaming from BrainAccess device.
        """
        if not self._connected or not self._client:
            raise RuntimeError("Cannot start stream: Driver is not connected.")

        if self._streaming:
            logger.warning("Stream is already active.")
            return

        if callback:
            self.add_callback(callback)

        self.decoder.reset()
        if self.dsp:
            self.dsp.reset()

        self.packet_count = 0
        self.sample_count = 0
        self.start_time = time.time()

        # 1. Send hardware configuration from device profile
        cfg = self.profile.stream_config
        logger.info(
            "Sending STREAM_CONFIG for %s (PGA=8, Rate=250Hz, %dch): %s",
            self.profile.model_name,
            self.profile.channel_count,
            cfg.hex(),
        )
        await self._client.write_gatt_char(CHR_STREAM_CONFIG, cfg, response=True)

        # 2. Subscribe to EEG notifications
        logger.info("Subscribing to STREAM_DATA notifications...")
        await self._client.start_notify(CHR_STREAM_DATA, self._on_notification)

        # 3. Send Start Command (0x01)
        logger.info("Sending STREAM_CMD (START=0x01)...")
        await self._client.write_gatt_char(CHR_STREAM_CMD, bytes([0x01]), response=True)

        self._streaming = True
        logger.info(
            "BrainAccess %s EEG stream running at %d Hz (%d channels: %s).",
            self.profile.model_name,
            SAMPLE_RATE,
            self.profile.channel_count,
            ", ".join(self.profile.channels),
        )

    async def stop_stream(self):
        """
        Stop real-time EEG streaming.
        """
        if not self._streaming or not self._client:
            return

        try:
            logger.info("Sending STREAM_CMD (STOP=0x00)...")
            await self._client.write_gatt_char(CHR_STREAM_CMD, bytes([0x00]), response=True)
        except Exception as e:
            logger.warning("Failed to send stop command: %s", e)

        try:
            logger.info("Stopping STREAM_DATA notifications...")
            await self._client.stop_notify(CHR_STREAM_DATA)
        except Exception:
            pass

        self._streaming = False
        logger.info("Streaming stopped. Total samples received: %d", self.sample_count)

    def _on_notification(self, char: BleakGATTCharacteristic, data: bytearray):
        """Internal callback invoked when BLE notification packet arrives."""
        recv_time = time.time()
        samples = self.decoder.parse_packet(data, timestamp=recv_time)
        if not samples:
            return

        self.packet_count += 1
        self.sample_count += len(samples)

        ch_names = self.profile.channels

        # Feed DSP for analysis, but keep the live preview on the decoded
        # samples. The preview must match the signal written to recordings.
        queue_items = []
        if self.dsp:
            filt_ch = {}
            for ch in ch_names:
                vals = np.array([s.channels[ch] for s in samples], dtype=np.float64)
                f_chunk, _ = self.dsp.push_chunk(ch, vals)
                filt_ch[ch] = f_chunk

            for i, s in enumerate(samples):
                d = {}
                for ch in ch_names:
                    d[ch] = float(s.channels.get(ch, 0.0))
                    d[f"filtered_{ch}"] = (
                        float(filt_ch[ch][i]) if len(filt_ch[ch]) > i else 0.0
                    )
                d["sample_index"] = s.sample_index
                d["timestamp"] = s.timestamp
                # Add raw values with "raw_" prefix
                for ch in ch_names:
                    d[f"raw_{ch}"] = s.channels[ch]
                queue_items.append(d)
        else:
            queue_items = [s.as_dict() for s in samples]

        # Thread-safe queue put
        for item in queue_items:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                pass

        # Trigger user callbacks
        with self._lock:
            for cb in self.callbacks:
                try:
                    cb(samples)
                except Exception as e:
                    logger.error("Error in user EEG callback: %s", e)

    def add_callback(self, cb: Callable[[List[EEGSample]], None]):
        """Add a callback for receiving decoded sample batches."""
        with self._lock:
            if cb not in self.callbacks:
                self.callbacks.append(cb)

    def configure_preview_filters(self, **settings):
        """Configure filters used for dashboard/queue preview only."""
        if not self.dsp:
            raise RuntimeError("DSP is disabled.")
        self.dsp.configure_filters(**settings)

    def remove_callback(self, cb: Callable[[List[EEGSample]], None]):
        """Remove a previously registered callback."""
        with self._lock:
            if cb in self.callbacks:
                self.callbacks.remove(cb)

    async def get_next_sample(self, timeout: float = 2.0) -> EEGSample:
        """Asynchronously wait for the next decoded sample."""
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)

    # -------------------------------------------------------------------------
    # Disconnect & Teardown
    # -------------------------------------------------------------------------
    async def disconnect(self):
        """Safely stop streaming and disconnect from the device."""
        if self._streaming:
            await self.stop_stream()

        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass

        self._connected = False
        self._client = None
        logger.info("Disconnected from BrainAccess EEG device.")

    @property
    def is_connected(self) -> bool:
        return self._connected and bool(self._client and self._client.is_connected)

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    async def read_battery(self) -> Optional[int]:
        """Read fresh battery level."""
        if self.is_connected and self._client:
            try:
                raw = await self._client.read_gatt_char(CHR_BATTERY_LEVEL)
                if raw:
                    self.battery_level = int(raw[0])
            except Exception:
                pass
        return self.battery_level

    def get_summary(self) -> Dict:
        """Return full device & streaming status dictionary."""
        return {
            "connected": self.is_connected,
            "streaming": self.is_streaming,
            "device_name": self.device_name,
            "address": self.device_address,
            "battery_level": self.battery_level,
            "model": self.profile.model_name,
            "firmware": self.firmware_revision,
            "hardware": self.hardware_revision,
            "serial": self.serial_number,
            "sample_rate": SAMPLE_RATE,
            "channels": list(self.profile.channels),
            "channel_count": self.profile.channel_count,
            "electrode_count": self.profile.electrode_count,
            "total_samples": self.sample_count,
            "dropped_packets": self.decoder.dropped_packets,
        }
