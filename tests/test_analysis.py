"""Cut, jump-cut, transition, black and frozen-frame detection on synthetic footage."""

import numpy as np
import pytest

from vcm.analysis import analyze_video
from vcm.ffmpeg_tools import probe
from vcm.synth import Shot, standard_shots


def _match(det, truth, tol):
    det = np.asarray(det)
    hits = [c for c in truth if len(det) and np.min(np.abs(det - c)) <= tol]
    false = [d for d in det if not len(truth) or np.min(np.abs(np.asarray(truth) - d)) > tol]
    return hits, false


@pytest.mark.parametrize("fps,seed", [(24, 5), (30, 3), (60, 6)])
def test_edited_sequence_cuts_found(videos, fps, seed):
    path, gt = videos.get(f"edited_{fps}.mp4", standard_shots(90.0, seed=seed, avg_shot=6.0),
                          width=480, height=270, fps=fps, seed=seed)
    an = analyze_video(probe(path), use_cache=False)
    tol = 1.5 / fps
    hits, false = _match(an.cut_times(), gt.cuts, tol)
    assert len(hits) == len(gt.cuts), f"missed cuts: {sorted(set(gt.cuts) - set(hits))}"
    # detections inside gradual transitions are fine (that footage is excluded anyway)
    false = [d for d in false if not any(a - 0.1 <= d <= b + 0.1 for a, b in gt.transitions)]
    assert len(false) <= 1, f"false cuts at {false}"
    # every cut and every transition makes the surrounding frames ineligible
    for c in gt.cuts:
        i = int(np.argmin(np.abs(an.t - c)))
        assert not an.eligible[max(0, i - 1):i + 1].any()
    for a, b in gt.transitions:
        m = (an.t >= a) & (an.t < b)
        assert m.any() and not an.eligible[m].any(), f"transition {a:.2f}-{b:.2f} not excluded"


def test_subtle_jump_cuts(videos):
    shots = [
        Shot(5, scene=1, camera="static", sprites=2),
        Shot(5, scene=1, camera="static", sprites=2, transition="jump", jump_offset=0.0, jump_zoom=1.08),
        Shot(5, scene=1, camera="static", sprites=2, transition="jump", jump_offset=0.0, time_skip=1.0),
        Shot(5, scene=1, camera="handheld", sprites=2, transition="jump", jump_offset=0.02, time_skip=0.5),
        Shot(5, scene=2, camera="static", sprites=0),
        Shot(5, scene=2, camera="static", sprites=0, transition="jump", jump_offset=0.015),
    ]
    path, gt = videos.get("subtle.mp4", shots, width=640, height=360, fps=30, seed=11)
    an = analyze_video(probe(path), use_cache=False)
    hits, false = _match(an.cut_times(), gt.cuts, 1.5 / 30)
    assert len(hits) == len(gt.cuts), f"missed subtle cuts: {sorted(set(gt.cuts) - set(hits))}"
    assert not false


def shaky_shots():
    """Handheld, walking and running footage (with motion blur) plus real jump cuts in it."""
    return [
        Shot(8, scene=1, camera="static", shake="handheld", motion_blur=True, sprites=3),
        Shot(8, scene=2, camera="static", shake="walking", motion_blur=True, sprites=4),
        Shot(8, scene=3, camera="pan", shake="running", motion_blur=True, sprites=3),
        Shot(7, scene=4, camera="static", shake="handheld", motion_blur=True, sprites=2,
             transition="jump", jump_offset=0.0, time_skip=1.5),       # jump cut while shaking
        Shot(7, scene=4, camera="static", shake="handheld", motion_blur=True, sprites=2,
             transition="jump", jump_offset=0.0, jump_zoom=1.12),      # punch-in while shaking
        Shot(6, scene=5, camera="static", sprites=2),
        Shot(5, scene=5, camera="static", sprites=2, transition="jump", jump_offset=0.0, jump_zoom=1.08),
        Shot(5, scene=5, camera="static", sprites=2, transition="jump", jump_offset=0.0, time_skip=1.0),
    ]


