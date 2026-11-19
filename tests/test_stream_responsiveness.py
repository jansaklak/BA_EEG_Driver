"""Regressions for reception stalls and slow dashboard analysis."""

import asyncio
import json
import struct
import threading
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from driver.brainaccess_halo import HaloDSP, HaloDriver, PROFILE_HALO, SAMPLE_RATE


def halo_packet():
    return bytes([1, 2, 3, 4, 5, 6]) * 2 + bytes([0x0F]) + struct.pack("<I", 2) + b"\x00"


def test_preview_buffer_is_bounded_without_losing_recording_samples():
    driver = HaloDriver(enable_dsp=False, profile=PROFILE_HALO)
    recorded = []
    driver.add_callback(recorded.extend)
    for _ in range(SAMPLE_RATE * 3):
        driver._on_notification(None, halo_packet())

    assert driver._queue.qsize() == SAMPLE_RATE * 5
    assert len(recorded) == driver.sample_count == SAMPLE_RATE * 6
    assert driver.preview_dropped_samples == SAMPLE_RATE
    oldest = driver._queue.get_nowait()
    assert oldest["sample_index"] == SAMPLE_RATE


def test_callback_can_unregister_itself():
    driver = HaloDriver(enable_dsp=False, profile=PROFILE_HALO)
    received = []

    def callback(samples):
        received.extend(samples)
        driver.remove_callback(callback)

    driver.add_callback(callback)
    worker = threading.Thread(target=driver._on_notification, args=(None, halo_packet()), daemon=True)
    worker.start()
    worker.join(timeout=1.0)
    assert not worker.is_alive(), "Callback deadlocked while removing itself"
    driver._on_notification(None, halo_packet())
    assert len(received) == 2


def test_stale_stream_and_disconnect_are_reported(monkeypatch):
    import driver.brainaccess_halo.driver as driver_module

    clock = [10.0]
    monkeypatch.setattr(driver_module.time, "monotonic", lambda: clock[0])
    driver = HaloDriver(enable_dsp=False, profile=PROFILE_HALO)
    driver._client = SimpleNamespace(is_connected=True)
    driver._connected = driver._streaming = True
    driver._stream_started_at = clock[0]
    assert not driver.stream_stalled
    clock[0] += 3.1
    assert driver.stream_stalled  # Includes a stream that never delivered data.
    driver._on_notification(None, halo_packet())
    assert not driver.stream_stalled
    assert driver.last_sample_age == 0.0
    clock[0] += 3.1
    assert driver.get_summary()["stream_stalled"]
    driver._on_disconnect(SimpleNamespace(is_connected=False))
    assert driver.is_connected  # Ignore a callback from a previous client.
    driver._on_disconnect(driver._client)
    assert not driver.is_connected
    assert not driver.is_streaming
    assert not driver.stream_stalled


def test_slow_fooof_does_not_stop_ble_reception_or_websocket(monkeypatch):
    from driver import web_dashboard

    async def scenario():
        driver = HaloDriver(profile=PROFILE_HALO)
        driver._client = SimpleNamespace(is_connected=True)
        driver._connected = driver._streaming = True
        monkeypatch.setattr(web_dashboard, "driver", driver)
        monkeypatch.setattr(web_dashboard, "gaze_analyzer", None)
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        fits = []

        def slow_fit(snapshot, **settings):
            fits.append(snapshot)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5.0)
            return {"model_info": {"available": True, "fitted": True}}

        monkeypatch.setattr(HaloDSP, "get_fooof", slow_fit)
        frames = []

        class WebSocket:
            async def accept(self):
                pass

            async def send_text(self, payload):
                frames.append(json.loads(payload))
                if len(frames) == 3:
                    raise WebSocketDisconnect()

        async def receive_packets():
            while True:
                driver._on_notification(None, halo_packet())
                await asyncio.sleep(0.01)

        receiver = asyncio.create_task(receive_packets())
        try:
            await asyncio.wait_for(web_dashboard.websocket_endpoint(WebSocket()), timeout=2.0)
            await asyncio.wait_for(started.wait(), timeout=1.0)
            assert len(fits) == 1  # One in-flight fit shared by all telemetry calls.
            assert frames[-1]["samples_total"] > frames[0]["samples_total"] > 0
            assert all(frame["chunk"] for frame in frames)
            assert not release.is_set()  # Frames arrived before the fit finished.
            assert fits[0].filtered_buffers is not driver.dsp.filtered_buffers
        finally:
            release.set()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            if driver.dsp._fooof_future is not None:
                await asyncio.wait_for(asyncio.shield(driver.dsp._fooof_future), timeout=2.0)

    asyncio.run(scenario())


def test_reset_discards_an_in_flight_fit(monkeypatch):
    async def scenario():
        dsp = HaloDSP(channels=["Cz"])
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def slow_fit(snapshot, **settings):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5.0)
            return {"model_info": {"fitted": True, "details": "Old data"}}

        monkeypatch.setattr(HaloDSP, "get_fooof", slow_fit)
        dsp.get_fooof_background()
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
            dsp.reset()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.shield(dsp._fooof_future), timeout=2.0)
        result = dsp.get_fooof_background()
        assert not result["model_info"]["fitted"]
        assert dsp._fooof_cache is None
        await asyncio.wait_for(asyncio.shield(dsp._fooof_future), timeout=2.0)

    asyncio.run(scenario())
