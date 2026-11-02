"""
BrainAccess EEG Packet Parser and Delta-Decoder.
Universal: supports HALO (4ch) and MINI (8-channel cap with configurable 10-20 montage).
"""

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import numpy as np

from .constants import (
    DeviceProfile,
    EEG_SCALE_UV,
    PACKET_FOOTER_LEN,
    PROFILE_HALO,
    RAW_CHANNELS_PER_SAMPLE,
)


@dataclass
class EEGSample:
    """
    A single multi-channel EEG sample.

    Channel values are stored in the `channels` dict (electrode name → µV).
    For backward compatibility with HALO code, .fp1/.fp2/.o1/.o2 properties
    are provided.
    """
    sample_index: int
    channels: Dict[str, float]   # e.g. {"F7": 12.3, "FC5": -5.1, ...}
    timestamp: float
    channel_names: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.channel_names:
            self.channel_names = list(self.channels.keys())

    # --- Backward-compatible HALO properties ---
    @property
    def fp1(self) -> float:
        return self.channels.get("Fp1", self.channels.get("Ch0", 0.0))

    @property
    def fp2(self) -> float:
        return self.channels.get("Fp2", self.channels.get("Ch1", 0.0))

    @property
    def o1(self) -> float:
        return self.channels.get("O1", self.channels.get("Ch6", 0.0))

    @property
    def o2(self) -> float:
        return self.channels.get("O2", self.channels.get("Ch7", 0.0))

    def as_array(self) -> np.ndarray:
        """Return channel values as numpy array in channel_names order."""
        return np.array([self.channels.get(ch, 0.0) for ch in self.channel_names], dtype=np.float64)

    def as_dict(self) -> dict:
        """Return flat dict with all channels plus metadata."""
        d = dict(self.channels)
        d["sample_index"] = self.sample_index
        d["timestamp"] = self.timestamp
        return d


# Backward-compatible alias
HaloSample = EEGSample