@pytest.mark.parametrize("fps,seed", [(30, 21), (25, 22)])
def test_camera_shake_is_not_a_cut(videos, fps, seed):
    """Shaky handheld/walking/running footage: no false cuts, real jump cuts still found."""
    path, gt = videos.get(f"shaky_{fps}.mp4", shaky_shots(), width=480, height=270, fps=fps, seed=seed)
    an = analyze_video(probe(path), use_cache=False)
    hits, false = _match(an.cut_times(), gt.cuts, 1.5 / fps)
    assert len(hits) == len(gt.cuts), f"missed cuts: {sorted(set(gt.cuts) - set(hits))}"
    assert not false, f"camera shake detected as cuts at {false}"
    # the shaky shots stay usable apart from the frames right at the real cuts
    assert an.eligible_seconds() > gt.duration - 0.5 * len(gt.cuts) - 0.5


def test_no_false_cuts_in_continuous_motion(videos):
    shots = [Shot(15, scene=3, camera="fast_pan", sprites=8),
             Shot(15, scene=3, camera="shake", sprites=8, transition="jump", jump_offset=0.0),
             Shot(15, scene=5, camera="zoom", sprites=8),
             Shot(15, scene=6, camera="handheld", sprites=8)]
    path, gt = videos.get("motion.mp4", shots, width=480, height=270, fps=30, seed=12)
    an = analyze_video(probe(path), use_cache=False)
    hits, false = _match(an.cut_times(), gt.cuts, 1.5 / 30)
    assert len(hits) == len(gt.cuts)
    assert len(false) <= 1, f"false cuts in continuous motion at {false}"
    # nearly all continuous footage stays eligible
    assert an.eligible_seconds() > 0.95 * gt.duration - 1.0


def test_dissolve_fade_black_and_frozen_excluded(videos):
    shots = [Shot(6, scene=1, camera="pan"),
             Shot(6, scene=2, camera="pan", transition="dissolve", transition_duration=1.0),
             Shot(3, scene=3, black=True),
             Shot(6, scene=4, camera="pan", transition="fade", transition_duration=1.0),
             Shot(3, scene=4, frozen=True),
             Shot(6, scene=5, camera="handheld", transition="dissolve", transition_duration=0.5)]
    path, gt = videos.get("transitions.mp4", shots, width=480, height=270, fps=25, seed=13)
    an = analyze_video(probe(path), use_cache=False)
    for a, b in gt.transitions:
        m = (an.t >= a) & (an.t < b)
        assert an.transition[m].mean() > 0.5 or not an.eligible[m].any()
        assert not an.eligible[m].any()
    for a, b in gt.black:
        m = (an.t >= a) & (an.t < b)
        assert an.black[m].all() and not an.eligible[m].any()
    for a, b in gt.frozen:
        # the encoder may still refine the picture for a few frames after the cut
        m = (an.t >= a + 0.35) & (an.t < b)
        assert an.frozen[m].all() and not an.eligible[m].any()
        m_all = (an.t >= a) & (an.t < b)
        assert an.eligible[m_all].sum() < 0.5 * an.fps, "no usable clip may start in a frozen section"
    sections = an.ineligible_sections()
    reasons = " ".join(s["reason"] for s in sections)
    assert "black" in reasons and "frozen" in reasons and "transition" in reasons


def test_variable_frame_rate_source(videos):
    path, gt = videos.get("vfr.mp4", [Shot(10, scene=1, camera="pan"), Shot(10, scene=2, camera="handheld")],
                          width=480, height=270, fps=30, vfr=True, seed=14)
    info = probe(path)
    an = analyze_video(info, use_cache=False)
    dt = np.diff(an.t)
    assert (dt > 0).all(), "timestamps must be strictly increasing"
    assert dt.max() / dt.min() > 1.5, "the test video should really be variable frame rate"
    hits, false = _match(an.cut_times(), gt.cuts, 0.05)
    assert len(hits) == len(gt.cuts) and not false
    assert an.t[-1] == pytest.approx(gt.duration, abs=0.1)


def test_analysis_cache_roundtrip(one_comp_video):
    path, _ = one_comp_video
    info = probe(path)
    a = analyze_video(info, use_cache=True)
    b = analyze_video(info, use_cache=True)  # served from the cache
    for k in a.raw:
        assert np.array_equal(a.raw[k], b.raw[k])
    assert np.array_equal(a.eligible, b.eligible)
    assert a.cut_times() == b.cut_times()
