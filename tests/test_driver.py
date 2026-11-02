"""
Unit tests for BrainAccess EEG driver components:
- Packet decoding (HALO 4ch legacy and MINI 8ch 24b+12b deltas)
- ADS1299 ADC delta accumulation & scaling
- DSP filtering & PSD computation (4ch and 8ch)
- Dynamic 10-20 electrode montage mapping
- Session recorder
"""

import os
import struct
import numpy as np
import pytest

from driver.brainaccess_halo import (
    CHANNELS,
    EEG_SCALE_UV,
    DEFAULT_MINI_MONTAGE,
    MONTAGE_PRESETS,
    STANDARD_10_20_POSITIONS,
    DeviceProfile,
    EEGSample,
    HaloDSP,
    EEGPacketDecoder,
    HaloRecorder,
    SAMPLE_RATE,
    PROFILE_HALO,
    PROFILE_MINI,
)

def test_decoder_halo():
    """Test HALO profile: 4 channels from byte indices 2,3,4,5."""
    decoder = EEGPacketDecoder(profile=PROFILE_HALO)
    
    # Create mock packet: 2 records (12 bytes) + 6 bytes footer = 18 bytes
    # record 1: deltas [10, -5, 20, -15, 30, -25]
    # record 2: deltas [1, 2, 3, 4, 5, 6]
    record1 = bytes([10, 256 - 5, 20, 256 - 15, 30, 256 - 25])
    record2 = bytes([1, 2, 3, 4, 5, 6])
    footer = bytes([0x0F]) + struct.pack("<I", 1) + bytes([0x00])
    
    packet_data = record1 + record2 + footer
    samples = decoder.parse_packet(packet_data, timestamp=1000.0)
    
    assert len(samples) == 2, f"Expected 2 samples, got {len(samples)}"
    
    # Check sample 1:
    # HALO uses byte indices [2, 3, 4, 5] -> Fp1, Fp2, O1, O2
    s1 = samples[0]
    assert s1.channel_names == ["Fp1", "Fp2", "O1", "O2"]
    assert np.isclose(s1.fp1, 20 * EEG_SCALE_UV)
    assert np.isclose(s1.fp2, -15 * EEG_SCALE_UV)
    assert np.isclose(s1.o1, 30 * EEG_SCALE_UV)
    assert np.isclose(s1.o2, -25 * EEG_SCALE_UV)
    assert np.isclose(s1.channels["Fp1"], 20 * EEG_SCALE_UV)
    
    # Check sample 2 (accumulated):
    # byte 2: 20 + 3 = 23
    # byte 3: -15 + 4 = -11
    # byte 4: 30 + 5 = 35
    # byte 5: -25 + 6 = -19
    s2 = samples[1]
    assert np.isclose(s2.fp1, 23 * EEG_SCALE_UV)
    assert np.isclose(s2.fp2, -11 * EEG_SCALE_UV)
    assert np.isclose(s2.o1, 35 * EEG_SCALE_UV)
    assert np.isclose(s2.o2, -19 * EEG_SCALE_UV)
    
    print("✅ test_decoder_halo PASSED")


def test_decoder_mini():
    """Test MINI profile: 8 channels with 24-bit initial + 12-bit deltas."""
    decoder = EEGPacketDecoder(profile=PROFILE_MINI)
    
    # Real packet hex from BrainAccess MINI hardware
    pkt_hex = (
        "cc0507e3f607913707134107361e07d98606cd5706fb4206"
        "b7004dd2c896a68c69d95a03b501249a6ef5a8b8e2e0e46df09d85cc2ffe947a"
        "cbd2cba7158ee33038e2be689ea40a046b6f09367f422704939a0b97bfe330fa"
        "e061f2977711a291b1c14df371fb39752a2e57984eb89b42dec50a60fa6d1128"
        "249fe1557e7f42bdf959ecfd1f682707d06d35d5345f41a0a2104df837030851"
        "921362b1cd04dbfbf35c39a21a1b80dd142f884d1f5627825f6ea66d09cd556c"
        "7cdb0e2d38568e820c38e22c7cfe10910726114de2517cc8400928c38e6d1021"
        "bb7455b8774b5c923604a6ce2878fe773f853e13cb57a66f1139801cdb73587e"
        "12215c0c4e5f036e7cee3de617615407f8f4d4c2b6f90a90c2351482ba5277bb"
        "36da58a72c5b54585c1eeb445de70de2971be2e22ab89b56ed24214b26f04a37"
        "df00f6ff6c00ff1800000018"
    )
    packet_data = bytes.fromhex(pkt_hex)
    samples = decoder.parse_packet(packet_data, timestamp=2000.0)
    
    # 1 initial + 24 deltas = 25 samples
    assert len(samples) == 25, f"Expected 25 samples, got {len(samples)}"
    
    s0 = samples[0]
    assert len(s0.channels) == 8
    assert s0.channel_names == DEFAULT_MINI_MONTAGE
    
    # Check channel 0 initial: 0x0705cc = 460236
    assert np.isclose(s0.channels[DEFAULT_MINI_MONTAGE[0]], 460236 * EEG_SCALE_UV)
    
    # as_array should have 8 elements
    arr = s0.as_array()
    assert arr.shape == (8,)
    
    print("✅ test_decoder_mini PASSED")


