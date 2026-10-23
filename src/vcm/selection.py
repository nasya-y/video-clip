"""Clip planning on the sped-up timeline.

The source is first mapped onto the sped-up timeline (timeline time = source time /
speed).  A clip is a window of exactly ``clip_duration`` timeline seconds (3.0 s by
default, i.e. 3.9 s of original footage at 1.3x) that lies completely inside one
eligible run of frames - a continuous section without cuts, transitions, black or
frozen frames.  Clips start on real source frames, so trimming is frame accurate even
for variable-frame-rate sources.

Planning maximises the number of complete compilations first and the quality of the
chosen footage second:

1. Within each eligible run the maximum number of non-overlapping clips is found
   greedily (optimal for interval packing on a frame grid).  The total ``K`` gives the
   maximum number of compilations ``M = K // N``.
2. Exactly ``K - M*N`` clips must be left out.  A dynamic programme decides in which
   runs to leave them out and where to place the remaining clips inside each run so
   that the summed quality (motion, sharpness, exposure) is as high as possible.
3. The chosen clips are distributed over the compilations; every compilation keeps its
   clips in chronological source order.

No two clips ever share a source frame, within or across compilations, and footage
already allocated by earlier runs (the ledger) is excluded up front.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np

from .analysis import AnalysisResult

EPS = 1e-7
# Interval comparisons against stored ledger values (rounded to microseconds) use this
# tolerance; any real overlap of footage is at least one frame (>= 4 ms) long.
OVERLAP_TOL = 1e-4


@dataclass
class Clip:
    start_idx: int          # first source frame index (analysis order)
    end_idx: int            # exclusive: first frame after the clip window
    source_start: float     # original source seconds (exact frame timestamp)
    source_end: float       # source_start + clip_duration * speed
    timeline_start: float   # sped-up timeline seconds
    timeline_end: float
    score: float
    motion: float
    sharpness: float
    exposure: float

    def overlaps(self, a: float, b: float) -> bool:
        return self.source_start < b - OVERLAP_TOL and a < self.source_end - OVERLAP_TOL

    def to_dict(self) -> dict:
        return {
            "source_start_s": round(self.source_start, 6),
            "source_end_s": round(self.source_end, 6),
            "source_duration_s": round(self.source_end - self.source_start, 6),
            "source_start_frame": int(self.start_idx),
            "source_frames_in_window": int(self.end_idx - self.start_idx),
            "timeline_start_s": round(self.timeline_start, 6),
            "timeline_end_s": round(self.timeline_end, 6),
            "score": round(float(self.score), 4),
            "motion_score": round(float(self.motion), 4),
            "sharpness_score": round(float(self.sharpness), 4),
            "exposure_score": round(float(self.exposure), 4),
        }


@dataclass
class Plan:
    compilations: list[list[Clip]]
    max_clips: int                    # K: clips that fit into the eligible footage
    clips_per_compilation: int
    eligible_timeline_s: float        # eligible footage on the sped-up timeline
    eligible_runs: list[tuple[float, float]] = field(default_factory=list)  # source seconds
    excluded_by_ledger_s: float = 0.0

    @property
    def leftover_clips(self) -> int:
        return self.max_clips - len(self.compilations) * self.clips_per_compilation


def eligible_mask(an: AnalysisResult, used_source_intervals: Iterable[tuple[float, float]] = ()) -> np.ndarray:
    elig = an.eligible.copy()
    t, d = an.t, an.durations
    for a, b in used_source_intervals:
        elig &= ~((t < b - OVERLAP_TOL) & (t + d > a + OVERLAP_TOL))
    return elig


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if len(mask) == 0:
        return []
    edges = np.nonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))[0]
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


class _Run:
    """Clip geometry inside one eligible run of frames [i0, i1)."""

    def __init__(self, i0: int, i1: int, tl: np.ndarray, tl_dur: np.ndarray, D: float,
                 nxt: np.ndarray):
        self.i0, self.i1 = i0, i1
        end = tl[i1 - 1] + tl_dur[i1 - 1]
        self.end = end
        # valid clip starts: the whole window [t, t + D) must fit inside the run
        last = int(np.searchsorted(tl, end - D + EPS, side="right")) - 1
        self.last_start = min(last, i1 - 1)
        self.E: list[int] = []
        j = i0
        while j <= self.last_start:
            self.E.append(j)
            j = int(nxt[j])
        self.c_max = len(self.E)
        self.tl = tl
        self.D = D

    def latest_starts(self, c: int) -> list[int]:
        L = [0] * c
        j = self.last_start
        for i in range(c - 1, -1, -1):
            L[i] = j
            # latest start whose window ends before j begins
            j = int(np.searchsorted(self.tl, self.tl[j] - self.D + EPS, side="right")) - 1
        return L


def _place(run: _Run, c: int, s: np.ndarray, nxt: np.ndarray) -> tuple[float, list[int]]:
    """Best total score and start frames for exactly c clips inside the run."""
    if c == 0:
        return 0.0, []
    L = run.latest_starts(c)
    E = run.E
    prev_idx = np.arange(E[0], L[0] + 1)
    prev_val = s[prev_idx]
    backs: list[tuple[np.ndarray, np.ndarray]] = []
    for i in range(1, c):
        cur_idx = np.arange(E[i], L[i] + 1)
        # running maximum over predecessors (they are ordered by time)
        run_max = np.maximum.accumulate(prev_val)
        # argmax positions of the running max
        is_new = np.concatenate([[True], prev_val[1:] > run_max[:-1]])
        pm_arg = np.maximum.accumulate(np.where(is_new, np.arange(len(prev_val)), 0))
        cnt = np.searchsorted(nxt[prev_idx], cur_idx, side="right")
        ok = cnt >= 1
        if not ok.all():  # pragma: no cover - geometry guarantees a predecessor
            cur_idx = cur_idx[ok]
            cnt = cnt[ok]
        sel = cnt - 1
        cur_val = s[cur_idx] + run_max[sel]
        backs.append((prev_idx, prev_idx[pm_arg[sel]]))
        prev_idx, prev_val = cur_idx, cur_val
    k = int(np.argmax(prev_val))
    total = float(prev_val[k])
    starts = [int(prev_idx[k])]
    cur_positions = prev_idx
    for (pidx, choice) in reversed(backs):
        pos = int(np.searchsorted(cur_positions, starts[-1]))
        starts.append(int(choice[pos]))
        cur_positions = pidx
    starts.reverse()
    return total, starts


def plan_compilations(an: AnalysisResult, speed: float, clip_duration: float,
                      clips_per_compilation: int, distribution: str = "spread",
                      used_source_intervals: Sequence[tuple[float, float]] = (),
                      max_compilations: Optional[int] = None) -> Plan:
    N = int(clips_per_compilation)
    D = float(clip_duration)
    S = float(speed)
    n = an.n
    if n == 0:
        return Plan([], 0, N, 0.0)
    t = an.t
    tl = t / S
    tl_dur = an.durations / S
    elig = eligible_mask(an, used_source_intervals)
    excluded = float(np.sum(an.durations[an.eligible & ~elig]) / S)
    nxt = np.searchsorted(tl, tl + D - EPS, side="left").astype(np.int64)
    q = an.quality.astype(np.float64)
    pref = np.concatenate([[0.0], np.cumsum(q)])
    cnt = np.maximum(nxt - np.arange(n), 1)
    score = (pref[np.minimum(nxt, n)] - pref[np.arange(n)]) / cnt

    runs = []
    eligible_tl = 0.0
    eligible_runs = []
    for i0, i1 in _runs(elig):
        eligible_tl += float(np.sum(tl_dur[i0:i1]))
        eligible_runs.append((float(t[i0]), float(t[i1 - 1] + an.durations[i1 - 1])))
        r = _Run(i0, i1, tl, tl_dur, D, nxt)
        if r.c_max > 0:
            runs.append(r)
    K = sum(r.c_max for r in runs)
    M = K // N if N > 0 else 0
    if max_compilations is not None:
        M = min(M, max_compilations)
    plan = Plan([], K, N, eligible_tl, eligible_runs, excluded)
    if M == 0:
        return plan
    drop = K - M * N

    # options per run: drop d clips (0..min(drop, c_max)) -> (score, starts)
    options: list[list[tuple[float, list[int]]]] = []
    for r in runs:
        opts = []
        for d in range(0, min(drop, r.c_max) + 1):
            opts.append(_place(r, r.c_max - d, score, nxt))
        options.append(opts)

    # knapsack over runs: exactly `drop` clips dropped in total, maximise score
    NEG = -1e18
    best = np.full(drop + 1, NEG)
    best[0] = 0.0
    choice = []
    for opts in options:
        new = np.full(drop + 1, NEG)
        ch = np.zeros(drop + 1, dtype=np.int64)
        for used in range(drop + 1):
            if best[used] <= NEG / 2:
                continue
            for d, (sc, _) in enumerate(opts):
                u = used + d
                if u > drop:
                    break
                v = best[used] + sc
                if v > new[u]:
                    new[u] = v
                    ch[u] = d
        choice.append(ch)
        best = new
    starts: list[int] = []
    u = drop
    for ri in range(len(runs) - 1, -1, -1):
        d = int(choice[ri][u])
        starts.extend(options[ri][d][1])
        u -= d
    starts.sort()

    clips = []
    for j in starts:
        e = int(nxt[j])
        sl = slice(j, max(e, j + 1))
        clips.append(Clip(
            start_idx=j, end_idx=e,
            source_start=float(t[j]), source_end=float(t[j] + D * S),
            timeline_start=float(tl[j]), timeline_end=float(tl[j] + D),
            score=float(score[j]),
            motion=float(np.mean(an.motion_score[sl])),
            sharpness=float(np.mean(an.sharp_score[sl])),
            exposure=float(np.mean(an.exposure_score[sl])),
        ))
    plan.compilations = distribute(clips, M, N, distribution)
    return plan


def distribute(clips: list[Clip], M: int, N: int, mode: str) -> list[list[Clip]]:
    clips = sorted(clips, key=lambda c: c.source_start)[: M * N]
    if mode == "best_first":
        ranked = sorted(clips, key=lambda c: -c.score)
        groups = [ranked[m * N:(m + 1) * N] for m in range(M)]
    elif mode == "sequential":
        groups = [clips[m * N:(m + 1) * N] for m in range(M)]
    else:  # spread: every compilation samples the whole video
        groups = [clips[m::M] for m in range(M)]
    return [sorted(g, key=lambda c: c.source_start) for g in groups]


def check_plan_invariants(compilations: Sequence[Sequence[Clip]], an: AnalysisResult, speed: float,
                          clip_duration: float, used: Sequence[tuple[float, float]] = ()) -> list[str]:
    """Return a list of violated rules (empty list = plan is valid)."""
    problems = []
    all_clips = [c for comp in compilations for c in comp]
    for comp_i, comp in enumerate(compilations, 1):
        starts = [c.source_start for c in comp]
        if starts != sorted(starts):
            problems.append(f"compilation {comp_i}: clips not chronological")
    ordered = sorted(all_clips, key=lambda c: c.source_start)
    for a, b in zip(ordered, ordered[1:]):
        if b.source_start < a.source_end - OVERLAP_TOL:
            problems.append(f"overlap between clips at {a.source_start:.3f}s and {b.source_start:.3f}s")
    for c in all_clips:
        if abs((c.timeline_end - c.timeline_start) - clip_duration) > 1e-6:
            problems.append(f"clip at {c.source_start:.3f}s has wrong timeline length")
        if abs((c.source_end - c.source_start) - clip_duration * speed) > 1e-6:
            problems.append(f"clip at {c.source_start:.3f}s has wrong source length")
        frames = slice(c.start_idx, c.end_idx)
        if not an.eligible[frames].all():
            problems.append(f"clip at {c.source_start:.3f}s contains ineligible frames")
        if an.cut_before[c.start_idx + 1:c.end_idx].any():
            problems.append(f"clip at {c.source_start:.3f}s contains a cut")
        covered_end = an.t[c.end_idx] if c.end_idx < an.n else an.t[-1] + an.durations[-1]
        if covered_end < c.source_end - 1e-6:
            problems.append(f"clip at {c.source_start:.3f}s runs past the end of the footage")
        for a, b in used:
            if c.overlaps(a, b):
                problems.append(f"clip at {c.source_start:.3f}s reuses ledger interval {a:.3f}-{b:.3f}")
    return problems
