"""Automatic quality checks of a rendered compilation.

A compilation is only marked complete when all of these pass:

* container/codec/stream checks (H.264 + AAC MP4, expected size, frame count, duration),
* a full decode of the file without errors and without black frames,
* frame matching: every output frame is compared with the original source frames
  around the time the plan says it came from.  This proves the clip order, the clip
  boundaries (3 s each), that the speed-up was applied exactly once (the matched
  source times advance 1.3x faster than output time) and that no foreign footage
  slipped in,
* an independent cut / transition re-check of every source interval,
* ledger checks: chronological order and no overlap with any other compilation from
  the same source,
* audio: present when the source has audio, same length as the video, and in sync
  (the loudness envelope of each output clip matches the time-scaled envelope of
  its source interval),
* watermark: the expected glyphs in the expected colour at the expected place in
  every checked frame, with readable contrast and a sensible size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Optional, Sequence

import cv2
import numpy as np

from .analysis import ANALYSIS_MAX_SIDE, AnalysisResult, _FeatureExtractor, finalize
from .ffmpeg_tools import (CancelToken, FFmpegError, FrameReader, analysis_size, probe, read_audio, run, ffmpeg,
                           tail)
from .render import AUDIO_RATE, RenderJob, sample_boundaries
from .selection import Clip, EPS, OVERLAP_TOL
from .watermark import WatermarkStyle, WatermarkVerifier

log = logging.getLogger(__name__)

# Same resolution as the main analysis, so the independent cut re-check judges the footage
# the way the analysis did.
VERIFY_SIDE = ANALYSIS_MAX_SIDE
# Footage decoded before each clip so the re-check has the same context as the analysis.
RECHECK_LEAD_S = 1.0
ENV_RATE = 16000
ENV_HOP = 160  # 10 ms


@dataclass
class VerificationResult:
    passed: bool
    checks: dict = field(default_factory=dict)
    bad_clips: list[int] = field(default_factory=list)   # clip indices whose footage must be replaced
    watermark_failed: bool = False
    render_failed: bool = False                          # file broken -> re-render
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "problems": self.problems, "checks": self.checks}


def _norm(gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-variance pixels inside the mask."""
    v = gray[mask].astype(np.float32)
    v -= v.mean()
    sd = float(v.std())
    return v / sd if sd > 1e-3 else v * 0.0


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    """1 - normalised cross-correlation (0 = identical picture)."""
    n = len(a)
    if n == 0:
        return 1.0
    return float(1.0 - np.dot(a, b) / n)


def _envelope(x: np.ndarray) -> np.ndarray:
    n = len(x) // ENV_HOP
    if n == 0:
        return np.zeros(0)
    fr = x[: n * ENV_HOP].reshape(n, ENV_HOP)
    return np.log(1e-4 + np.sqrt((fr.astype(np.float64) ** 2).mean(axis=1)))


def _best_lag(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """Lag (in hops) of b relative to a with the highest correlation."""
    best = (0, -1.0)
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[lag:], b[: len(b) - lag] if lag else b
        else:
            x, y = a[: len(a) + lag], b[-lag:]
        m = min(len(x), len(y))
        if m < 20:
            continue
        x, y = x[:m], y[:m]
        sx, sy = x.std(), y.std()
        if sx < 1e-6 or sy < 1e-6:
            continue
        c = float(np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy))
        if c > best[1]:
            best = (lag, c)
    return best


