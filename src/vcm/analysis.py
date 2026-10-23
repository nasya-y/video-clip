"""Full-length frame analysis: cut / jump-cut / transition detection and quality scoring.

The whole source is decoded once at a small resolution (about 192 px on the long
side).  For every frame we measure:

* colour change (HSV difference) and plain pixel difference to the previous frame,
* dense optical flow (OpenCV DIS) -> total, global (camera) and local (subject) motion,
* the flow-compensated residual: how much of the change can *not* be explained by
  motion.  Real cuts and jump cuts leave a large residual, camera moves do not,
* brightness, contrast, clipping and sharpness for the quality score.

Cuts are found with adaptive (local-baseline) thresholds so that both obvious scene
changes and subtle jump cuts in otherwise static footage are caught, while smooth
camera movement is not mistaken for a cut.  Gradual transitions (cross-dissolves,
fades) are detected separately by testing whether a frame is a blend of the frames
before and after it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from .ffmpeg_tools import CancelToken, FFmpegError, FrameReader, MediaInfo, analysis_size
from .paths import cache_dir

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 4
ANALYSIS_MAX_SIDE = 192

RAW_FIELDS = ("t", "luma", "contrast", "sharp", "dark", "bright", "content", "pix", "res",
              "res_p", "mag", "mag_p", "gmag", "lmag")


@dataclass(frozen=True)
class Sensitivity:
    ratio: float          # spike must be this many times its local baseline
    content_min: float    # minimum HSV change (0-255) for an adaptive colour cut
    residual_min: float   # minimum motion-compensated residual (0-255)
    motion_ratio: float   # motion spike ratio for jump cuts
    motion_min: float     # minimum flow magnitude (px at analysis size) for a jump cut
    hard_content: float   # colour change that is a cut even with a modest ratio
    local_min: float      # minimum localised residual (top 3% of pixels) for a local jump
    local_ratio: float    # localised residual spike ratio


SENSITIVITY = {
    "normal": Sensitivity(3.0, 12.0, 8.0, 5.0, 3.0, 45.0, 25.0, 4.0),
    "high": Sensitivity(2.5, 9.0, 6.0, 4.0, 2.5, 38.0, 18.0, 3.0),
    "maximum": Sensitivity(2.0, 6.0, 4.5, 3.0, 1.5, 30.0, 12.0, 2.5),
}


@dataclass
class AnalysisResult:
    """Per-frame analysis of one source video (times are original source seconds)."""

    raw: dict[str, np.ndarray]
    dissolve_ranges: np.ndarray            # (k, 2) inclusive frame ranges of gradual transitions
    width: int
    height: int
    fps: float
    # derived (filled by finalize)
    durations: np.ndarray = field(default_factory=lambda: np.zeros(0))
    cut_before: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    cut_reason: list = field(default_factory=list)
    transition: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    black: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    frozen: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    eligible: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    quality: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    motion_score: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    sharp_score: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    exposure_score: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    sensitivity: str = "high"

    @property
    def t(self) -> np.ndarray:
        return self.raw["t"]

    @property
    def n(self) -> int:
        return len(self.raw["t"])

    @property
    def end_time(self) -> float:
        if self.n == 0:
            return 0.0
        return float(self.t[-1] + self.durations[-1])

    def cut_times(self) -> list[float]:
        return [float(x) for x in self.t[np.nonzero(self.cut_before)[0]]]

    def ineligible_sections(self) -> list[dict]:
        """Contiguous sections that may not be used, with the reason(s)."""
        out = []
        if self.n == 0:
            return out
        bad = ~self.eligible
        idx = np.nonzero(np.diff(np.concatenate([[0], bad.astype(np.int8), [0]])))[0]
        for a, b in zip(idx[::2], idx[1::2]):
            reasons = []
            if self.transition[a:b].any():
                reasons.append("gradual transition (dissolve/fade)")
            if self.black[a:b].any():
                reasons.append("black or blank frames")
            if self.frozen[a:b].any():
                reasons.append("frozen picture")
            if not reasons:
                reasons.append("cut boundary safety margin")
            out.append({
                "source_start_s": round(float(self.t[a]), 4),
                "source_end_s": round(float(self.t[b - 1] + self.durations[b - 1]), 4),
                "frames": int(b - a),
                "reason": ", ".join(reasons),
            })
        return out

    def eligible_seconds(self) -> float:
        return float(np.sum(self.durations[self.eligible])) if self.n else 0.0

    # -- persistence
    def save(self, path: Path) -> None:
        arrays = {f"raw_{k}": v for k, v in self.raw.items()}
        np.savez_compressed(
            path, **arrays, dissolve_ranges=self.dissolve_ranges,
            meta=np.array([self.width, self.height, self.fps, ANALYSIS_VERSION], dtype=np.float64))

    @classmethod
    def load(cls, path: Path) -> "AnalysisResult":
        with np.load(path) as z:
            meta = z["meta"]
            if int(meta[3]) != ANALYSIS_VERSION:
                raise ValueError("stale analysis cache")
            raw = {k[4:]: z[k] for k in z.files if k.startswith("raw_")}
            return cls(raw=raw, dissolve_ranges=z["dissolve_ranges"], width=int(meta[0]),
                       height=int(meta[1]), fps=float(meta[2]))


# --------------------------------------------------------------------------- helpers

def file_fingerprint(path: str) -> str:
    """Fast identity of a file: size, modification time and hashes of its head and tail."""
    st = os.stat(path)
    h = hashlib.sha1()
    h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > (2 << 20):
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def _neighbor_median(x: np.ndarray, w: int, valid: np.ndarray) -> np.ndarray:
    """Median of x over [i-w, i+w] excluding i itself and invalid entries (NaN if none)."""
    n = len(x)
    if n == 0:
        return x.copy()
    vals = np.where(valid, x, np.nan).astype(np.float64)
    padded = np.concatenate([np.full(w, np.nan), vals, np.full(w, np.nan)])
    win = np.lib.stride_tricks.sliding_window_view(padded, 2 * w + 1).copy()
    win[:, w] = np.nan
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            med = np.nanmedian(win, axis=1)
    return med


def _rank(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x.astype(np.float32)
    order = np.argsort(x, kind="stable")
    r = np.empty(len(x), np.float32)
    r[order] = np.linspace(0.0, 1.0, len(x), dtype=np.float32)
    return r


def _top_mean(a: np.ndarray, frac: float = 0.03) -> float:
    """Mean of the largest ``frac`` share of values (localised change measure)."""
    flat = a.reshape(-1)
    k = max(1, int(len(flat) * frac))
    return float(np.partition(flat, len(flat) - k)[len(flat) - k:].mean())


def _dilate(mask: np.ndarray, before: int, after: int) -> np.ndarray:
    """Extend True runs by ``before`` frames to the left and ``after`` to the right."""
    if not mask.any() or (before <= 0 and after <= 0):
        return mask.copy()
    n = len(mask)
    out = mask.copy()
    idx = np.nonzero(mask)[0]
    for k in range(1, before + 1):
        j = idx - k
        out[j[j >= 0]] = True
    for k in range(1, after + 1):
        j = idx + k
        out[j[j < n]] = True
    return out


# --------------------------------------------------------------------------- extraction

class _FeatureExtractor:
    def __init__(self, width: int, height: int, fps: float):
        self.w, self.h = width, height
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST)
        gx, gy = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
        self.gx, self.gy = gx, gy
        self.border = max(2, min(width, height) // 24)
        self.prev_gray: Optional[np.ndarray] = None
        self.prev_blur: Optional[np.ndarray] = None
        self.prev_hsv: Optional[np.ndarray] = None
        self.cols: dict[str, list[float]] = {k: [] for k in RAW_FIELDS}
        # gradual transition detection on tiny frames
        self.tw, self.th = max(16, width // 3), max(10, height // 3)
        fps = fps if fps and fps > 0 else 30.0
        ks = {max(2, int(round(0.12 * fps))), max(3, int(round(0.30 * fps))), max(4, int(round(0.60 * fps)))}
        self.ks = sorted(ks)
        self.tiny: deque[tuple[np.ndarray, np.ndarray]] = deque(maxlen=2 * self.ks[-1] + 1)
        self.dissolves: list[tuple[int, int]] = []
        self.debug: list[tuple] = []
        self.i = -1

    def push(self, t: float, frame_bgr: np.ndarray) -> None:
        self.i += 1
        c = self.cols
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        c["t"].append(t)
        luma = float(gray.mean())
        c["luma"].append(luma)
        c["contrast"].append(float(gray.std()))
        c["sharp"].append(float(cv2.Laplacian(gray, cv2.CV_32F).var()))
        c["dark"].append(float(np.count_nonzero(gray < 16)) / gray.size)
        c["bright"].append(float(np.count_nonzero(gray > 245)) / gray.size)
        if self.prev_gray is None:
            for k in ("content", "pix", "res", "res_p", "mag", "mag_p", "gmag", "lmag"):
                c[k].append(0.0)
        else:
            dh = cv2.absdiff(hsv, self.prev_hsv)
            c["content"].append(float(dh.reshape(-1, 3).mean(axis=0).mean()))
            c["pix"].append(float(cv2.absdiff(gray, self.prev_gray).mean()))
            flow = self.dis.calc(self.prev_blur, blur, None)
            fx, fy = flow[..., 0], flow[..., 1]
            mapx = self.gx + fx
            mapy = self.gy + fy
            warped = cv2.remap(blur, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            b = self.border
            diff = cv2.absdiff(self.prev_blur, warped)[b:-b, b:-b]
            c["res"].append(float(diff.mean()))
            c["res_p"].append(_top_mean(diff))
            mag = np.sqrt(fx * fx + fy * fy)
            c["mag"].append(float(mag.mean()))
            c["mag_p"].append(_top_mean(mag))
            mx, my = float(np.median(fx)), float(np.median(fy))
            c["gmag"].append(float(np.hypot(mx, my)))
            lx, ly = fx - mx, fy - my
            c["lmag"].append(float(np.sqrt(lx * lx + ly * ly).mean()))
        self.prev_gray, self.prev_blur, self.prev_hsv = gray, blur, hsv
        self._dissolve_step(blur)

    def _dissolve_step(self, blur: np.ndarray) -> None:
        """Detect cross-dissolves / fades: frame i-k is a blend of frames i-2k and i."""
        tiny = cv2.resize(blur, (self.tw, self.th), interpolation=cv2.INTER_AREA).astype(np.float32)
        self.tiny.append((tiny, blur))
        i = self.i
        buf = self.tiny
        for k in self.ks:
            if len(buf) < 2 * k + 1:
                continue
            Bt, B = buf[-1]
            Mt, M = buf[-1 - k]
            At, A = buf[-1 - 2 * k]
            # cheap gate on tiny frames: the two ends must differ clearly
            if float(np.abs(At - Bt).mean()) < 8.0:
                continue
            A32, B32, M32 = A.astype(np.float32), B.astype(np.float32), M.astype(np.float32)
            d_ab = A32 - B32
            denom = float((d_ab * d_ab).sum())
            if denom <= 1e-6:
                continue
            alpha = float(((M32 - B32) * d_ab).sum()) / denom
            if not 0.15 <= alpha <= 0.85:
                continue
            b = self.border
            r_bl = float(np.abs(M32 - (alpha * A32 + (1.0 - alpha) * B32))[b:-b, b:-b].mean())
            r_a = float(np.abs(M32 - A32)[b:-b, b:-b].mean())
            r_b = float(np.abs(M32 - B32)[b:-b, b:-b].mean())
            if min(r_a, r_b) < 3.0 or r_bl > 0.4 * min(r_a, r_b):
                continue
            # Motion can mimic a blend; require that warping A or B onto M with optical
            # flow explains M clearly worse than the blend does.
            rw = min(self._warp_residual(A, M), self._warp_residual(B, M))
            if rw < 1.6 * r_bl + 0.5:
                continue
            self.dissolves.append((i - 2 * k, i))
            self.debug.append((i - 2 * k, i, k, alpha, r_bl, r_a, r_b, rw))

    def _warp_residual(self, src: np.ndarray, dst: np.ndarray) -> float:
        flow = self.dis.calc(dst, src, None)
        warped = cv2.remap(src, self.gx + flow[..., 0], self.gy + flow[..., 1], cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
        b = self.border
        return float(cv2.absdiff(dst, warped)[b:-b, b:-b].mean())


def analyze_video(info: MediaInfo, cancel: Optional[CancelToken] = None,
                  progress: Optional[Callable[[float], None]] = None,
                  use_cache: bool = True, sensitivity: str = "high") -> AnalysisResult:
    """Decode and analyse every frame of the source video."""
    cache_file: Optional[Path] = None
    if use_cache:
        try:
            key = hashlib.sha1(f"{file_fingerprint(info.path)}|{ANALYSIS_VERSION}|{ANALYSIS_MAX_SIDE}".encode()).hexdigest()
            cache_file = cache_dir() / f"analysis-{key}.npz"
            if cache_file.is_file():
                res = AnalysisResult.load(cache_file)
                log.info("Using cached analysis for %s", info.path)
                if progress:
                    progress(1.0)
                return finalize(res, sensitivity)
        except Exception as exc:  # corrupted/stale cache -> recompute
            log.info("Analysis cache not usable (%s)", exc)

    dw, dh = info.display_size
    aw, ah = analysis_size(dw, dh, ANALYSIS_MAX_SIDE)
    fps = float(info.fps)
    ext = _FeatureExtractor(aw, ah, fps)
    expected = max(1.0, info.duration * fps)
    reader = FrameReader(info.path, aw, ah, cancel=cancel)
    last_report = -1.0
    for idx, t, frame in reader:
        ext.push(t, frame)
        if progress and idx % 25 == 0:
            frac = min(0.999, idx / expected)
            if frac - last_report >= 0.002:
                progress(frac)
                last_report = frac
    if ext.i < 0:
        raise FFmpegError("No video frames could be decoded from this file.", reader.log_tail())
    if reader.decode_errors:
        log.warning("%s: %d decoder warnings during analysis", info.path, reader.decode_errors)
    raw = {k: np.asarray(v, dtype=np.float64 if k == "t" else np.float32) for k, v in ext.cols.items()}
    dissolves = np.asarray(ext.dissolves, dtype=np.int64).reshape(-1, 2)
    result = AnalysisResult(raw=raw, dissolve_ranges=dissolves, width=aw, height=ah, fps=fps)
    result.decode_warnings = reader.decode_errors  # type: ignore[attr-defined]
    result.debug_dissolves = ext.debug  # type: ignore[attr-defined]
    if cache_file is not None:
        try:
            tmp = cache_file.with_name(cache_file.stem + ".tmp.npz")
            result.save(tmp)
            os.replace(tmp, cache_file)
        except OSError as exc:
            log.info("Could not write analysis cache: %s", exc)
    if progress:
        progress(1.0)
    return finalize(result, sensitivity)


# --------------------------------------------------------------------------- decisions

def finalize(res: AnalysisResult, sensitivity: str = "high") -> AnalysisResult:
    """Turn raw per-frame measurements into cuts, eligibility and quality scores."""
    s = SENSITIVITY.get(sensitivity, SENSITIVITY["high"])
    res.sensitivity = sensitivity
    r = res.raw
    n = len(r["t"])
    t = r["t"]
    if n == 0:
        return res
    # frame durations (last frame: median duration)
    dt = np.diff(t)
    pos = dt[dt > 0]
    med_dt = float(np.median(pos)) if len(pos) else 1.0 / max(res.fps, 1.0)
    durations = np.empty(n)
    durations[:-1] = np.where(dt > 0, dt, med_dt)
    durations[-1] = med_dt
    res.durations = durations
    fps_eff = 1.0 / med_dt if med_dt > 0 else res.fps

    # Variable frame rate: a frame that follows a long interval naturally changes more.
    # Scale such frames down to the typical interval so they do not look like spikes.
    scale = np.ones(n)
    scale[1:] = np.minimum(1.0, med_dt / np.maximum(dt, 1e-6))
    content, pix, resid, mag = r["content"] * scale, r["pix"], r["res"] * scale, r["mag"] * scale
    res_p, mag_p = r["res_p"] * scale, r["mag_p"] * scale
    dup = pix < 0.08
    valid = ~dup
    valid[0] = False
    w = 4
    base_c = np.nan_to_num(_neighbor_median(content, w, valid), nan=0.0)
    base_r = np.nan_to_num(_neighbor_median(resid, w, valid), nan=0.0)
    base_m = np.nan_to_num(_neighbor_median(mag, w, valid), nan=0.0)
    base_rp = np.nan_to_num(_neighbor_median(res_p, w, valid), nan=0.0)
    ratio_c = content / np.maximum(base_c, 1.0)
    ratio_r = resid / np.maximum(base_r, 0.75)
    ratio_m = mag / np.maximum(base_m, 0.35)
    ratio_rp = res_p / np.maximum(base_rp, 2.0)

    hard = (content >= s.hard_content) & (ratio_c >= 1.6)
    colour = (content >= s.content_min) & (ratio_c >= s.ratio) & (ratio_r >= 1.5) & (resid >= 0.5 * s.residual_min)
    residual = (resid >= s.residual_min) & (ratio_r >= s.ratio)
    # part of the picture changed abruptly (e.g. the subject jumps position)
    local = (res_p >= s.local_min) & (ratio_rp >= s.local_ratio) & (resid >= 1.5 * base_r)
    # an isolated motion spike (not periodic shake): frames two steps away must be calm
    mag_pm2 = np.maximum(np.concatenate([[0, 0], mag[:-2]]), np.concatenate([mag[2:], [0, 0]]))
    jump = (((mag >= s.motion_min) | (mag_p >= 2.0 * s.motion_min)) & (ratio_m >= s.motion_ratio)
            & (mag >= 2.0 * mag_pm2) & (resid >= 1.5 * base_r + 0.2))
    gap = np.zeros(n, bool)
    gap[1:] = (dt > 3.0 * med_dt) & (dt > 0.1)
    cut = (hard | colour | residual | local | jump | gap) & ~dup
    cut[0] = False
    res.cut_before = cut
    reasons = []
    for i in np.nonzero(cut)[0]:
        kinds = [name for name, m in (("scene change", hard), ("colour cut", colour),
                                      ("cut", residual), ("local jump", local),
                                      ("jump cut", jump), ("timestamp gap", gap)) if m[i]]
        reasons.append({"source_time_s": round(float(t[i]), 4), "frame": int(i), "type": ", ".join(kinds)})
    res.cut_reason = reasons

    # gradual transitions
    trans = np.zeros(n, bool)
    for a, b in res.dissolve_ranges:
        trans[max(0, int(a)):min(n, int(b) + 1)] = True
    res.transition = trans

    # black / blank frames
    black = ((r["luma"] < 18.0) & (r["contrast"] < 8.0)) | (r["contrast"] < 2.5)
    res.black = black

    # frozen picture: identical frames for >= 1.5 s
    frozen = np.zeros(n, bool)
    still = pix < 0.05
    still[0] = False
    if still.any():
        edges = np.nonzero(np.diff(np.concatenate([[0], still.astype(np.int8), [0]])))[0]
        for a, b in zip(edges[::2], edges[1::2]):
            a0 = max(0, a - 1)  # the frame that started repeating
            if t[b - 1] + durations[b - 1] - t[a0] >= 1.5:
                frozen[a0:b] = True
    res.frozen = frozen

    # eligibility: everything except cut margins, transitions, black and frozen frames
    guard = max(2, int(round(0.06 * fps_eff)))
    cut_zone = np.zeros(n, bool)
    cut_idx = np.nonzero(cut)[0]
    for i in cut_idx:
        cut_zone[max(0, i - guard):min(n, i + guard)] = True
    bad = cut_zone | _dilate(trans | black | frozen, guard, guard)
    res.eligible = ~bad

    # quality scores
    motion = (r["lmag"] + 0.35 * r["gmag"]) * fps_eff  # px / s at analysis size
    m_abs = 1.0 - np.exp(-motion / 25.0)
    res.motion_score = (0.5 * m_abs + 0.5 * _rank(motion)).astype(np.float32)
    res.sharp_score = _rank(np.log1p(r["sharp"])).astype(np.float32)
    luma = r["luma"] / 255.0
    expo = 1.0 - np.minimum(1.0, np.abs(luma - 0.47) / 0.47)
    expo = np.clip(expo - 0.8 * (r["dark"] + r["bright"]), 0.0, 1.0)
    res.exposure_score = expo.astype(np.float32)
    contrast = np.minimum(1.0, r["contrast"] / 55.0)
    q = 0.5 * res.motion_score + 0.2 * res.sharp_score + 0.2 * res.exposure_score + 0.1 * contrast
    res.quality = q.astype(np.float32)
    return res
