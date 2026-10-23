"""Clip planning rules on synthetic analysis data (no video decoding needed)."""

from functools import lru_cache

import numpy as np
import pytest

from vcm.analysis import AnalysisResult
from vcm.selection import OVERLAP_TOL, check_plan_invariants, eligible_mask, plan_compilations


def fake_analysis(times, eligible_runs, quality=None, seed=0):
    rng = np.random.default_rng(seed)
    t = np.asarray(times, dtype=np.float64)
    n = len(t)
    an = AnalysisResult(raw={"t": t}, dissolve_ranges=np.zeros((0, 2), int), width=1, height=1, fps=30)
    d = np.diff(t)
    an.durations = np.concatenate([d, [np.median(d) if len(d) else 1 / 30]])
    e = np.zeros(n, bool)
    for a, b in eligible_runs:
        e[a:b] = True
    an.eligible = e
    an.cut_before = np.zeros(n, bool)
    q = rng.random(n) if quality is None else np.asarray(quality)
    an.quality = q.astype(np.float32)
    an.motion_score = an.sharp_score = an.exposure_score = an.quality
    return an


def brute_force(an, S, D, N):
    """Exhaustive optimum: max clip count K, M = K // N, best score with exactly M*N clips."""
    tl = an.t / S
    n = an.n
    nxt = np.searchsorted(tl, tl + D - 1e-7)
    pref = np.concatenate([[0], np.cumsum(an.quality.astype(float))])
    score = (pref[np.minimum(nxt, n)] - pref[np.arange(n)]) / np.maximum(nxt - np.arange(n), 1)
    valid = []
    for j in range(n):
        e = nxt[j]
        end_ok = e < n or (tl[-1] + an.durations[-1] / S >= tl[j] + D - 1e-7)
        if e > j and an.eligible[j:e].all() and end_ok:
            if e < n or True:
                valid.append(j)

    @lru_cache(None)
    def count(pos):
        best = 0
        for j in valid:
            if j >= pos:
                best = max(best, 1 + count(int(nxt[j])))
        return best

    @lru_cache(None)
    def best(pos, k):
        if k == 0:
            return 0.0
        b = -1e18
        for j in valid:
            if j >= pos:
                b = max(b, score[j] + best(int(nxt[j]), k - 1))
        return b

    K = count(0)
    M = K // N
    return K, M, (best(0, M * N) if M else 0.0)


@pytest.mark.parametrize("trial", range(40))
def test_matches_brute_force_optimum(trial):
    rng = np.random.default_rng(trial)
    n = int(rng.integers(30, 70))
    runs, p = [], 0
    while p < n:
        a = p + int(rng.integers(0, 4))
        b = a + int(rng.integers(2, 20))
        runs.append((a, min(b, n)))
        p = b + int(rng.integers(1, 3))
    an = fake_analysis(np.arange(n) / 10.0, runs, seed=trial)
    S, D, N = 1.3, 0.39 * float(rng.choice([1, 2, 3])), int(rng.integers(1, 4))
    plan = plan_compilations(an, S, D, N)
    K, M, sc = brute_force(an, S, D, N)
    assert plan.max_clips == K
    assert len(plan.compilations) == M
    got = sum(c.score for comp in plan.compilations for c in comp)
    assert got == pytest.approx(sc, abs=1e-6)
    assert check_plan_invariants(plan.compilations, an, S, D) == []


def test_counts_short_one_and_many():
    fps = 30
    # 30 s of continuous eligible footage = 7 clips of 3.9 s -> no compilation
    an = fake_analysis(np.arange(30 * fps) / fps, [(0, 30 * fps)])
    plan = plan_compilations(an, 1.3, 3.0, 10)
    assert plan.max_clips == 7 and plan.compilations == []
    # exactly 39 s -> exactly 10 clips -> one compilation, clips back to back
    an = fake_analysis(np.arange(39 * fps) / fps, [(0, 39 * fps)])
    plan = plan_compilations(an, 1.3, 3.0, 10)
    assert plan.max_clips == 10 and len(plan.compilations) == 1
    # 10 minutes -> floor(600 / 3.9) = 153 clips -> 15 compilations
    an = fake_analysis(np.arange(600 * fps) / fps, [(0, 600 * fps)])
    plan = plan_compilations(an, 1.3, 3.0, 10)
    assert plan.max_clips == 153 and len(plan.compilations) == 15
    assert check_plan_invariants(plan.compilations, an, 1.3, 3.0) == []


def test_clip_windows_are_exact_on_sped_up_timeline():
    fps = 30
    an = fake_analysis(np.arange(120 * fps) / fps, [(0, 120 * fps)])
    plan = plan_compilations(an, 1.3, 3.0, 10)
    for comp in plan.compilations:
        assert len(comp) == 10
        for c in comp:
            assert c.timeline_end - c.timeline_start == pytest.approx(3.0, abs=1e-9)
            assert c.source_end - c.source_start == pytest.approx(3.9, abs=1e-9)
            assert c.timeline_start == pytest.approx(c.source_start / 1.3, abs=1e-12)
            # clips start exactly on a source frame
            assert c.source_start == pytest.approx(an.t[c.start_idx])


