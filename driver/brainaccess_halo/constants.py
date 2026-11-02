"""
BrainAccess EEG — Constants and BLE GATT Definitions.
Universal driver supporting HALO (4-channel) and MINI (8-channel BLE).
Cross-platform BLE (macOS, Windows, Linux) via bleak.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

# Device names used by current BrainAccess EEG hardware.
BRAINACCESS_NAME_MARKERS = ("HALO", "MINI", "BRAINACCESS", "BA ")

# --- BLE GATT UUIDs ---
# Standard BLE Services & Characteristics
SVC_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"
SVC_BATTERY     = "0000180f-0000-1000-8000-00805f9b34fb"

CHR_BATTERY_LEVEL   = "00002a19-0000-1000-8000-00805f9b34fb"
CHR_MODEL_NUMBER    = "00002a24-0000-1000-8000-00805f9b34fb"
CHR_SERIAL_NUMBER   = "00002a25-0000-1000-8000-00805f9b34fb"
CHR_FIRMWARE_REV    = "00002a26-0000-1000-8000-00805f9b34fb"
CHR_HARDWARE_REV    = "00002a27-0000-1000-8000-00805f9b34fb"
CHR_MANUFACTURER    = "00002a29-0000-1000-8000-00805f9b34fb"

# BrainAccess Custom Control Service (reverse-engineered from libbacore)
SVC_CONTROL        = "ace20bb6-5d39-45de-9f21-b101acf45b9b"
CHR_STREAM_CMD     = "49beba02-5f6b-4ca2-b3c9-2e6b71aa9392"  # write: 0x01 = start, 0x00 = stop
CHR_STREAM_DATA    = "a12f7c4b-9e8d-6a53-1e0f-c274815b36d7"  # notify: streaming packets
CHR_STREAM_CONFIG  = "3467e3a2-4511-ae66-7515-b26fce099337"  # write: hardware configuration
CHR_STREAM_STATUS  = "e7945b26-141c-4459-8090-33b8bc85cee8"  # notify: stream status

# --- ADC & Hardware Specs ---
# BrainAccess devices use an ADS1299-class 24-bit delta-sigma ADC.
# PGA gain = 8 (GainMode 0x04) and internal VREF ≈ 4.5 V.
# LSB = (2 * VREF) / (Gain * 2^24) = 9.0 / (8 * 16777216) ≈ 0.0671 µV/count.
EEG_SCALE_UV: float = 0.0671

SAMPLE_RATE = 250               # Native sample rate in Hz

# Packet Layout (common across all models)
RAW_CHANNELS_PER_SAMPLE = 6    # legacy HALO record format
PACKET_FOOTER_LEN = 6           # [enable_mask(1B), counter_u32_LE(4B), status(1B)]


# ---------------------------------------------------------------------------
# Device Profiles
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeviceProfile:
    """Hardware profile for a specific BrainAccess device model."""
    model_name: str
    electrode_count: int            # physical electrodes on the device
    channels: List[str]             # EEG channel names exposed by the driver
    channel_byte_indices: List[int] # byte offsets within 6-byte BLE record
    stream_config: bytes            # GATT config payload for STREAM_CONFIG
    has_accelerometer: bool = False

    @property
    def channel_count(self) -> int:
        return len(self.channels)

    @property
    def electrode_map(self) -> Dict[str, int]:
        return {name: idx for idx, name in enumerate(self.channels)}


# HALO headband: 4 EEG channels (Fp1, Fp2, O1, O2)
# Records: 6 bytes, but only bytes 2-5 carry EEG. Bytes 0-1 are auxiliary/unused.
# HALO headband: 4 EEG channels (Fp1, Fp2, O1, O2)
# Records: 6 bytes (4 channels × 12 bits)
PROFILE_HALO = DeviceProfile(
    model_name="HALO",
    electrode_count=4,
    channels=["Fp1", "Fp2", "O1", "O2"],
    channel_byte_indices=[2, 3, 4, 5],
    stream_config=bytes([0x00, 0x04, 0x04, 0x04, 0x04, 0x0F, 0x06]),
    has_accelerometer=True,
)

# Standard 10-20 and 10-10 international scalp electrode positions
STANDARD_10_20_POSITIONS: List[str] = [
    # Frontal Pole
    "Fp1", "Fpz", "Fp2",
    # Anterior Frontal
    "AF7", "AF3", "AFz", "AF4", "AF8",
    # Frontal
    "F7", "F5", "F3", "F1", "Fz", "F2", "F4", "F6", "F8",
    # Fronto-Central
    "FT7", "FC5", "FC3", "FC1", "FCz", "FC2", "FC4", "FC6", "FT8",
    # Temporal & Central
    "T7", "C5", "C3", "C1", "Cz", "C2", "C4", "C6", "T8",
    # Centro-Parietal
    "TP7", "CP5", "CP3", "CP1", "CPz", "CP2", "CP4", "CP6", "TP8",
    # Parietal
    "P7", "P5", "P3", "P1", "Pz", "P2", "P4", "P6", "P8",
    # Parieto-Occipital
    "PO7", "PO3", "POz", "PO4", "PO8",
    # Occipital
    "O1", "Oz", "O2", "Iz",
    # Ear / Mastoid references
    "A1", "A2", "M1", "M2",
]

# MINI reference/electrode layout. REF and BIAS are physical reference
# contacts, not additional EEG channels. P8/P7 can work, but an ear/mastoid
# reference is usually preferable when the cap wiring allows it.
DEFAULT_MINI_REFERENCE = "P8"
DEFAULT_MINI_BIAS = "P7"
DEFAULT_MINI_MONTAGE: List[str] = ["Cz", "Fp1", "Fp2", "F3", "F4", "C3", "C4", "Oz"]

MONTAGE_PRESETS: Dict[str, List[str]] = {
    "Mój układ (REF P8, BIAS P7; Cz, Fp1, Fp2, F3, F4, C3, C4, Oz)": [
        "Cz", "Fp1", "Fp2", "F3", "F4", "C3", "C4", "Oz"
    ],
    "Zbalansowany (F7, FC5, Fp1, POz, C3, C4, O1, O2)": [
        "F7", "FC5", "Fp1", "POz", "C3", "C4", "O1", "O2"
    ],
    "Standardowy 10-20 (Fp1, Fp2, F3, F4, C3, C4, O1, O2)": [
        "Fp1", "Fp2", "F3", "F4", "C3", "C4", "O1", "O2"
    ],
    "Czołowy / BCI (Fp1, Fp2, F7, F8, FC5, FC6, C3, C4)": [
        "Fp1", "Fp2", "F7", "F8", "FC5", "FC6", "C3", "C4"
    ],
    "Tylny / Alfa & Sen (C3, C4, P3, Pz, P4, POz, O1, O2)": [
        "C3", "C4", "P3", "Pz", "P4", "POz", "O1", "O2"
    ],
    "Domyślne numeryczne (Ch0, Ch1, Ch2, Ch3, Ch4, Ch5, Ch6, Ch7)": [
        "Ch0", "Ch1", "Ch2", "Ch3", "Ch4", "Ch5", "Ch6", "Ch7"
    ],
}

# MINI cap: 8 EEG electrodes (0, 1, 2, 3, 4, 5, 6, 7) + BIAS + REF.
# Configurable positions (10-20 system: F7, FC5, Fp1, POz, etc.).
# STREAM_CONFIG: 11 bytes:
# [0x00 (impedance), 8x 0x04 (gain X8 for 8 channels), 0xFF (mask 8ch), 0x06 (250Hz)]
PROFILE_MINI = DeviceProfile(
    model_name="MINI",
    electrode_count=8,
    channels=list(DEFAULT_MINI_MONTAGE),
    channel_byte_indices=[0, 1, 2, 3, 4, 5, 6, 7],
    stream_config=bytes([0x00, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0xFF, 0x06]),
    has_accelerometer=True,
)

# Registry for auto-detection by device name
DEVICE_PROFILES: Dict[str, DeviceProfile] = {
    "HALO": PROFILE_HALO,
    "MINI": PROFILE_MINI,
}

# Fallback profile when model cannot be determined
DEFAULT_PROFILE = PROFILE_MINI  # 8 channels by default


def detect_profile(device_name: str) -> DeviceProfile:
    """Auto-detect device profile from BLE advertised name."""
    upper = (device_name or "").upper()
    for key, profile in DEVICE_PROFILES.items():
        if key in upper:
            return profile
    return DEFAULT_PROFILE


# --- Backward compatibility aliases ---
# These point to HALO defaults so existing code that imports them still works.
CHANNELS = PROFILE_HALO.channels
CHANNEL_BYTE_INDICES = PROFILE_HALO.channel_byte_indices
ELECTRODE_MAP = PROFILE_HALO.electrode_map
STREAM_CONFIG_DEFAULT = PROFILE_HALO.stream_config


# --- Brainwave Frequency Bands (Hz) ---
BANDS: dict[str, tuple[float, float]] = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta":  (13.0, 30.0),
    "gamma": (30.0, 45.0),
}

# Directories
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "recordings"
DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
