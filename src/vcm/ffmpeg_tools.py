"""Thin, cancellable wrappers around the bundled FFmpeg / FFprobe executables."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

from .paths import bundle_root

log = logging.getLogger(__name__)

_WIN = sys.platform == "win32"
_CREATE_NO_WINDOW = 0x08000000


class FFmpegError(RuntimeError):
    """FFmpeg/FFprobe failed; ``details`` holds the tail of its log."""

    def __init__(self, message: str, details: str = ""):
        super().__init__(message)
        self.details = details


class FFmpegNotFound(FFmpegError):
    pass


class Cancelled(Exception):
    """Raised when the user cancels processing."""


class CancelToken:
    """Thread-safe cancellation flag that also kills registered child processes."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            _kill(p)

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
        if self._event.is_set():
            _kill(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


# --------------------------------------------------------------------------- locate tools

def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("VCM_FFMPEG_DIR")
    if env:
        dirs.append(Path(env))
    root = bundle_root()
    dirs += [root / "ffmpeg" / "bin", root / "ffmpeg", root / "vendor" / "ffmpeg" / "bin"]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        dirs += [exe_dir / "ffmpeg" / "bin", exe_dir / "ffmpeg", exe_dir / "_internal" / "ffmpeg" / "bin"]
    return dirs


_tool_cache: dict[str, str] = {}


def find_tool(name: str) -> str:
    """Return the full path of ``ffmpeg`` or ``ffprobe`` (bundled copy preferred)."""
    if name in _tool_cache:
        return _tool_cache[name]
    exe = name + (".exe" if _WIN else "")
    for d in _candidate_dirs():
        p = d / exe
        if p.is_file():
            _tool_cache[name] = str(p)
            return str(p)
    found = shutil.which(name)
    if found:
        _tool_cache[name] = found
        return found
    raise FFmpegNotFound(
        f"{name} was not found. The application package seems incomplete - please reinstall it."
    )


def ffmpeg() -> str:
    return find_tool("ffmpeg")


def ffprobe() -> str:
    return find_tool("ffprobe")


def popen(cmd: Sequence[str], cancel: Optional[CancelToken] = None, **kwargs) -> subprocess.Popen:
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if _WIN:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
    log.debug("exec: %s", subprocess.list2cmdline(list(cmd)))
    proc = subprocess.Popen(list(cmd), **kwargs)
    if cancel is not None:
        cancel.register(proc)
    return proc


def run(cmd: Sequence[str], cancel: Optional[CancelToken] = None, timeout: Optional[float] = None,
        binary: bool = False) -> subprocess.CompletedProcess:
    """Run a command to completion, honouring cancellation. Raises Cancelled if cancelled."""
    proc = popen(cmd, cancel, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    waited = 0.0
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                waited += 0.25
                if cancel is not None and cancel.cancelled:
                    _kill(proc)
                    proc.communicate()
                    raise Cancelled()
                if timeout is not None and waited > timeout:
                    _kill(proc)
                    proc.communicate()
                    raise FFmpegError(f"Command timed out after {timeout:.0f}s: {Path(cmd[0]).name}")
    finally:
        if cancel is not None:
            cancel.unregister(proc)
    if cancel is not None and cancel.cancelled:
        raise Cancelled()
    if not binary:
        out = out.decode("utf-8", "replace")
    return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err.decode("utf-8", "replace"))


def tail(text: str, lines: int = 12) -> str:
    rows = [r for r in text.replace("\r", "\n").splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


_version_cache: dict[str, str] = {}


def ffmpeg_version() -> str:
    if "v" not in _version_cache:
        try:
            r = run([ffmpeg(), "-hide_banner", "-version"], timeout=30)
            _version_cache["v"] = r.stdout.splitlines()[0].strip() if r.stdout else "unknown"
        except Exception as exc:  # pragma: no cover - diagnostic only
            _version_cache["v"] = f"unknown ({exc})"
    return _version_cache["v"]


_filter_cache: Optional[set[str]] = None


def available_filters() -> set[str]:
    global _filter_cache
    if _filter_cache is None:
        names: set[str] = set()
        try:
            r = run([ffmpeg(), "-hide_banner", "-filters"], timeout=30)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and re.fullmatch(r"[A-Z.|]{2,4}", parts[0]):
                    names.add(parts[1])
        except Exception:  # pragma: no cover
            pass
        _filter_cache = names
    return _filter_cache


# --------------------------------------------------------------------------- probing

def _frac(value) -> Fraction:
    try:
        if value in (None, "", "0/0", "N/A"):
            return Fraction(0)
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _float(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "N/A"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class MediaInfo:
    path: str
    size_bytes: int
    duration: float
    format_name: str
    start_time: float
    # video
    has_video: bool
    video_codec: str = ""
    coded_width: int = 0
    coded_height: int = 0
    rotation: int = 0
    sar: Fraction = Fraction(1)
    avg_frame_rate: Fraction = Fraction(0)
    r_frame_rate: Fraction = Fraction(0)
    nb_frames: Optional[int] = None
    pix_fmt: str = ""
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""
    color_range: str = ""
    field_order: str = ""
    video_stream_index: int = 0
    video_duration: float = 0.0
    # audio
    has_audio: bool = False
    audio_codec: str = ""
    audio_sample_rate: int = 0
    audio_channels: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def fps(self) -> Fraction:
        """Best guess of the nominal frame rate."""
        avg, rr = self.avg_frame_rate, self.r_frame_rate
        if avg > 0 and avg < 1000:
            return avg
        if rr > 0 and rr < 1000:
            return rr
        return Fraction(30)

    @property
    def maybe_vfr(self) -> bool:
        avg, rr = self.avg_frame_rate, self.r_frame_rate
        if avg <= 0 or rr <= 0:
            return True
        return abs(float(avg) - float(rr)) / float(rr) > 0.01

    @property
    def display_size(self) -> tuple[int, int]:
        """Width/height as displayed: square pixels, rotation applied."""
        w, h = self.coded_width, self.coded_height
        if self.sar and self.sar > 0 and self.sar != 1:
            w = int(round(w * float(self.sar)))
        if abs(self.rotation) % 180 == 90:
            w, h = h, w
        return w, h

    @property
    def interlaced(self) -> bool:
        return self.field_order in ("tt", "bb", "tb", "bt")

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in ("smpte2084", "arib-std-b67")

    def summary(self) -> dict:
        w, h = self.display_size
        return {
            "container": self.format_name,
            "duration_s": round(self.duration, 3),
            "size_bytes": self.size_bytes,
            "video": {
                "codec": self.video_codec,
                "width": w,
                "height": h,
                "coded_width": self.coded_width,
                "coded_height": self.coded_height,
                "rotation": self.rotation,
                "frame_rate": f"{self.fps.numerator}/{self.fps.denominator}",
                "frame_rate_float": round(float(self.fps), 4),
                "variable_frame_rate_suspected": self.maybe_vfr,
                "pixel_format": self.pix_fmt,
                "color_transfer": self.color_transfer or None,
                "hdr": self.is_hdr,
                "interlaced": self.interlaced,
            } if self.has_video else None,
            "audio": {
                "codec": self.audio_codec,
                "sample_rate": self.audio_sample_rate,
                "channels": self.audio_channels,
            } if self.has_audio else None,
        }


def probe(path: str | os.PathLike, cancel: Optional[CancelToken] = None) -> MediaInfo:
    path = str(path)
    if not os.path.isfile(path):
        raise FFmpegError(f"File not found: {path}")
    cmd = [ffprobe(), "-v", "error", "-show_format", "-show_streams", "-of", "json", path]
    r = run(cmd, cancel, timeout=120)
    if r.returncode != 0:
        raise FFmpegError("This file could not be read as a video (FFprobe failed).", tail(r.stderr))
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError("FFprobe returned unreadable information.", str(exc)) from exc
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and not (s.get("disposition") or {}).get("attached_pic")]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    info = MediaInfo(
        path=path,
        size_bytes=os.path.getsize(path),
        duration=_float(fmt.get("duration")),
        format_name=str(fmt.get("format_name", "")),
        start_time=_float(fmt.get("start_time")),
        has_video=bool(vstreams),
        raw=data,
    )
    if vstreams:
        v = vstreams[0]
        info.video_codec = str(v.get("codec_name", ""))
        info.video_stream_index = int(v.get("index", 0))
        info.field_order = str(v.get("field_order", "") or "")
        info.coded_width = int(v.get("width") or 0)
        info.coded_height = int(v.get("height") or 0)
        sar = _frac(v.get("sample_aspect_ratio", "1:1").replace(":", "/"))
        info.sar = sar if sar > 0 else Fraction(1)
        info.avg_frame_rate = _frac(v.get("avg_frame_rate"))
        info.r_frame_rate = _frac(v.get("r_frame_rate"))
        nb = v.get("nb_frames")
        info.nb_frames = int(nb) if nb not in (None, "", "N/A") and str(nb).isdigit() else None
        info.pix_fmt = str(v.get("pix_fmt", ""))
        info.color_space = str(v.get("color_space", "") or "")
        info.color_transfer = str(v.get("color_transfer", "") or "")
        info.color_primaries = str(v.get("color_primaries", "") or "")
        info.color_range = str(v.get("color_range", "") or "")
        info.video_duration = _float(v.get("duration"), info.duration)
        rot = 0
        tags = v.get("tags") or {}
        if "rotate" in tags:
            rot = int(_float(tags.get("rotate")))
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(_float(sd.get("rotation")))
        info.rotation = rot
        if info.duration <= 0:
            info.duration = info.video_duration
    if astreams:
        a = astreams[0]
        info.has_audio = True
        info.audio_codec = str(a.get("codec_name", ""))
        info.audio_sample_rate = int(_float(a.get("sample_rate"), 0))
        info.audio_channels = int(a.get("channels") or 0)
    return info


# --------------------------------------------------------------------------- frame reading

_SHOWINFO_CFG = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
_SHOWINFO_FRAME = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+|NOPTS)\s+pts_time:(\S+)")
_DECODE_ERROR = re.compile(
    r"(error while decoding|Invalid data found|corrupt|concealing \d+ (DC|AC|MV) errors|missing picture)",
    re.IGNORECASE,
)


def even(value: float) -> int:
    v = int(round(value))
    return max(2, v - (v % 2))


def analysis_size(display_w: int, display_h: int, max_side: int = 192) -> tuple[int, int]:
    """Small analysis resolution with the same aspect ratio (even dimensions)."""
    if display_w <= 0 or display_h <= 0:
        return max_side, even(max_side * 9 / 16)
    if display_w >= display_h:
        return even(max_side), even(max_side * display_h / display_w)
    return even(max_side * display_w / display_h), even(max_side)


# Containers without a seek index: FFmpeg estimates seek positions from the bit rate, which
# can land a second or more away from the requested frame (MPEG transport/program streams
# such as camcorder .MTS/.M2TS and .VOB files, and raw elementary streams).
UNINDEXED_FORMATS = {"mpegts", "mpeg", "mpegvideo", "h264", "hevc"}


def has_seek_index(info: MediaInfo) -> bool:
    return not (set(info.format_name.split(",")) & UNINDEXED_FORMATS)


def remux_to_matroska(info: MediaInfo, dest: str, cancel: Optional[CancelToken] = None) -> MediaInfo:
    """Copy the video and the first audio stream into Matroska, without re-encoding.

    Matroska has a seek index, so every later seek lands exactly on the requested frame.
    Audio that Matroska cannot hold as it is gets converted losslessly to FLAC."""
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-fflags", "+genpts",
           "-i", info.path, "-map", f"0:{info.video_stream_index}"]
    if info.has_audio:
        cmd += ["-map", "0:a:0"]
    details = ""
    for codecs in (["-c", "copy"], ["-c:v", "copy", "-c:a", "flac"]):
        r = run(cmd + codecs + ["-f", "matroska", dest], cancel)
        if r.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return probe(dest, cancel)
        details = tail(r.stderr)
    raise FFmpegError("The video could not be copied into a seekable container.", details)


