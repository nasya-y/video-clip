"""End-to-end processing of synthetic videos through FFmpeg (integration tests)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from conftest import clip_intervals, load_report, two_shot
from vcm.ffmpeg_tools import FrameReader, probe, read_audio
from vcm.pipeline import Pipeline, collect_videos
from vcm.settings import Settings

TOL_FRAME = 1 / 24 + 0.03


def run(inputs, out: Path, settings: Settings | None = None):
    return Pipeline(settings or Settings(), str(out)).run([str(p) for p in inputs])


def assert_valid_compilation(path: Path, expect_audio: bool, size=None):
    info = probe(str(path))
    assert info.video_codec == "h264"
    assert info.format_name.startswith("mov,mp4")
    assert abs(info.duration - 30.0) <= TOL_FRAME
    if expect_audio:
        assert info.has_audio and info.audio_codec == "aac"
    else:
        assert not info.has_audio
    if size:
        assert info.display_size == size
    return info


def assert_no_forbidden_footage(report: dict, gt, guard: float = 0.0):
    for _, a, b in clip_intervals(report):
        for c in gt.cuts:
            assert not (a + 1e-6 < c < b - 1e-6), f"clip {a:.3f}-{b:.3f} contains the cut at {c:.3f}"
        for x, y in gt.transitions + gt.black + gt.frozen:
            assert b <= x + 1e-6 or a >= y - 1e-6, f"clip {a:.3f}-{b:.3f} overlaps unusable {x:.2f}-{y:.2f}"


def assert_no_overlaps(report: dict):
    iv = sorted(clip_intervals(report), key=lambda r: r[1])
    for (_, a0, b0), (_, a1, b1) in zip(iv, iv[1:]):
        assert a1 >= b0 - 1e-4, f"footage reused: {a0:.4f}-{b0:.4f} and {a1:.4f}-{b1:.4f}"


# ----------------------------------------------------------------------------- counts

def test_short_source_produces_no_compilation(short_video, tmp_path):
    path, gt = short_video
    s = run([path], tmp_path)
    assert s.compilations_created == []
    assert not s.errors
    folder = tmp_path / "short"
    rep = load_report(folder)
    assert rep["status"] == "no_compilations"
    assert list(folder.glob("*.mp4")) == []
    rem = rep["remaining"]
    assert 0 < rem["additional_clips_possible"] < 10
    assert rem["clips_needed_for_next_compilation"] == 10 - rem["additional_clips_possible"]
    assert rem["unused_eligible_footage_source_s"] > 15
    assert "more would be needed" in rem["message"]


def test_exactly_one_compilation_with_full_report(one_comp_video, tmp_path):
    path, gt = one_comp_video
    s = run([path], tmp_path)
    assert not s.errors, s.errors
    folder = tmp_path / Path(path).stem
    assert folder.is_dir(), "output folder must be named after the video"
    files = sorted(p.name for p in folder.glob("*.mp4"))
    assert files == ["Compilation 1.mp4"]
    assert_valid_compilation(folder / "Compilation 1.mp4", True, (480, 270))
    rep = load_report(folder)
    # report contents required by the specification
    assert rep["source"]["filename"] == Path(path).name
    assert rep["source"]["duration_s"] == pytest.approx(gt.duration, abs=0.1)
    assert rep["playback_speed_applied"] == 1.3
    assert "entire source timeline" in rep["speed_method"]
    comp = rep["compilations"][0]
    assert comp["status"] == "completed" and comp["filename"] == "Compilation 1.mp4"
    assert comp["duration_s"] == pytest.approx(30.0, abs=1e-6)
    assert len(comp["clips"]) == 10
    for k, clip in enumerate(comp["clips"]):
        assert clip["source_end_s"] - clip["source_start_s"] == pytest.approx(3.9, abs=1e-5)
        assert clip["timeline_end_s"] - clip["timeline_start_s"] == pytest.approx(3.0, abs=1e-5)
        assert clip["output_frames"] == 90
        assert "score" in clip and "motion_score" in clip
    starts = [c["source_start_s"] for c in comp["clips"]]
    assert starts == sorted(starts)
    wm = comp["watermark"]
    assert wm["text"] == "onlyfans.com/nyraishtara"
    assert wm["colour_hex"].startswith("#") and wm["position"] and wm["font_size_px"] >= 14
    ver = comp["verification"]
    assert ver["passed"] is True
    for name in ("container", "decode", "clip_count", "clip_durations", "total_duration", "speed_applied_once",
                 "clips_match_planned_source", "no_internal_cuts", "chronological_order", "no_footage_reuse",
                 "audio", "watermark"):
        assert ver["checks"][name]["passed"] is True, name
    assert ver["checks"]["speed_applied_once"]["measured_speed_median"] == pytest.approx(1.3, abs=0.03)
    assert ver["checks"]["watermark"]["frames_present"] == ver["checks"]["watermark"]["frames_checked"]
    assert rep["remaining"]["additional_clips_possible"] < 10
    assert rep["allocation_ledger"] and all(e["status"] == "used" for e in rep["allocation_ledger"])
    assert "cuts" in rep["analysis"] and rep["skipped_sections"]
    assert_no_forbidden_footage(rep, gt)


@pytest.fixture(scope="module")
def multi_result(multi_comp_video, tmp_path_factory):
    out = tmp_path_factory.mktemp("multi_out")
    summary = run([multi_comp_video[0]], out)
    return out, summary


def test_multiple_compilations_no_reuse_and_cut_free(multi_comp_video, multi_result):
    path, gt = multi_comp_video
    out, s = multi_result
    assert not s.errors, s.errors
    folder = out / "multi"
    rep = load_report(folder)
    n = rep["compilations_completed"]
    assert n >= 2
    files = sorted(folder.glob("Compilation *.mp4"), key=lambda p: int(p.stem.split()[-1]))
    assert [p.name for p in files] == [f"Compilation {i}.mp4" for i in range(1, n + 1)]
    for p in files:
        assert_valid_compilation(p, True, (480, 270))
    assert_no_overlaps(rep)
    assert_no_forbidden_footage(rep, gt)
    # maximum output: what is left cannot form another compilation
    assert rep["remaining"]["additional_clips_possible"] < 10
    assert len(clip_intervals(rep)) == 10 * n


def test_audio_sync_against_ground_truth_beeps(multi_comp_video, multi_result):
    """Beeps in the output must sit exactly where the plan says the source audio comes from."""
    path, gt = multi_comp_video
    out, _ = multi_result
    rep = load_report(out / "multi")
    comp = rep["compilations"][0]
    sr = 16000
    audio = read_audio(comp["path"], sr)
    env = np.abs(audio)
    # onsets of the 1 kHz beeps (much louder than the background tones)
    hot = env > 0.3
    onsets = np.nonzero(hot & ~np.concatenate([[False], hot[:-1]]))[0] / sr
    onsets = [o for i, o in enumerate(onsets) if i == 0 or o - onsets[i - 1] > 0.05]
    beeps = np.asarray(gt.beeps)
    checked = 0
    for u in onsets:
        clip = next((c for c in comp["clips"] if c["output_start_s"] <= u < c["output_end_s"] - 0.02), None)
        if clip is None or u - clip["output_start_s"] < 0.02:
            continue
        src_t = clip["source_start_s"] + (u - clip["output_start_s"]) * 1.3
        err = float(np.min(np.abs(beeps - src_t)))
        assert err < 0.03, f"audio offset {err * 1000:.0f} ms at output {u:.3f}s"
        checked += 1
    assert checked >= 10


@pytest.mark.parametrize("name,fps,w,h,audio,kw", [
    ("fps24_720p.mp4", 24, 1280, 720, True, {}),
    ("fps25_portrait.mp4", 25, 720, 1280, True, {}),
    ("fps2997_vga.mp4", "30000/1001", 640, 480, True, {}),
    ("fps50_noaudio.mp4", 50, 640, 360, False, {}),
    ("fps60_small.mp4", 60, 426, 240, True, {}),
    ("vfr_pipeline.mp4", 30, 480, 270, True, {"vfr": True}),
    ("anamorphic.mp4", 25, 640, 480, True, {"sar": "4/3"}),
    ("rotated.mp4", 30, 640, 360, True, {"rotation": 90}),
])
def test_frame_rates_resolutions_and_audio(videos, tmp_path, name, fps, w, h, audio, kw):
    from fractions import Fraction
    path, gt = videos.get(name, two_shot(45.0), width=w, height=h, fps=Fraction(fps), audio=audio, seed=31, **kw)
    s = run([path], tmp_path)
    assert not s.errors, s.errors
    folder = tmp_path / Path(name).stem
    rep = load_report(folder)
    assert rep["compilations_completed"] == 1
    out = folder / "Compilation 1.mp4"
    src = probe(path)
    exp_w, exp_h = src.display_size
    exp_w -= exp_w % 2
    exp_h -= exp_h % 2
    info = assert_valid_compilation(out, audio, (exp_w, exp_h))
    if kw.get("rotation"):
        assert (exp_w, exp_h) == (h, w)  # portrait output from a rotated phone-style file
    if kw.get("sar"):
        assert exp_w == 852  # 640 * 4/3 -> square pixels
    comp = rep["compilations"][0]
    assert comp["verification"]["passed"]
    out_fps = Fraction(comp["frame_rate"])
    expected_fps = Fraction(fps) if not kw.get("vfr") else None
    if expected_fps:
        assert out_fps == expected_fps
    total = sum(c["output_frames"] for c in comp["clips"])
    assert abs(total / float(out_fps) - 30.0) <= 1 / float(out_fps) + 1e-9
    assert_no_forbidden_footage(rep, gt)


def test_folder_selection_and_output_folder_skipping(one_comp_video, short_video, tmp_path):
    src_dir = tmp_path / "in"
    src_dir.mkdir()
    import shutil
    shutil.copy(one_comp_video[0], src_dir / "a clip.mp4")
    shutil.copy(short_video[0], src_dir / "b.MOV")
    (src_dir / "notes.txt").write_text("not a video")
    found = collect_videos([str(src_dir)])
    assert [Path(p).name for p in found] == ["a clip.mp4", "b.MOV"]
    out = src_dir / "out"
    s = run([src_dir], out)
    assert not s.errors
    # the output folder (which contains a report) must never be treated as input
    assert collect_videos([str(out / "a clip")]) == []
    # source files untouched
    assert (src_dir / "a clip.mp4").stat().st_size == Path(one_comp_video[0]).stat().st_size


def test_clips_touching_both_ends_of_the_file(videos, tmp_path):
    """39.3 s of continuous footage: ten 3.9 s clips (each starting on a frame, i.e. 98 frames apart at
    25 fps) need 39.18 s, so the clips must run from the first frames to the last frames of the file."""
    from vcm.synth import Shot
    path, gt = videos.get("exact39.mp4", [Shot(39.3, scene=7, camera="pan", sprites=4)], width=320, height=180,
                          fps=25, seed=41)
    s = run([path], tmp_path)
    assert not s.errors, s.errors
    rep = load_report(tmp_path / "exact39")
    comp = rep["compilations"][0]
    assert comp["verification"]["passed"]
    assert comp["clips"][0]["source_start_s"] < 0.15
    assert comp["clips"][-1]["source_end_s"] > 39.15
    assert_valid_compilation(tmp_path / "exact39" / "Compilation 1.mp4", True, (320, 180))


def test_interlaced_source_is_deinterlaced(videos, tmp_path):
    import subprocess
    from vcm.ffmpeg_tools import ffmpeg
    src, gt = videos.get("progressive_for_interlace.mp4", two_shot(45.0), width=640, height=360, fps=25, seed=43)
    inter = tmp_path / "interlaced.mp4"
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-c:v", "libx264",
                    "-flags", "+ildct+ilme", "-x264opts", "tff=1", "-field_order", "tt", "-crf", "18", "-c:a", "copy",
                    str(inter)], check=True)
    assert probe(str(inter)).interlaced
    s = run([inter], tmp_path / "out")
    assert not s.errors, s.errors
    rep = load_report(tmp_path / "out" / "interlaced")
    assert rep["compilations_completed"] == 1
    assert any("deinterlaced" in w for w in rep["warnings"])
    assert rep["compilations"][0]["verification"]["passed"]


def test_file_with_cover_art_stream(videos, tmp_path):
    import subprocess
    from vcm.ffmpeg_tools import ffmpeg
    from PIL import Image
    src, gt = videos.get("for_cover.mp4", two_shot(45.0), width=480, height=270, fps=30, seed=44)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 300), (200, 30, 30)).save(cover)
    out = tmp_path / "with cover.mp4"
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-i", str(cover),
                    "-map", "1", "-map", "0", "-c", "copy", "-c:v:0", "png", "-disposition:v:0", "attached_pic",
                    str(out)], check=True)
    info = probe(str(out))
    assert info.video_codec == "h264" and info.display_size == (480, 270)  # the cover picture is ignored
    s = run([out], tmp_path / "o")
    assert not s.errors, s.errors
    assert load_report(tmp_path / "o" / "with cover")["compilations_completed"] == 1
