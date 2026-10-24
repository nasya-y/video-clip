"""User settings with the required defaults, validation and JSON persistence."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from .paths import settings_file

log = logging.getLogger(__name__)

DEFAULT_WATERMARK_TEXT = "onlyfans.com/nyraishtara"

QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    # label, x264 crf, x264 preset, aac bitrate
    "maximum": {"label": "Maximum", "crf": 16, "preset": "slow", "audio_bitrate": "256k"},
    "high": {"label": "High (default)", "crf": 18, "preset": "medium", "audio_bitrate": "192k"},
    "balanced": {"label": "Balanced", "crf": 21, "preset": "medium", "audio_bitrate": "160k"},
    "compact": {"label": "Compact", "crf": 24, "preset": "fast", "audio_bitrate": "128k"},
}

WATERMARK_SIZES: dict[str, dict[str, Any]] = {
    # font size as a fraction of the shorter frame side
    "medium": {"label": "Medium", "font_frac": 0.050},
    "large": {"label": "Large (default)", "font_frac": 0.060},
    "xlarge": {"label": "Extra large", "font_frac": 0.072},
}

WATERMARK_POSITIONS: dict[str, str] = {
    "auto": "Automatic",
    "top-left": "Top left",
    "top-center": "Top centre",
    "top-right": "Top right",
    "bottom-left": "Bottom left",
    "bottom-center": "Bottom centre",
    "bottom-right": "Bottom right",
}

CUT_SENSITIVITY: dict[str, str] = {
    "normal": "Normal",
    "high": "High (default)",
    "maximum": "Maximum (strictest)",
}

DISTRIBUTIONS: dict[str, str] = {
    "spread": "Spread across the whole video (default)",
    "best_first": "Best clips in the first compilations",
    "sequential": "Consecutive sections of the video",
}


@dataclass
class Settings:
    # processing rules
    speed: float = 1.3
    clip_duration: float = 3.0
    clips_per_compilation: int = 10
    quality: str = "high"
    max_fps: int = 60
    cut_sensitivity: str = "high"
    distribution: str = "spread"
    # watermark
    watermark_text: str = DEFAULT_WATERMARK_TEXT
    watermark_size: str = "large"
    watermark_position: str = "auto"
    watermark_color_mode: str = "auto"  # "auto" | "custom"
    watermark_custom_color: str = "#39FF14"
    # fluted-glass look over the picture (the watermark stays on top, sharp)
    glass_effect: bool = False
    # remembered UI state
    input_paths: list[str] = field(default_factory=list)
    output_dir: str = ""
    last_browse_dir: str = ""

    @property
    def compilation_duration(self) -> float:
        return self.clip_duration * self.clips_per_compilation

    @property
    def source_clip_duration(self) -> float:
        """Length of original footage consumed by one clip (before the speed-up)."""
        return self.clip_duration * self.speed

    def validated(self) -> "Settings":
        s = Settings(**asdict(self))
        s.speed = _clamp(_num(s.speed, 1.3), 0.25, 4.0)
        s.clip_duration = _clamp(_num(s.clip_duration, 3.0), 0.5, 60.0)
        s.clips_per_compilation = int(_clamp(_num(s.clips_per_compilation, 10), 1, 100))
        s.max_fps = int(_clamp(_num(s.max_fps, 60), 10, 120))
        s.glass_effect = s.glass_effect is True
        if s.quality not in QUALITY_PRESETS:
            s.quality = "high"
        if s.cut_sensitivity not in CUT_SENSITIVITY:
            s.cut_sensitivity = "high"
        if s.distribution not in DISTRIBUTIONS:
            s.distribution = "spread"
        if s.watermark_size not in WATERMARK_SIZES:
            s.watermark_size = "large"
        if s.watermark_position not in WATERMARK_POSITIONS:
            s.watermark_position = "auto"
        if s.watermark_color_mode not in ("auto", "custom"):
            s.watermark_color_mode = "auto"
        if not isinstance(s.watermark_custom_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", s.watermark_custom_color):
            s.watermark_custom_color = "#39FF14"
        if not isinstance(s.watermark_text, str):
            s.watermark_text = DEFAULT_WATERMARK_TEXT
        s.watermark_text = s.watermark_text.strip()
        if not isinstance(s.input_paths, list):
            s.input_paths = []
        s.input_paths = [str(p) for p in s.input_paths if isinstance(p, str)]
        s.output_dir = str(s.output_dir or "")
        s.last_browse_dir = str(s.last_browse_dir or "")
        return s

    def problems(self) -> list[str]:
        """Human readable reasons why processing cannot start with these settings."""
        out = []
        if not self.watermark_text:
            out.append("The watermark text is empty.")
        return out

    def processing_dict(self) -> dict[str, Any]:
        """Settings that matter for the output (stored in each report)."""
        q = QUALITY_PRESETS[self.quality]
        return {
            "playback_speed": self.speed,
            "clip_duration_s": self.clip_duration,
            "clips_per_compilation": self.clips_per_compilation,
            "compilation_duration_s": round(self.compilation_duration, 6),
            "source_footage_per_clip_s": round(self.source_clip_duration, 6),
            "quality": self.quality,
            "video_codec": "H.264 (libx264)",
            "x264_crf": q["crf"],
            "x264_preset": q["preset"],
            "audio_codec": "AAC",
            "audio_bitrate": q["audio_bitrate"],
            "max_output_fps": self.max_fps,
            "cut_sensitivity": self.cut_sensitivity,
            "clip_distribution": self.distribution,
            "watermark_text": self.watermark_text,
            "watermark_size": self.watermark_size,
            "watermark_position": self.watermark_position,
            "watermark_color_mode": self.watermark_color_mode,
            "watermark_custom_color": self.watermark_custom_color if self.watermark_color_mode == "custom" else None,
            "glass_effect": self.glass_effect,
        }

    def plan_key(self) -> dict[str, Any]:
        """Settings that, when changed, invalidate a previously stored clip plan."""
        return {
            "speed": round(self.speed, 6),
            "clip_duration": round(self.clip_duration, 6),
            "clips_per_compilation": self.clips_per_compilation,
            "cut_sensitivity": self.cut_sensitivity,
            "distribution": self.distribution,
        }

    def reset_processing_defaults(self) -> "Settings":
        """Defaults for everything except remembered folders."""
        d = Settings()
        d.input_paths = list(self.input_paths)
        d.output_dir = self.output_dir
        d.last_browse_dir = self.last_browse_dir
        return d


def _num(v, default):
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_settings(path: Optional[Path] = None) -> Settings:
    path = path or settings_file()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        log.warning("Could not read settings (%s); using defaults", exc)
        return Settings()
    known = {f.name for f in fields(Settings)}
    kwargs = {k: v for k, v in (data or {}).items() if k in known}
    try:
        return Settings(**kwargs).validated()
    except TypeError:
        return Settings()


def save_settings(settings: Settings, path: Optional[Path] = None) -> None:
    path = Path(path or settings_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings.validated()), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