class EEGPacketDecoder:
    """
    Decodes BLE packets from BrainAccess EEG hardware (HALO and MINI).

    BrainAccess MINI / MIDI packet structure:
      - Initial sample: N channels × 3 bytes (24-bit signed LE ADC counts)
      - Delta records: bitstream of N channels × 12-bit signed differences (MSB first)
      - Footer: 12 bytes [accel_xyz(6B), enable_mask(1B), packet_counter(4B), status(1B)]
                or 6 bytes without accelerometer.

    Also supports fallback to legacy 6-byte int8 record format.
    """

    def __init__(
        self,
        profile: DeviceProfile = PROFILE_HALO,
        scale_uv: float = EEG_SCALE_UV,
    ):
        self.profile = profile
        self.scale_uv = scale_uv
        self.accum = [0.0] * max(RAW_CHANNELS_PER_SAMPLE, profile.channel_count)
        self.last_packet_counter: Optional[int] = None
        self.total_samples_decoded: int = 0
        self.dropped_packets: int = 0

    def reset(self):
        """Reset internal accumulator state."""
        self.accum = [0.0] * max(RAW_CHANNELS_PER_SAMPLE, self.profile.channel_count)
        self.last_packet_counter = None
        self.total_samples_decoded = 0
        self.dropped_packets = 0

    def set_profile(self, profile: DeviceProfile):
        """Switch device profile (e.g. after auto-detection)."""
        self.profile = profile
        self.reset()

    def set_channel_names(self, names: List[str]):
        """Override active channel names (e.g. when applying a 10-20 montage)."""
        if len(names) != self.profile.channel_count:
            raise ValueError(
                f"Expected {self.profile.channel_count} channel names, got {len(names)}"
            )
        self.profile = DeviceProfile(
            model_name=self.profile.model_name,
            electrode_count=self.profile.electrode_count,
            channels=list(names),
            channel_byte_indices=list(range(len(names))),
            stream_config=self.profile.stream_config,
            has_accelerometer=self.profile.has_accelerometer,
        )

    def parse_packet(self, data: bytes | bytearray, timestamp: float) -> List[EEGSample]:
        """
        Parse raw notification payload and return list of decoded EEGSample objects.
        """
        data_len = len(data)
        if data_len < 18:
            return []

        n_ch = self.profile.channel_count
        ch_names = self.profile.channels

        # ---------------------------------------------------------------------
        # 1. BrainAccess MINI: 8 channels with 24-bit Header + 12-bit Deltas
        # ---------------------------------------------------------------------
        if self.profile.electrode_count == 8 or self.profile.model_name == "MINI":
            bytes_per_sample = (n_ch * 12) // 8
            header_bytes = n_ch * 3

            footer_len = None
            for cand_footer in (12, 6):
                payload_len = data_len - cand_footer
                if payload_len >= header_bytes:
                    delta_len = payload_len - header_bytes
                    if delta_len % bytes_per_sample == 0:
                        footer_len = cand_footer
                        break

            if footer_len is not None:
                # Both MINI packet variants put the 32-bit packet counter in
                # the four bytes immediately before the final status byte.
                counter = struct.unpack("<I", data[-5:-1])[0]

                # The first sample is an absolute signed 24-bit ADC value.
                accum = []
                for i in range(n_ch):
                    b = data[i * 3 : (i + 1) * 3]
                    val = b[0] | (b[1] << 8) | (b[2] << 16)
                    if val & 0x800000:
                        val -= 0x1000000
                    accum.append(val)

                samples: List[EEGSample] = []

                def make_sample(values: List[int]) -> EEGSample:
                    return EEGSample(
                        sample_index=self.total_samples_decoded,
                        channels={
                            ch_names[i]: values[i] * self.scale_uv
                            for i in range(n_ch)
                        },
                        timestamp=timestamp,
                        channel_names=list(ch_names),
                    )

                samples.append(make_sample(accum))
                self.total_samples_decoded += 1

                # Following samples contain n_ch signed 12-bit deltas packed
                # most-significant-bit first.
                delta_bytes = data[header_bytes : data_len - footer_len]
                n_delta = len(delta_bytes) // bytes_per_sample
                if self.last_packet_counter is not None:
                    # The footer counter is the index of the last decoded
                    # sample. The current notification advances it by the
                    # initial sample plus all deltas.
                    diff = (counter - self.last_packet_counter) & 0xFFFFFFFF
                    expected_advance = n_delta + 1
                    if diff > expected_advance:
                        self.dropped_packets += diff - expected_advance
                self.last_packet_counter = counter

                bit_buf = 0
                bits_in_buf = 0
                byte_idx = 0
                curr = list(accum)

                for _ in range(n_delta):
                    for ch_i in range(n_ch):
                        while bits_in_buf < 12:
                            bit_buf = (bit_buf << 8) | delta_bytes[byte_idx]
                            byte_idx += 1
                            bits_in_buf += 8

                        shift = bits_in_buf - 12
                        raw_12 = (bit_buf >> shift) & 0xFFF
                        bits_in_buf -= 12
                        bit_buf &= (1 << bits_in_buf) - 1
                        curr[ch_i] += raw_12 - 0x1000 if raw_12 & 0x800 else raw_12

                    samples.append(make_sample(curr))
                    self.total_samples_decoded += 1

                self.accum = curr
                return samples

            # A MINI notification must never be interpreted as the legacy
            # six-byte HALO format: doing so would silently produce fake data.
            return []

        # ---------------------------------------------------------------------
        # 2. Fallback: Legacy 6-byte int8 record format
        # ---------------------------------------------------------------------
        body_len = data_len - PACKET_FOOTER_LEN
        if body_len % RAW_CHANNELS_PER_SAMPLE != 0:
            return []

        footer = data[body_len:]
        counter = struct.unpack("<I", footer[1:5])[0]
        num_records = body_len // RAW_CHANNELS_PER_SAMPLE

        if self.last_packet_counter is not None:
            expected = (self.last_packet_counter + num_records) & 0xFFFFFFFF
            if counter > expected:
                diff = (counter - self.last_packet_counter) & 0xFFFFFFFF
                self.dropped_packets += max(0, (diff - num_records) // num_records)
        self.last_packet_counter = counter

        ch_indices = self.profile.channel_byte_indices
        samples = []

        for r in range(num_records):
            offset = r * RAW_CHANNELS_PER_SAMPLE
            for i in range(min(len(self.accum), RAW_CHANNELS_PER_SAMPLE)):
                b = data[offset + i]
                delta = b if b < 128 else b - 256
                if r == 0:
                    delta = max(-127, min(127, delta))
                self.accum[i] += delta

            ch_values = {}
            for idx, name in enumerate(ch_names):
                b_idx = ch_indices[idx] if idx < len(ch_indices) else idx
                if b_idx < len(self.accum):
                    ch_values[name] = self.accum[b_idx] * self.scale_uv
                else:
                    ch_values[name] = 0.0

            sample = EEGSample(
                sample_index=self.total_samples_decoded,
                channels=ch_values,
                timestamp=timestamp,
                channel_names=list(ch_names),
            )
            samples.append(sample)
            self.total_samples_decoded += 1

        return samples


# Backward-compatible alias
HaloPacketDecoder = EEGPacketDecoder