def test_cuts_split_runs_and_are_never_crossed():
    fps = 25
    n = 200 * fps
    runs, p = [], 0
    rng = np.random.default_rng(3)
    while p < n:
        L = int(rng.integers(2 * fps, 12 * fps))
        runs.append((p, min(n, p + L)))
        p += L + 3
    an = fake_analysis(np.arange(n) / fps, runs, seed=3)
    plan = plan_compilations(an, 1.3, 3.0, 10)
    assert plan.compilations
    for comp in plan.compilations:
        for c in comp:
            assert an.eligible[c.start_idx:c.end_idx].all()
            assert any(a <= c.start_idx and c.end_idx <= b for a, b in runs)


def test_variable_frame_rate_timestamps():
    rng = np.random.default_rng(1)
    durs = rng.choice([1 / 60, 1 / 30, 1 / 24, 2 / 30], size=6000)
    t = np.concatenate([[0], np.cumsum(durs)[:-1]])
    an = fake_analysis(t, [(0, len(t))])
    plan = plan_compilations(an, 1.3, 3.0, 10)
    assert plan.compilations
    assert check_plan_invariants(plan.compilations, an, 1.3, 3.0) == []
    for comp in plan.compilations:
        for c in comp:
            assert c.source_start in set(t.tolist())


def test_ledger_exclusion_and_rounding_tolerance():
    fps = 30
    an = fake_analysis(np.arange(200 * fps) / fps, [(0, 200 * fps)])
    first = plan_compilations(an, 1.3, 3.0, 10)
    used = [(round(c.source_start, 6), round(c.source_end, 6)) for c in first.compilations[0]]
    second = plan_compilations(an, 1.3, 3.0, 10, used_source_intervals=used)
    assert len(second.compilations) == len(first.compilations) - 1
    for comp in second.compilations:
        for c in comp:
            for a, b in used:
                assert not c.overlaps(a, b)
    assert check_plan_invariants(second.compilations, an, 1.3, 3.0, used) == []
    # adjacent (touching) intervals are not an overlap
    c = first.compilations[0][0]
    assert not c.overlaps(c.source_end, c.source_end + 3.9)
    assert c.overlaps(c.source_end - 2 * OVERLAP_TOL - 0.01, c.source_end + 3.9)


def test_never_reuses_footage_across_compilations():
    fps = 30
    an = fake_analysis(np.arange(300 * fps) / fps, [(0, 140 * fps), (140 * fps + 5, 300 * fps)])
    plan = plan_compilations(an, 1.3, 3.0, 10)
    clips = sorted((c for comp in plan.compilations for c in comp), key=lambda c: c.source_start)
    for a, b in zip(clips, clips[1:]):
        assert b.source_start >= a.source_end - 1e-9
        assert b.start_idx >= a.end_idx


@pytest.mark.parametrize("mode", ["spread", "best_first", "sequential"])
def test_distribution_modes_keep_chronological_order(mode):
    fps = 30
    an = fake_analysis(np.arange(300 * fps) / fps, [(0, 300 * fps)], seed=4)
    plan = plan_compilations(an, 1.3, 3.0, 10, mode)
    assert len(plan.compilations) == 7
    for comp in plan.compilations:
        starts = [c.source_start for c in comp]
        assert starts == sorted(starts)
    if mode == "spread":
        firsts = [comp[0].source_start for comp in plan.compilations]
        assert firsts == sorted(firsts)
        # every compilation covers the whole video
        for comp in plan.compilations:
            assert comp[-1].source_start > 250
    if mode == "best_first":
        means = [np.mean([c.score for c in comp]) for comp in plan.compilations]
        assert means == sorted(means, reverse=True)
    if mode == "sequential":
        for a, b in zip(plan.compilations, plan.compilations[1:]):
            assert a[-1].source_start < b[0].source_start


def test_quality_preference_uses_slack():
    fps = 30
    n = 50 * fps  # 12 clips fit, 10 are needed -> 2 clips' worth of slack
    q = np.full(n, 0.1)
    q[20 * fps:35 * fps] = 1.0  # a high quality section
    an = fake_analysis(np.arange(n) / fps, [(0, n)], quality=q)
    plan = plan_compilations(an, 1.3, 3.0, 10)
    assert len(plan.compilations) == 1
    covered = sum(min(c.source_end, 35) - max(c.source_start, 20) for c in plan.compilations[0]
                  if c.source_end > 20 and c.source_start < 35)
    assert covered >= 14.5  # the good section is used almost completely


def test_eligible_mask_excludes_used():
    fps = 10
    an = fake_analysis(np.arange(100) / fps, [(0, 100)])
    m = eligible_mask(an, [(2.0, 3.0)])
    assert not m[20:30].any() and m[:20].all() and m[30:].all()
