"""Hardware configuration and the EEG time base must agree at both rates."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from driver.brainaccess_halo import EEGSample, HaloDSP, HaloDriver, HaloRecorder, PROFILE_MINI
from driver.brainaccess_halo.constants import CHR_STREAM_CONFIG, CHR_STREAM_CMD
from driver.task_runner import SessionEEGWriter, merge_triggers
from tasks.common import SessionPaths


@pytest.mark.parametrize("rate,code", [(250, 6), (500, 5)])
def test_stream_rate_configuration(rate, code):
    driver = HaloDriver(profile=PROFILE_MINI, sample_rate=rate)
    driver._client = SimpleNamespace(
        is_connected=True, write_gatt_char=AsyncMock(), start_notify=AsyncMock()
    )
    driver._connected = True
    asyncio.run(driver.start_stream())
    writes = driver._client.write_gatt_char.await_args_list
    assert writes[0].args == (CHR_STREAM_CONFIG, PROFILE_MINI.stream_config[:-1] + bytes([code]))
    assert writes[1].args == (CHR_STREAM_CMD, b"\x01")
    assert driver.get_summary()["sample_rate"] == driver.dsp.sample_rate == rate
    assert driver._queue.maxsize == 5 * rate


@pytest.mark.parametrize("rate", [250, 500])
def test_psd_frequency_and_filter_response(rate):
    dsp = HaloDSP(channels=["Oz"], sample_rate=rate, lowpass_hz=80)
    times = np.arange(rate * 4) / rate
    signal = 25 * np.sin(2 * np.pi * 10 * times) + 20 * np.sin(2 * np.pi * 50 * times)
    dsp.push_chunk("Oz", signal)
    frequencies, powers = map(np.asarray, dsp.get_psd())
    assert frequencies[np.argmax(powers)] == pytest.approx(10, abs=1)
    assert dsp.get_band_powers()["Oz"]["alpha"] > dsp.get_band_powers()["Oz"]["beta"]
    raw_freqs, raw_powers = map(np.asarray, dsp.get_psd(max_frequency=80, use_filtered=False))
    # The 80 Hz low-pass lets this comparison isolate the notch at 50 Hz.
    filt_freqs, filt_powers = map(np.asarray, dsp.get_psd(max_frequency=80))
    assert filt_powers[np.argmin(abs(filt_freqs - 50))] < 0.05 * raw_powers[np.argmin(abs(raw_freqs - 50))]
    assert dsp.buffer_size == 4 * rate
    assert dsp.window_size == rate


def test_500_hz_recording_metadata_and_timestamps(tmp_path, monkeypatch):
    from driver import task_runner

    recorder = HaloRecorder(data_dir=tmp_path, session_name="rate_test")
    recorder.start({"sample_rate": 500, "channels": ["Oz"]})
    recorder.stop()
    assert json.loads(recorder.meta_path.read_text())["sample_rate_hz"] == 500

    writer = SessionEEGWriter(tmp_path / "samples.csv", ["Oz"], sample_rate=500)
    monkeypatch.setattr(task_runner, "local_clock", lambda: 100.0)
    writer.on_samples([EEGSample(i, {"Oz": 1.0}, 0.0) for i in range(3)])
    writer.close()
    assert writer.timestamps == pytest.approx([99.996, 99.998, 100.0])


def test_trigger_boundary_uses_session_sample_rate(tmp_path):
    paths = SessionPaths(tmp_path / "test")
    paths.dir.mkdir()
    paths.meta.write_text(json.dumps({"sample_rate_hz": 500}))
    # At 500 Hz, an event 3 ms before the first sample is outside the recording.
    paths.events.write_text("lsl_time,marker\n99.997,start\n")
    result = merge_triggers(paths, [100.0, 100.002])
    assert result["events_outside_eeg"] == ["start"]


def test_invalid_or_changed_rate_does_not_disconnect_active_device(monkeypatch):
    from driver import web_dashboard

    driver = HaloDriver(sample_rate=250)
    driver._client = SimpleNamespace(is_connected=True)
    driver._connected = driver._streaming = True
    monkeypatch.setattr(web_dashboard, "driver", driver)
    for rate in (1000, "500", 500):
        result = asyncio.run(web_dashboard.api_connect({"sample_rate": rate}))
        assert result["status"] == "error"
        assert driver.is_connected and driver.is_streaming


def test_lsl_uses_selected_rate_and_sample_spacing(monkeypatch):
    from driver.brainaccess_halo import lsl_streamer

    chunks = []
    fake_lsl = SimpleNamespace(
        StreamInfo=lambda **kwargs: SimpleNamespace(
            desc=lambda: SimpleNamespace(append_child=lambda *args: SimpleNamespace(
                append_child=lambda *args: SimpleNamespace(append_child_value=lambda *args: None)
            )), **kwargs
        ),
        StreamOutlet=lambda info: SimpleNamespace(push_chunk=lambda data, timestamp: chunks.append(timestamp)),
        local_clock=lambda: 100.0,
    )
    monkeypatch.setattr(lsl_streamer, "_pylsl", lambda: fake_lsl)
    streamer = lsl_streamer.HaloLSLStreamer(HaloDriver(sample_rate=500))
    assert streamer.info.nominal_srate == 500
    streamer.start_lsl()
    streamer._on_samples([EEGSample(i, {"Oz": 1.0}, 0.0) for i in range(3)])
    assert chunks[0] == pytest.approx([99.996, 99.998, 100.0])
    streamer.stop_lsl()