class FrameReader:
    """Stream decoded frames (optionally scaled/cropped) together with exact timestamps.

    Timestamps are in seconds on the same zero-based timeline FFmpeg uses for ``-ss``
    seeking, which keeps analysis and rendering consistent, including for
    variable-frame-rate sources.
    """

    def __init__(self, path: str, width: int, height: int, *, pix_fmt: str = "bgr24",
                 start: Optional[float] = None, duration: Optional[float] = None,
                 crop: Optional[tuple[int, int, int, int]] = None, scale: bool = True,
                 pre_filters: Optional[str] = None, cancel: Optional[CancelToken] = None,
                 color_matrix: Optional[str] = None, stream: Optional[int] = None):
        self.path = path
        self.width = width
        self.height = height
        self.channels = {"bgr24": 3, "rgb24": 3, "gray": 1}[pix_fmt]
        self.frame_bytes = width * height * self.channels
        self.cancel = cancel
        filters = []
        if pre_filters:
            filters.append(pre_filters)
        if color_matrix:
            filters.append(f"scale=in_color_matrix={color_matrix}")
        if crop is not None:
            cw, ch, cx, cy = crop
            # convert first: on 4:2:0 video FFmpeg rounds odd crop sizes/offsets to even values
            filters.append(f"format={pix_fmt},crop={cw}:{ch}:{cx}:{cy}:exact=1")
        if scale:
            filters.append(f"scale={width}:{height}:flags=area")
        filters.append("showinfo=checksum=0")
        cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info"]
        if start is not None:
            cmd += ["-ss", f"{max(0.0, start):.6f}"]
        if duration is not None:
            cmd += ["-t", f"{duration:.6f}"]
        vmap = f"0:{stream}" if stream is not None else "0:v:0"
        cmd += ["-i", path, "-map", vmap, "-an", "-sn", "-dn", "-vf", ",".join(filters),
                "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", pix_fmt, "pipe:1"]
        self._cmd = cmd
        self._proc: Optional[subprocess.Popen] = None
        self._ts: list[float] = []
        self._cond = threading.Condition()
        self._stderr_done = False
        self._tail: deque[str] = deque(maxlen=40)
        self.decode_errors = 0
        self._tb: Optional[Fraction] = None
        self.frames_read = 0

    # -- stderr parsing
    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace")
                if "showinfo" in line:
                    if self._tb is None:
                        m = _SHOWINFO_CFG.search(line)
                        if m and int(m.group(2)) > 0:
                            self._tb = Fraction(int(m.group(1)), int(m.group(2)))
                            continue
                    m = _SHOWINFO_FRAME.search(line)
                    if m:
                        t = self._parse_time(m.group(2), m.group(3))
                        with self._cond:
                            self._ts.append(t)
                            self._cond.notify_all()
                        continue
                    continue
                if _DECODE_ERROR.search(line):
                    self.decode_errors += 1
                self._tail.append(line.rstrip())
        finally:
            with self._cond:
                self._stderr_done = True
                self._cond.notify_all()

    def _parse_time(self, pts: str, pts_time: str) -> float:
        if pts != "NOPTS" and self._tb is not None:
            return float(int(pts) * self._tb)
        try:
            return float(pts_time)
        except ValueError:
            return self._ts[-1] if self._ts else 0.0

    def _timestamp(self, index: int) -> float:
        with self._cond:
            while len(self._ts) <= index and not self._stderr_done:
                self._cond.wait(0.2)
                if self.cancel is not None and self.cancel.cancelled:
                    raise Cancelled()
            if len(self._ts) > index:
                return self._ts[index]
        # Timestamp line missing (should not happen) - extrapolate.
        if len(self._ts) >= 2:
            step = (self._ts[-1] - self._ts[0]) / (len(self._ts) - 1)
            return self._ts[-1] + step * (index - len(self._ts) + 1)
        return index / 30.0

    def __iter__(self) -> Iterator[tuple[int, float, np.ndarray]]:
        self._proc = popen(self._cmd, self.cancel, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           bufsize=0)
        reader = threading.Thread(target=self._read_stderr, daemon=True)
        reader.start()
        stdout = self._proc.stdout
        assert stdout is not None
        idx = 0
        try:
            while True:
                if self.cancel is not None and self.cancel.cancelled:
                    raise Cancelled()
                buf = _read_exact(stdout, self.frame_bytes)
                if buf is None:
                    break
                arr = np.frombuffer(buf, dtype=np.uint8)
                if self.channels == 1:
                    frame = arr.reshape(self.height, self.width)
                else:
                    frame = arr.reshape(self.height, self.width, self.channels)
                t = self._timestamp(idx)
                self.frames_read = idx + 1
                yield idx, t, frame
                idx += 1
            self._proc.wait()
            reader.join(timeout=5)
            if self.cancel is not None and self.cancel.cancelled:
                raise Cancelled()
            if self._proc.returncode not in (0, None) and idx == 0:
                raise FFmpegError("FFmpeg could not decode the video stream.", self.log_tail())
        finally:
            self.close()

    def close(self) -> None:
        if self._proc is not None:
            _kill(self._proc)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            for s in (self._proc.stdout, self._proc.stderr):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            if self.cancel is not None:
                self.cancel.unregister(self._proc)

    @property
    def returncode(self) -> Optional[int]:
        return None if self._proc is None else self._proc.returncode

    def log_tail(self) -> str:
        return "\n".join(list(self._tail)[-12:])


def _read_exact(stream, n: int) -> Optional[bytes]:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


def read_frames(path: str, width: int, height: int, **kwargs) -> tuple[np.ndarray, np.ndarray]:
    """Decode everything into memory (only for short ranges): returns (times, frames)."""
    reader = FrameReader(path, width, height, **kwargs)
    times, frames = [], []
    for _, t, f in reader:
        times.append(t)
        frames.append(f.copy())
    if not frames:
        shape = (0, height, width) if reader.channels == 1 else (0, height, width, reader.channels)
        return np.zeros(0), np.zeros(shape, np.uint8)
    return np.asarray(times, dtype=np.float64), np.stack(frames)


def read_audio(path: str, sample_rate: int = 16000, start: Optional[float] = None,
               duration: Optional[float] = None, cancel: Optional[CancelToken] = None) -> np.ndarray:
    """Decode the first audio stream to mono float32 samples."""
    cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.6f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(sample_rate),
            "-f", "f32le", "pipe:1"]
    r = run(cmd, cancel, timeout=600, binary=True)
    if r.returncode != 0:
        raise FFmpegError("Could not decode audio.", tail(r.stderr))
    return np.frombuffer(r.stdout, dtype=np.float32).copy()
