"""FFmpeg render graph for one compilation.

Every clip is read straight from the original file with an accurate seek to its exact
start frame, sped up once (``setpts=PTS/speed`` for video, ``atempo`` for audio),
conformed to a constant output frame rate and cut to an exact number of frames and
audio samples.  The clips are joined with the ``concat`` filter, the watermark PNG is
overlaid for the full duration, and the result is encoded once to H.264 + AAC.
There is no intermediate lossy step and the original file is only ever read.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import __version__, APP_NAME
from .analysis import AnalysisResult
from .ffmpeg_tools import (CancelToken, Cancelled, FFmpegError, MediaInfo, available_filters, even,
                           ffmpeg, popen, tail, _read_exact)
from .selection import Clip
from .settings import QUALITY_PRESETS, Settings

log = logging.getLogger(__name__)

STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                  Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(100),
                  Fraction(120000, 1001), Fraction(120)]
AUDIO_RATE = 48000


_VALID_TAGS = {
    "colorspace": {"bt709", "smpte170m", "bt470bg", "bt2020nc", "fcc", "smpte240m"},
    "color_primaries": {"bt709", "smpte170m", "bt470bg", "bt2020", "smpte240m", "bt470m"},
    "color_trc": {"bt709", "smpte170m", "bt470bg", "gamma22", "gamma28", "smpte240m", "bt2020-10", "iec61966-2-1"},
    "color_range": {"tv"},
}


@dataclass
class OutputFormat:
    width: int
    height: int
    fps: Fraction
    needs_scale: bool
    colorspace: str          # matrix used to draw the watermark: bt709 | smpte170m
    hdr_tonemap: bool
    audio: bool
    audio_channels: int
    notes: list
    color_tags: dict = None  # colour metadata copied from the source (empty when it has none)
    glass: bool = False      # fluted-glass effect over the picture (under the watermark)

    def to_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height,
            "frame_rate": f"{self.fps.numerator}/{self.fps.denominator}",
            "frame_rate_float": round(float(self.fps), 4),
            "colorspace": self.colorspace, "colour_tags": dict(self.color_tags or {}),
            "hdr_tonemapped": self.hdr_tonemap,
            "glass_effect": self.glass,
            "audio": self.audio, "audio_channels": self.audio_channels if self.audio else 0,
            "audio_sample_rate": AUDIO_RATE if self.audio else None,
            "notes": list(self.notes),
        }


def _snap_rate(fps: float) -> Fraction:
    best = min(STANDARD_RATES, key=lambda r: abs(float(r) - fps))
    if abs(float(best) - fps) / fps < 0.015:
        return best
    return Fraction(fps).limit_denominator(1001)


def output_format(info: MediaInfo, an: AnalysisResult, settings: Settings) -> OutputFormat:
    notes = []
    w, h = info.display_size
    ow, oh = even(w), even(h)
    if (ow, oh) != (w, h):
        notes.append(f"dimensions rounded to even numbers for H.264 ({w}x{h} -> {ow}x{oh})")
    needs_scale = info.sar != 1 or (ow, oh) != (w, h)
    if info.sar != 1:
        notes.append("non-square pixels converted to square pixels (same display aspect ratio)")
    # frame rate: nominal rate, or the measured median rate for variable-frame-rate files
    measured = 1.0 / float(np.median(an.durations)) if an.n > 1 else float(info.fps)
    nominal = float(info.fps)
    if info.maybe_vfr or abs(measured - nominal) / max(nominal, 1.0) > 0.03:
        base = measured
        notes.append(f"variable frame rate source; output conformed to constant {_snap_rate(base)} fps")
    else:
        base = nominal
    fps = _snap_rate(base)
    if float(fps) > settings.max_fps + 0.01:
        k = int(np.ceil(float(fps) / settings.max_fps - 1e-9))
        fps = _snap_rate(float(fps) / k)
        notes.append(f"frame rate reduced to {float(fps):.3f} fps (maximum {settings.max_fps})")
    hdr = info.is_hdr and "zscale" in available_filters() and "tonemap" in available_filters()
    if info.is_hdr and not hdr:
        notes.append("HDR source but tone mapping filters unavailable - colours may look flat")
    if info.interlaced:
        notes.append("interlaced source deinterlaced (bwdif)")
    if hdr:
        cs = "bt709"
        notes.append("HDR source tone-mapped to SDR BT.709")
    elif info.color_space in ("smpte170m", "bt470bg"):
        cs = "smpte170m"
    elif info.color_space == "bt709" or oh >= 720 or ow >= 1280:
        cs = "bt709"
    else:
        cs = "smpte170m"
    channels = 0
    if info.has_audio:
        channels = 1 if info.audio_channels == 1 else 2
    # keep the source's colour metadata so players show the output exactly like the source
    if hdr:
        tags = {"colorspace": "bt709", "color_primaries": "bt709", "color_trc": "bt709", "color_range": "tv"}
    else:
        src = {"colorspace": info.color_space, "color_primaries": info.color_primaries,
               "color_trc": info.color_transfer, "color_range": info.color_range}
        tags = {k: v for k, v in src.items() if v in _VALID_TAGS[k]}
    return OutputFormat(ow, oh, fps, needs_scale, cs, hdr, info.has_audio, channels, notes, tags,
                        glass=bool(getattr(settings, "glass_effect", False)))


# Fluted (reeded) glass: vertical ribs, each acting as a cylindrical lens that shows a
# squeezed copy of the picture around it, softened and shaded at the rib edges.
GLASS_RIBS = 34            # ribs across the width
GLASS_SQUEEZE = 2.2        # each rib shows this many rib widths of the picture behind it


def glass_chain(w: int, h: int, head: str, tail: str) -> str:
    """Filter text applying the glass effect to the stream given by ``head``.

    ``head`` is the input label (optionally followed by filters ending in a comma) and
    ``tail`` the output label ("" leaves the last chain open for more filters).  The lens
    maps are computed once from single-frame sources, so the cost per frame is a blur and
    a pixel lookup."""
    rib = max(6, round(w / GLASS_RIBS))
    sig = max(1.0, w / 140.0)
    c = f"(trunc(X/{rib})*{rib}+{rib}/2)"
    xmap = f"clip({c}+{GLASS_SQUEEZE}*(X-{c}),0,{w - 1})"
    u = f"(mod(X,{rib})/{rib})"
    light = f"0.10*exp(-pow(({u}-0.32)/0.10,2))"
    shade = f"0.22*(exp(-pow({u}/0.12,2))+exp(-pow((1-{u})/0.12,2)))"
    one = f"color=c=black:s={w}x{h}:r=1:d=1"
    return (f"{head}format=yuv444p,gblur=sigma={sig:.2f}:sigmaV={1.6 * sig:.2f}[gl0];"
            f"{one},format=gray16le,geq=lum='{xmap}'[glx];"
            f"{one},format=gray16le,geq=lum='Y'[gly];"
            f"color=c=black@0:s={w}x{h}:r=1:d=1,format=yuva444p,"
            f"geq=lum='if(gt({light},{shade}),255,0)':cb=128:cr=128:a='255*max({light},{shade})'[gls];"
            f"[gl0][glx][gly]remap=fill=black[gl1];[gl1][gls]overlay=format=yuv444,format=yuv420p{tail}")


def frame_boundaries(n_clips: int, clip_duration: float, fps: Fraction) -> list[int]:
    """Cumulative output frame index at each clip boundary (no drift across clips)."""
    return [int(round(k * clip_duration * fps)) for k in range(n_clips + 1)]


def sample_boundaries(frames: Sequence[int], fps: Fraction) -> list[int]:
    return [int(round(Fraction(b) / fps * AUDIO_RATE)) for b in frames]


def _atempo_chain(speed: float) -> str:
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.10g}")
    return ",".join(parts)


@dataclass
class RenderJob:
    info: MediaInfo
    an: AnalysisResult
    clips: list[Clip]
    speed: float
    clip_duration: float
    fmt: OutputFormat

    @property
    def boundaries(self) -> list[int]:
        return frame_boundaries(len(self.clips), self.clip_duration, self.fmt.fps)

    @property
    def total_frames(self) -> int:
        return self.boundaries[-1]

    @property
    def duration(self) -> float:
        return float(Fraction(self.total_frames) / self.fmt.fps)

    def seek_points(self) -> list[tuple[float, float]]:
        """(seek time, offset of the clip's first frame after the seek) per clip."""
        out = []
        t = self.an.t
        for c in self.clips:
            ts = float(t[c.start_idx])
            if c.start_idx > 0:
                prev = float(t[c.start_idx - 1])
                seek = ts - 0.5 * min(ts - prev, 0.5)
            else:
                seek = max(0.0, ts - 0.0005)
            seek = max(0.0, seek)
            out.append((seek, ts - seek))
        return out

    def input_args(self) -> list[str]:
        args = []
        L = self.clip_duration * self.speed
        for seek, _off in self.seek_points():
            args += ["-ss", f"{seek:.6f}", "-t", f"{L + 1.0:.6f}", "-i", self.info.path]
        return args

    def picture_filters(self) -> list[str]:
        """Filters that change how source pictures look (deinterlacing, HDR tone mapping).

        Used by the renderer and by verification, so that the source is compared with the
        output exactly as it was transformed."""
        parts = []
        if self.info.interlaced:
            parts.append("bwdif=mode=send_frame:deint=interlaced")
        if self.fmt.hdr_tonemap:
            parts += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                      "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]
        return parts

    def match_filters(self) -> str:
        """Filters that turn source pictures into what the output shows (for verification)."""
        if not self.fmt.glass:
            return ",".join(self.picture_filters())
        head = "[in]" + "".join(f + "," for f in self.picture_filters()
                                + [f"scale={self.fmt.width}:{self.fmt.height}:flags=lanczos", "setsar=1"])
        return glass_chain(self.fmt.width, self.fmt.height, head, "")

    def _video_chain(self, i: int, n_frames: int) -> str:
        fmt = self.fmt
        parts = self.picture_filters()
        parts += ["settb=AVTB", f"setpts=(PTS-STARTPTS)/{self.speed:.10g}",
                  f"fps={fmt.fps.numerator}/{fmt.fps.denominator}", f"trim=end_frame={n_frames}",
                  "setpts=PTS-STARTPTS"]
        if fmt.needs_scale:
            parts.append(f"scale={fmt.width}:{fmt.height}:flags=lanczos")
        parts += ["setsar=1", "format=yuv420p"]
        return f"[{i}:{self.info.video_stream_index}]" + ",".join(parts) + f"[v{i}]"

    def _audio_chain(self, i: int, offset: float, n_samples: int) -> str:
        L = self.clip_duration * self.speed
        layout = "mono" if self.fmt.audio_channels == 1 else "stereo"
        parts = [f"atrim=start={offset:.6f}:duration={L:.6f}", "asetpts=PTS-STARTPTS",
                 _atempo_chain(self.speed), f"aresample={AUDIO_RATE}",
                 f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts={layout}",
                 f"apad=whole_len={n_samples}", f"atrim=end_sample={n_samples}", "asetpts=PTS-STARTPTS"]
        return f"[{i}:a:0]" + ",".join(parts) + f"[a{i}]"

    def filter_graph(self, watermark: Optional[tuple[int, int, int]] = None,
                     preview: Optional[tuple[int, int, float]] = None) -> tuple[str, str, Optional[str]]:
        """Return (filter_complex, video label, audio label).

        ``watermark`` = (input index, x, y); ``preview`` = (width, height, fps) for
        low-resolution analysis frames (no audio, no watermark).
        """
        fb = self.boundaries
        sb = sample_boundaries(fb, self.fmt.fps)
        n = len(self.clips)
        chains = []
        use_audio = self.fmt.audio and preview is None
        for i, (_seek, off) in enumerate(self.seek_points()):
            chains.append(self._video_chain(i, fb[i + 1] - fb[i]))
            if use_audio:
                chains.append(self._audio_chain(i, off, sb[i + 1] - sb[i]))
        if use_audio:
            pads = "".join(f"[v{i}][a{i}]" for i in range(n))
            chains.append(f"{pads}concat=n={n}:v=1:a=1[cv][ca]")
        else:
            pads = "".join(f"[v{i}]" for i in range(n))
            chains.append(f"{pads}concat=n={n}:v=1:a=0[cv]")
        vout = "cv"
        if self.fmt.glass:
            chains.append(glass_chain(self.fmt.width, self.fmt.height, "[cv]", "[cvg]"))
            vout = "cvg"
        if preview is not None:
            pw, ph, pf = preview
            chains.append(f"[{vout}]fps={pf:.10g},scale={pw}:{ph}:flags=area,format=rgb24[pv]")
            vout = "pv"
        elif watermark is not None:
            idx, x, y = watermark
            matrix = "bt709" if self.fmt.colorspace == "bt709" else "smpte170m"
            chains.append(f"[{idx}:v]scale=out_color_matrix={matrix}:out_range=tv,format=yuva444p[wm]")
            chains.append(f"[{vout}][wm]overlay=x={x}:y={y}:format=yuv444:eof_action=repeat,format=yuv420p[vout]")
            vout = "vout"
        return ";".join(chains), vout, ("ca" if use_audio else None)


_PROGRESS = re.compile(r"out_time_(?:us|ms)=(\d+)")


def _color_args(fmt: OutputFormat) -> list[str]:
    args = []
    for k, v in (fmt.color_tags or {}).items():
        args += [f"-{k}", v]
    return args


def render(job: RenderJob, watermark_png: str, wm_xy: tuple[int, int], out_path: str, settings: Settings,
           title: str, cancel: Optional[CancelToken] = None,
           progress: Optional[Callable[[float], None]] = None) -> list[str]:
    """Encode the compilation to ``out_path`` (MP4). Returns the FFmpeg command for the log."""
    q = QUALITY_PRESETS[settings.quality]
    n = len(job.clips)
    graph, vlabel, alabel = job.filter_graph(watermark=(n, wm_xy[0], wm_xy[1]))
    fps = job.fmt.fps
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    cmd += job.input_args()
    cmd += ["-i", watermark_png]
    cmd += ["-filter_complex", graph, "-map", f"[{vlabel}]"]
    if alabel:
        cmd += ["-map", f"[{alabel}]"]
    cmd += ["-c:v", "libx264", "-preset", q["preset"], "-crf", str(q["crf"]), "-profile:v", "high",
            "-pix_fmt", "yuv420p", "-g", str(max(1, int(round(float(fps) * 2)))),
            "-r", f"{fps.numerator}/{fps.denominator}", "-fps_mode", "cfr"]
    cmd += _color_args(job.fmt)
    if alabel:
        cmd += ["-c:a", "aac", "-b:a", q["audio_bitrate"], "-ar", str(AUDIO_RATE), "-ac", str(job.fmt.audio_channels)]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1", "-metadata", f"title={title}",
            "-metadata", f"comment=Created with {APP_NAME} {__version__}",
            "-movflags", "+faststart", "-f", "mp4", out_path]
    total_us = job.duration * 1e6
    proc = popen(cmd, cancel, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_chunks: list[bytes] = []

    def _drain_err():
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            err_chunks.append(chunk)

    t_err = threading.Thread(target=_drain_err, daemon=True)
    t_err.start()
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            if cancel is not None and cancel.cancelled:
                break
            m = _PROGRESS.search(raw.decode("ascii", "ignore"))
            if m and progress and total_us > 0:
                progress(min(1.0, int(m.group(1)) / total_us))
        proc.wait()
        t_err.join(timeout=5)
    finally:
        if cancel is not None:
            cancel.unregister(proc)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if cancel is not None and cancel.cancelled:
        raise Cancelled()
    if proc.returncode != 0:
        raise FFmpegError("FFmpeg failed while rendering the compilation.",
                          tail(b"".join(err_chunks).decode("utf-8", "replace")))
    return cmd


def preview_frames(job: RenderJob, width: int, fps: float = 4.0,
                   cancel: Optional[CancelToken] = None) -> tuple[np.ndarray, np.ndarray]:
    """Low-resolution RGB frames of the assembled compilation (before watermarking).

    Returns (frames (N, h, w, 3) uint8, clip index of every frame).
    """
    pw = even(min(width, job.fmt.width))
    ph = even(job.fmt.height * pw / job.fmt.width)
    graph, vlabel, _ = job.filter_graph(preview=(pw, ph, fps))
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats"]
    cmd += job.input_args()
    cmd += ["-filter_complex", graph, "-map", f"[{vlabel}]", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    proc = popen(cmd, cancel, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_chunks: list[bytes] = []
    t_err = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)  # type: ignore[union-attr]
    t_err.start()
    frames = []
    size = pw * ph * 3
    try:
        while True:
            if cancel is not None and cancel.cancelled:
                raise Cancelled()
            buf = _read_exact(proc.stdout, size)
            if buf is None:
                break
            frames.append(np.frombuffer(buf, np.uint8).reshape(ph, pw, 3))
        proc.wait()
        t_err.join(timeout=5)
    finally:
        if cancel is not None:
            cancel.unregister(proc)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if cancel is not None and cancel.cancelled:
        raise Cancelled()
    if proc.returncode != 0 or not frames:
        raise FFmpegError("FFmpeg failed while preparing preview frames.",
                          tail(b"".join(err_chunks).decode("utf-8", "replace")))
    arr = np.stack(frames)
    fb = job.boundaries
    times = np.arange(len(arr)) / fps
    bounds_t = np.array([float(Fraction(b) / job.fmt.fps) for b in fb])
    clip_ids = np.clip(np.searchsorted(bounds_t, times, side="right") - 1, 0, len(job.clips) - 1)
    return arr, clip_ids