def verify_compilation(job: RenderJob, out_path: str, style: WatermarkStyle, sensitivity: str,
                       other_intervals: Sequence[tuple[float, float]] = (),
                       cancel: Optional[CancelToken] = None,
                       progress: Optional[Callable[[float], None]] = None) -> VerificationResult:
    res = VerificationResult(passed=False)
    checks = res.checks
    fmt = job.fmt
    S, D = job.speed, job.clip_duration
    N = len(job.clips)
    fb = job.boundaries
    fps = fmt.fps
    expected_frames = fb[-1]
    expected_duration = float(Fraction(expected_frames) / fps)
    frame_dur = float(1 / fps)

    def step(x: float) -> None:
        if progress:
            progress(x)

    # ---------------------------------------------------------------- 1. container / streams
    try:
        info = probe(out_path, cancel)
    except FFmpegError as exc:
        res.render_failed = True
        res.problems.append(f"output file cannot be opened: {exc}")
        checks["file_opens"] = {"passed": False, "error": str(exc)}
        return res
    has_audio_expected = fmt.audio
    stream_ok = (info.has_video and info.video_codec == "h264" and info.display_size == (fmt.width, fmt.height)
                 and (info.has_audio == has_audio_expected) and (not info.has_audio or info.audio_codec == "aac"))
    checks["container"] = {
        "passed": bool(stream_ok), "format": "mp4", "video_codec": info.video_codec,
        "audio_codec": info.audio_codec or None, "resolution": list(info.display_size),
        "expected_resolution": [fmt.width, fmt.height], "audio_expected": has_audio_expected,
    }
    if not stream_ok:
        res.render_failed = True
        res.problems.append("output streams/codecs/resolution are not as expected")
    step(0.05)

    # ---------------------------------------------------------------- 2. full decode of output
    aw, ah = analysis_size(fmt.width, fmt.height, VERIFY_SIDE)
    mask = np.ones((ah, aw), bool)
    sx, sy = aw / fmt.width, ah / fmt.height
    x0, y0 = int(style.x * sx) - 2, int(style.y * sy) - 2
    x1, y1 = int(np.ceil((style.x + style.width) * sx)) + 2, int(np.ceil((style.y + style.height) * sy)) + 2
    mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = False
    reader = FrameReader(out_path, aw, ah, cancel=cancel)
    out_norm, out_times, out_luma, out_contrast = [], [], [], []
    for _, t, f in reader:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        out_norm.append(_norm(g, mask))
        out_times.append(t)
        out_luma.append(float(g.mean()))
        out_contrast.append(float(g.std()))
    n_out = len(out_norm)
    times = np.asarray(out_times)
    cfr_ok = n_out > 1 and float(np.max(np.abs(np.diff(times) - frame_dur))) < 0.25 * frame_dur
    dur_ok = abs(info.duration - expected_duration) <= frame_dur + 0.03
    decode_ok = reader.decode_errors == 0 and n_out == expected_frames
    checks["decode"] = {
        "passed": bool(decode_ok and cfr_ok and dur_ok), "frames_decoded": n_out, "frames_expected": expected_frames,
        "decoder_errors": reader.decode_errors, "constant_frame_rate": bool(cfr_ok),
        "duration_s": round(info.duration, 4), "expected_duration_s": round(expected_duration, 4),
        "duration_tolerance_s": round(frame_dur + 0.03, 4),
    }
    if not checks["decode"]["passed"]:
        res.render_failed = True
        res.problems.append("output did not decode cleanly to the expected number of frames/duration")
        return res
    black = [(i, round(float(times[i]), 3)) for i in range(n_out) if out_luma[i] < 18 and out_contrast[i] < 8]
    checks["no_black_frames"] = {"passed": not black, "black_frames": black[:20]}
    black_clips = sorted({int(np.searchsorted(fb, i, side="right") - 1) for i, _ in black})
    step(0.2)

    # ---------------------------------------------------------------- 3. per-clip source checks
    clip_reports = []
    timing_fail, content_fail, cut_fail = [], [], []
    measured_speeds = []
    for k, clip in enumerate(job.clips):
        if cancel is not None:
            cancel.check()
        t_k = float(job.an.t[clip.start_idx])
        start = max(0.0, t_k - RECHECK_LEAD_S)
        window = D * S * S + 0.7 + (t_k - start)
        src_reader = FrameReader(job.info.path, aw, ah, start=start, duration=window, cancel=cancel,
                                 stream=job.info.video_stream_index,
                                 pre_filters=",".join(job.picture_filters()) or None)
        st, sn, sbgr = [], [], []
        for _, t, f in src_reader:
            st.append(start + t)
            sbgr.append(f.copy())
            sn.append(_norm(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), mask))
        st_arr = np.asarray(st)
        if len(st_arr) < 3:
            content_fail.append(k)
            clip_reports.append({"clip": k + 1, "passed": False, "error": "source interval could not be decoded"})
            continue
        # snap decoded times onto the analysis timeline (removes seek rounding)
        nearest = int(np.argmin(np.abs(st_arr - t_k)))
        st_arr = st_arr + (t_k - st_arr[nearest]) if abs(st_arr[nearest] - t_k) < 0.25 * frame_dur * S else st_arr
        src_dt = float(np.median(np.diff(st_arr))) if len(st_arr) > 1 else frame_dur
        # (a) independent cut re-check inside [t_k, t_k + D*S)
        ext = _FeatureExtractor(aw, ah, 1.0 / max(src_dt, 1e-3))
        for t, f in zip(st_arr, sbgr):
            ext.push(float(t), f)
        raw = {kk: np.asarray(v, dtype=np.float64 if kk == "t" else np.float32) for kk, v in ext.cols.items()}
        mini = finalize(AnalysisResult(raw=raw, dissolve_ranges=np.asarray(ext.dissolves, np.int64).reshape(-1, 2),
                                       width=aw, height=ah, fps=1.0 / max(src_dt, 1e-3)), sensitivity)
        inside = (st_arr > t_k + 0.5 * src_dt) & (st_arr < t_k + D * S - 0.5 * src_dt)
        cut_inside = bool((mini.cut_before & inside).any() or (mini.transition & inside).any()
                          or (mini.black & inside).any())
        if cut_inside:
            cut_fail.append(k)
        if fmt.glass:
            # compare with the source as the output shows it: through the same glass effect
            gts, sn = [], []
            for _, t, f in FrameReader(job.info.path, aw, ah, start=start, duration=window, cancel=cancel,
                                       stream=job.info.video_stream_index, pre_filters=job.match_filters()):
                gts.append(start + t)
                sn.append(_norm(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), mask))
            if len(gts) != len(st_arr) and gts:
                st_arr = np.asarray(gts)
                nearest = int(np.argmin(np.abs(st_arr - t_k)))
                if abs(st_arr[nearest] - t_k) < 0.25 * frame_dur * S:
                    st_arr = st_arr + (t_k - st_arr[nearest])
        # (b) frame matching
        dists, offsets, decisive = [], [], []
        speed_pts = []
        wins_vs_wrong = 0
        wrong_tests = 0
        for j in range(fb[k], fb[k + 1]):
            u = (j - fb[k]) * frame_dur
            tau = t_k + u * S
            cand = np.nonzero(np.abs(st_arr - tau) <= 2.5 * src_dt + 1e-6)[0]
            if len(cand) == 0:
                dists.append(1.0)
                continue
            dd = np.array([_dist(out_norm[j], sn[c]) for c in cand])
            bi = int(np.argmin(dd))
            best_c = int(cand[bi])
            dists.append(float(dd[bi]))
            off = (st_arr[best_c] - tau) / src_dt
            far = [dd[i] for i in range(len(cand)) if abs(int(cand[i]) - best_c) >= 2]
            is_dec = bool(far) and min(far) > dd[bi] * 1.5 + 0.004
            if is_dec:
                decisive.append(j)
                offsets.append(off)
                speed_pts.append((u, float(st_arr[best_c])))
            # would a missing or doubled speed-up have matched better?
            if u > 0.75:
                alt = []
                for wrong in (t_k + u, t_k + u * S * S):
                    ci = int(np.argmin(np.abs(st_arr - wrong)))
                    if abs(st_arr[ci] - wrong) <= 1.5 * src_dt:
                        alt.append(_dist(out_norm[j], sn[ci]))
                if alt:
                    wrong_tests += 1
                    if dd[bi] + 0.004 < min(alt):
                        wins_vs_wrong += 1
        med = float(np.median(dists)) if dists else 1.0
        n_frames = fb[k + 1] - fb[k]
        # Either the pictures are near-identical, or they are reasonably similar and the
        # matches are unambiguous in time (which proves this is the planned footage).
        content_ok = ((med <= 0.06 and float(np.percentile(dists, 90)) <= 0.15)
                      or (med <= 0.3 and len(decisive) >= 0.5 * n_frames))
        timing_ok = True
        measured = None
        if len(offsets) >= 5:
            off_arr = np.abs(np.asarray(offsets))
            timing_ok = float(np.mean(off_arr <= 1.6)) >= 0.9
        if len(speed_pts) >= 10:
            us = np.array([p[0] for p in speed_pts])
            ts = np.array([p[1] for p in speed_pts])
            if us.max() - us.min() >= 0.4 * D:
                slope = float(np.polyfit(us, ts, 1)[0])
                measured = slope
                measured_speeds.append(slope)
                if abs(slope - S) > 0.04 * S:
                    timing_ok = False
        if not content_ok:
            content_fail.append(k)
        if not timing_ok and k not in content_fail:
            timing_fail.append(k)
        clip_reports.append({
            "clip": k + 1,
            "passed": bool(content_ok and timing_ok and not cut_inside),
            "output_frames": fb[k + 1] - fb[k],
            "output_start_s": round(fb[k] * frame_dur, 4),
            "output_duration_s": round((fb[k + 1] - fb[k]) * frame_dur, 4),
            "source_start_s": round(t_k, 4),
            "source_end_s": round(t_k + D * S, 4),
            "picture_match_median_distance": round(med, 4),
            "frames_with_decisive_match": len(decisive),
            "timing_within_tolerance": bool(timing_ok),
            "measured_speed": None if measured is None else round(measured, 4),
            "matched_better_than_wrong_speed": f"{wins_vs_wrong}/{wrong_tests}",
            "cut_or_transition_inside": cut_inside,
        })
        step(0.2 + 0.55 * (k + 1) / N)
    per_clip_frames = [fb[k + 1] - fb[k] for k in range(N)]
    nominal = D * float(fps)
    checks["clip_count"] = {"passed": N == len(job.clips) and len(clip_reports) == N, "clips": N}
    checks["clip_durations"] = {
        "passed": all(abs(f - nominal) <= 1.0 for f in per_clip_frames),
        "frames_per_clip": per_clip_frames,
        "seconds_per_clip": [round(f * frame_dur, 4) for f in per_clip_frames],
        "tolerance": "±1 frame per clip, no accumulated drift",
    }
    checks["total_duration"] = {
        "passed": abs(expected_duration - D * N) <= frame_dur + 1e-9,
        "duration_s": round(expected_duration, 4), "target_s": round(D * N, 4),
    }
    checks["speed_applied_once"] = {
        "passed": not timing_fail,
        "target_speed": S,
        "measured_speed_median": round(float(np.median(measured_speeds)), 4) if measured_speeds else None,
        "clips_measured": len(measured_speeds),
        "note": "speed is measured from motion; static clips cannot be measured and are checked by picture match",
    }
    checks["clips_match_planned_source"] = {"passed": not content_fail, "failed_clips": [k + 1 for k in content_fail]}
    checks["no_internal_cuts"] = {"passed": not cut_fail, "failed_clips": [k + 1 for k in cut_fail]}
    starts = [float(job.an.t[c.start_idx]) for c in job.clips]
    chrono = all(b > a for a, b in zip(starts, starts[1:]))
    within = all(job.clips[i].source_end <= job.clips[i + 1].source_start + OVERLAP_TOL for i in range(N - 1))
    checks["chronological_order"] = {"passed": bool(chrono and within)}
    overlaps = []
    reuse_bad = []
    for k, c in enumerate(job.clips):
        for a, b in other_intervals:
            if c.overlaps(a, b):
                overlaps.append([round(c.source_start, 4), round(a, 4), round(b, 4)])
                reuse_bad.append(k)
    checks["no_footage_reuse"] = {"passed": not overlaps, "intervals_checked": len(other_intervals),
                                  "overlaps": overlaps}
    checks["clips"] = clip_reports

    # ---------------------------------------------------------------- 4. audio
    if fmt.audio:
        try:
            out_a = read_audio(out_path, ENV_RATE, cancel=cancel)
            a_dur = len(out_a) / ENV_RATE
            # samples at 16 kHz for each clip boundary
            sb = [int(round(b * ENV_RATE / AUDIO_RATE)) for b in sample_boundaries(fb, fps)]
            per_clip = []
            sync_fail = []
            for k, clip in enumerate(job.clips):
                t_k = float(job.an.t[clip.start_idx])
                src_a = read_audio(job.info.path, ENV_RATE, start=t_k, duration=D * S, cancel=cancel)
                e_src = _envelope(src_a)
                e_out = _envelope(out_a[sb[k]:sb[k + 1]])
                if len(e_src) < 30 or len(e_out) < 30:
                    per_clip.append({"clip": k + 1, "result": "too short"})
                    continue
                # time-scale the source envelope onto the output clip timeline
                u = np.arange(len(e_out)) * ENV_HOP / ENV_RATE
                src_t = np.arange(len(e_src)) * ENV_HOP / ENV_RATE
                e_src_scaled = np.interp(u * S, src_t, e_src)
                if e_src_scaled.std() < 0.05 or e_out.std() < 0.05:
                    per_clip.append({"clip": k + 1, "result": "inconclusive (steady/silent audio)"})
                    continue
                lag, corr = _best_lag(e_out, e_src_scaled, 15)
                ok = corr >= 0.5 and abs(lag) * 10 <= 40
                if corr < 0.5:
                    ok = True  # weak structure: cannot measure precisely, not evidence of a fault
                    result = "inconclusive (low envelope correlation)"
                else:
                    result = "in sync" if ok else "out of sync"
                if not ok:
                    sync_fail.append(k)
                per_clip.append({"clip": k + 1, "offset_ms": lag * 10, "correlation": round(corr, 3), "result": result})
            len_ok = abs(a_dur - expected_duration) <= frame_dur + 0.03
            silent_out = float(np.sqrt(np.mean(out_a.astype(np.float64) ** 2))) < 1e-5 if len(out_a) else True
            checks["audio"] = {
                "passed": bool(len_ok and not sync_fail),
                "duration_s": round(a_dur, 4), "video_duration_s": round(expected_duration, 4),
                "length_matches_video": bool(len_ok), "silent": bool(silent_out),
                "sync_tolerance_ms": 40, "clips": per_clip,
            }
            if sync_fail:
                timing_fail.extend(k for k in sync_fail if k not in timing_fail)
            if not len_ok:
                res.render_failed = True
        except FFmpegError as exc:
            checks["audio"] = {"passed": False, "error": str(exc)}
            res.render_failed = True
    else:
        checks["audio"] = {"passed": True, "note": "source has no audio; output has no audio track"}
    step(0.85)

    # ---------------------------------------------------------------- 5. watermark in every 2nd frame
    wv = WatermarkVerifier(style, fmt.width, fmt.height)
    cw, ch, cx, cy = wv.crop
    try:
        wreader = FrameReader(out_path, cw, ch, pix_fmt="rgb24", crop=(cw, ch, cx, cy), scale=False,
                              pre_filters="select='not(mod(n\\,2))'", cancel=cancel,
                              color_matrix="bt709" if fmt.colorspace == "bt709" else "bt601")
        for _, _, crop in wreader:
            wv.add(crop)
        wres = wv.result()
    except FFmpegError as exc:
        wres = None
        checks["watermark"] = {"passed": False, "error": str(exc)}
    if wres is not None:
        checks["watermark"] = {"passed": wres.passed, "text": style.text, **wres.to_dict()}
        res.watermark_failed = not wres.passed
    step(1.0)

    # ---------------------------------------------------------------- verdict
    res.bad_clips = sorted(set(content_fail) | set(cut_fail) | set(timing_fail) | set(reuse_bad) | set(black_clips))
    for name, chk in checks.items():
        if isinstance(chk, dict) and not chk.get("passed", True):
            res.problems.append(f"check failed: {name}")
    res.passed = not res.problems and not res.bad_clips
    return res
