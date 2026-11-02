import csv
import json
from pathlib import Path

import numpy as np

from tasks.common import SessionPaths
from tasks.music import analysis

CHANNELS = ["Cz", "Fp1", "Fp2", "F3", "F4", "C3", "C4", "Oz"]
FS = 250.0


def make_session(tmp_path, block_seconds=30.0, gap=10.0):
    """Syntetyczna sesja: przy muzyce alfa rośnie 2x; Oz ma artefakt w jednym bloku."""
    paths = SessionPaths(tmp_path / "MUSIC_test_20261030-120000")
    paths.dir.mkdir()
    rng = np.random.default_rng(0)
    t0 = 1000.0
    blocks, start = [], t0 + 10.0
    for index in range(4):
        condition = "silence" if index % 2 == 0 else "music"
        blocks.append({"block": index + 1, "condition": condition, "start_lsl_time": f"{start:.6f}",
                       "end_lsl_time": f"{start + block_seconds:.6f}", "duration_sec": block_seconds,
                       "relax_rating": 2 + index % 2 * 2, "liking_rating": 5 if condition == "music" else ""})
        start += block_seconds + gap
    n = int((start + 5 - t0) * FS)
    times = t0 + np.arange(n) / FS
    data = rng.normal(0, 5, (n, len(CHANNELS)))
    alpha_amp = np.full(n, 10.0)
    for block in blocks:
        if block["condition"] == "music":
            inside = (times >= float(block["start_lsl_time"])) & (times <= float(block["end_lsl_time"]))
            alpha_amp[inside] = 10.0 * np.sqrt(2)
    data += (alpha_amp * np.sin(2 * np.pi * 10 * times))[:, None]
    bad = (times >= float(blocks[0]["start_lsl_time"])) & (times <= float(blocks[0]["end_lsl_time"]))
    data[bad, CHANNELS.index("Oz")] += 2000 * np.sign(np.sin(2 * np.pi * 0.5 * times[bad]))

    with paths.eeg.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_index", "lsl_time", *CHANNELS, "trigger"])
        for i in range(n):
            writer.writerow([i, f"{times[i]:.6f}", *np.round(data[i], 3), ""])
    with paths.behavior.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(blocks[0]))
        writer.writeheader()
        writer.writerows(blocks)
    paths.meta.write_text(json.dumps({"pseudonym": "test", "task_code": "MUSIC", "sample_rate_hz": FS}), encoding="utf-8")
    return paths


def test_music_analysis_detects_alpha_increase(tmp_path):
    paths = make_session(tmp_path)
    summary = analysis.analyze(paths)

    for key in ("topomap_png", "heatmap_png", "timecourse_png", "indices_png", "npz"):
        assert Path(summary["outputs"][key]).exists()
    change = summary["change_pct_music_vs_silence"]["alpha"]
    for channel in ("Cz", "F3", "F4", "C3", "C4"):
        assert 60 < change[channel] < 140  # moc alfa ~2x
    # Oz ma artefakt w całym pierwszym bloku ciszy, ale drugi blok ciszy jest czysty.
    assert summary["clean_windows_pct"]["Oz"] < 80
    assert summary["blocks_detail"][1]["liking_rating"] == 5.0
    assert summary["blocks_detail"][0]["frontal_alpha_asymmetry"] is not None
