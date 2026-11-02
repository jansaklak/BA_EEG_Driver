"""
BrainAccess EEG — Cross-Platform Python Driver (macOS, Windows, Linux).
Supports HALO (4-channel headband) and MINI (8-channel cap).
"""

from .constants import (
    BANDS,
    CHANNELS,
    DEFAULT_PROFILE,
    DeviceProfile,
    ELECTRODE_MAP,
    PROFILE_HALO,
    PROFILE_MINI,
    SAMPLE_RATE,
    EEG_SCALE_UV,
    detect_profile,
    STANDARD_10_20_POSITIONS,
    DEFAULT_MINI_MONTAGE,
    DEFAULT_MINI_REFERENCE,
    DEFAULT_MINI_BIAS,
    MONTAGE_PRESETS,
)
from .driver import HaloDriver
from .dsp import HaloDSP
from .packet import EEGPacketDecoder, EEGSample, HaloPacketDecoder, HaloSample
from .recorder import HaloRecorder
from .lsl_streamer import HaloLSLStreamer
from .online_analysis import GazeAnalyzer

__all__ = [
    # Driver
    "HaloDriver",
    # Sample types
    "EEGSample",
    "HaloSample",       # backward-compat alias
    # Processing
    "HaloDSP",
    "EEGPacketDecoder",
    "HaloPacketDecoder", # backward-compat alias
    "HaloRecorder",
    "HaloLSLStreamer",
    "GazeAnalyzer",
    # Constants
    "SAMPLE_RATE",
    "CHANNELS",
    "BANDS",
    "ELECTRODE_MAP",
    "EEG_SCALE_UV",
    "STANDARD_10_20_POSITIONS",
    "DEFAULT_MINI_MONTAGE",
    "DEFAULT_MINI_REFERENCE",
    "DEFAULT_MINI_BIAS",
    "MONTAGE_PRESETS",
    # Device profiles
    "DeviceProfile",
    "PROFILE_HALO",
    "PROFILE_MINI",
    "DEFAULT_PROFILE",
    "detect_profile",
]