def test_channel_mapping():
    """Test dynamic 10-20 montage mapping."""
    decoder = EEGPacketDecoder(profile=PROFILE_MINI)
    custom_montage = ["F7", "FC5", "Fp1", "POz", "C3", "C4", "O1", "O2"]
    decoder.set_channel_names(custom_montage)
    
    assert decoder.profile.channels == custom_montage
    
    # Test all positions are in STANDARD_10_20_POSITIONS
    for pos in ["F7", "FC5", "Fp1", "POz"]:
        assert pos in STANDARD_10_20_POSITIONS
        
    print("✅ test_channel_mapping PASSED")


def test_dsp_mini():
    """Test DSP with 8 MINI channels."""
    channels = DEFAULT_MINI_MONTAGE
    dsp = HaloDSP(buffer_seconds=2.0, window_seconds=1.0, channels=channels)
    
    t = np.linspace(0, 1.5, int(1.5 * SAMPLE_RATE))
    wave = 35.0 * np.sin(2 * np.pi * 10.0 * t)
    
    for i in range(len(t)):
        sample_dict = {
            ch: float(wave[i]) if ch in ("Cz", "Oz") else float(12.0 * np.sin(2 * np.pi * 6.0 * t[i]))
            for ch in channels
        }
        dsp.push_sample(sample_dict)
    
    powers = dsp.get_band_powers()
    assert len(powers) == 8
    assert "Oz" in powers
    assert powers["Oz"]["alpha"] > powers["Oz"]["delta"]
    
    qual = dsp.get_signal_quality()
    assert len(qual) == 8
    for ch in channels:
        assert qual[ch]["score_pct"] > 50
    
    print("✅ test_dsp_mini PASSED")


def test_recorder(tmp_path):
    """Test recorder with dynamic 8-channel header."""
    rec = HaloRecorder(data_dir=tmp_path)
    meta = {
        "model": "MINI",
        "device_name": "BA MINI 002",
        "channels": DEFAULT_MINI_MONTAGE,
        "sample_rate": 250,
        "battery_level": 88,
    }
    rec.start(meta)
    
    sample = EEGSample(
        sample_index=0,
        channels={ch: 10.0 for ch in DEFAULT_MINI_MONTAGE},
        timestamp=123456.78,
        channel_names=DEFAULT_MINI_MONTAGE,
    )
    rec.write_samples([sample])
    info = rec.stop()
    
    assert info["total_samples"] == 1
    with open(info["csv_path"], "r", encoding="utf-8") as f:
        header = f.readline().strip()
        for ch in DEFAULT_MINI_MONTAGE:
            assert ch in header
            
    os.remove(info["csv_path"])
    print("✅ test_recorder PASSED")


def test_profile_detection():
    """Test device name detection."""
    from driver.brainaccess_halo.constants import detect_profile
    assert detect_profile("BA MINI 002").model_name == "MINI"
    assert detect_profile("BrainAccess HALO 001").model_name == "HALO"
    assert detect_profile("Unknown Device").model_name == "MINI"
    print("✅ test_profile_detection PASSED")


