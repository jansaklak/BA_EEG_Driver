from driver.brainaccess_halo.online_analysis import GazeAnalyzer
from driver.brainaccess_halo.packet import EEGSample


CHANNELS = ["Cz", "Fp1", "Fp2", "F3", "F4", "C3", "C4", "Oz"]


def _samples(level: float, count: int = 120):
    for index in range(count):
        values = {channel: 0.0 for channel in CHANNELS}
        values["Fp1"] = level
        values["Fp2"] = level
        yield EEGSample(index, values, index / 250.0, CHANNELS)


def test_gaze_calibration_and_classification():
    analyzer = GazeAnalyzer(CHANNELS)
    for label, level in (("neutral", 0.0), ("up", 80.0), ("down", -80.0)):
        analyzer.start_calibration(label, seconds=1.0)
        analyzer.push_samples(_samples(level))
        analyzer.stop_calibration()

    result = analyzer.classify()
    assert result["ready"] is True
    assert result["label"] == "down"
    assert result["confidence"] > 0.9


def test_gaze_requires_vertical_eog_channels():
    analyzer = GazeAnalyzer(["Cz", "F3", "F4", "C3", "C4", "Oz"])
    assert analyzer.available is False
    analyzer.push_samples([])
    assert analyzer.classify()["reason"] == "not_enough_data_or_missing_Fp1_Fp2"


def test_calibration_survives_ring_buffer_rollover():
    analyzer = GazeAnalyzer(CHANNELS, buffer_seconds=1.0)
    analyzer.push_samples(_samples(5.0, 250))
    analyzer.start_calibration("neutral", seconds=1.0)
    analyzer.push_samples(_samples(10.0, 250))
    analyzer.stop_calibration()
    assert analyzer.calibration_status()["labels"] == ["neutral"]