def test_fooof_synthetic_alpha():
    """
    Test FOOOF integration on synthetic signal with 1/f background and 10 Hz alpha wave.
    Verifies that:
    1. FOOOF model fits successfully (R^2 > 0.85).
    2. Model info contains fit metrics (R^2, error, n_peaks).
    3. Flattened PSD correctly isolates the 10 Hz alpha peak and removes the aperiodic slope.
    """
    channels = ["Cz", "Oz"]
    dsp = HaloDSP(buffer_seconds=4.0, window_seconds=1.0, channels=channels)

    duration = 4.0
    t = np.linspace(0, duration, int(duration * SAMPLE_RATE), endpoint=False)

    np.random.seed(42)
    # Generate 1/f-like noise by integrating white noise
    white_noise = np.random.randn(len(t)) * 4.0
    pink_noise = np.cumsum(white_noise)
    pink_noise = pink_noise - np.mean(pink_noise)

    # 10 Hz alpha oscillation (25 uV amplitude)
    alpha_wave = 25.0 * np.sin(2.0 * np.pi * 10.0 * t)
    synthetic_signal = pink_noise + alpha_wave

    for i in range(len(t)):
        dsp.push_sample({ch: float(synthetic_signal[i]) for ch in channels})

    res = dsp.get_fooof(max_frequency=45.0, force=True)

    # 1. Structure check
    assert "frequencies" in res
    assert "psd" in res
    assert "psd_flat" in res
    assert "model_info" in res
    freqs = np.array(res["frequencies"])
    psd = np.array(res["psd"])
    psd_flat = np.array(res["psd_flat"])

    assert len(freqs) > 10
    assert len(psd) == len(freqs)
    assert len(psd_flat) == len(freqs)

    # 2. Model fit check
    info = res["model_info"]
    assert info["available"] is True
    assert info["fitted"] is True
    assert info["channels_fitted"] == len(channels)
    assert info["r_squared"] is not None and info["r_squared"] > 0.85
    assert info["n_peaks"] >= 1

    # 3. Peak isolation check in flattened PSD
    idx_10 = int(np.argmin(np.abs(freqs - 10.0)))
    idx_2 = int(np.argmin(np.abs(freqs - 2.0)))
    idx_30 = int(np.argmin(np.abs(freqs - 30.0)))

    # In flattened spectrum, 10 Hz alpha should be much larger than 2 Hz and 30 Hz
    assert psd_flat[idx_10] > psd_flat[idx_2]
    assert psd_flat[idx_10] > psd_flat[idx_30]
    # Check that 10 Hz is in the top 3 highest bins of the flattened spectrum
    assert idx_10 in np.argsort(psd_flat)[-3:]

    print("✅ test_fooof_synthetic_alpha PASSED")


def test_fooof_caching_and_insufficient_data():
    """Test FOOOF cache TTL and graceful handling of insufficient data."""
    channels = ["Cz", "Oz"]
    dsp = HaloDSP(buffer_seconds=4.0, window_seconds=1.0, channels=channels)

    # 1. Insufficient data (< 2 seconds)
    t_short = np.linspace(0, 0.5, int(0.5 * SAMPLE_RATE), endpoint=False)
    for i in range(len(t_short)):
        dsp.push_sample({"Cz": 1.0, "Oz": 1.0})

    short_res = dsp.get_fooof()
    assert short_res["model_info"]["fitted"] is False
    assert "Zbyt mało danych" in short_res["model_info"]["details"]

    # 2. Push full 4 seconds of data
    t_full = np.linspace(0, 4.0, int(4.0 * SAMPLE_RATE), endpoint=False)
    wave = 15.0 * np.sin(2.0 * np.pi * 10.0 * t_full)
    for i in range(len(t_full)):
        dsp.push_sample({"Cz": float(wave[i]), "Oz": float(wave[i])})

    first_res = dsp.get_fooof(cache_ttl_seconds=5.0, force=True)
    assert first_res["model_info"]["fitted"] is True

    # Second call should return the exact cached object
    second_res = dsp.get_fooof(cache_ttl_seconds=5.0, force=False)
    assert second_res is first_res

    # Reset clears cache
    dsp.reset()
    assert dsp._fooof_cache is None

    print("✅ test_fooof_caching_and_insufficient_data PASSED")


if __name__ == "__main__":
    test_decoder_halo()
    test_decoder_mini()
    test_channel_mapping()
    test_dsp_mini()
    import tempfile
    from pathlib import Path
    test_recorder(Path(tempfile.mkdtemp()))
    test_profile_detection()
    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY!")
