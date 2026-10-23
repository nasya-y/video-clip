"""Locations of bundled resources and per-user application data."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that holds the bundled ``resources`` (and ``ffmpeg`` when frozen)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # src/vcm/paths.py -> repository root
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    return bundle_root().joinpath("resources", *parts)


def font_path() -> Path:
    return resource_path("fonts", "Poppins-Bold.ttf")


def face_model_path() -> Path:
    return resource_path("models", "face_detection_yunet_2023mar.onnx")


def app_data_dir() -> Path:
    """Per-user writable directory (settings, logs, cache, temp)."""
    override = os.environ.get("VCM_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_ID
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_ID
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def log_dir() -> Path:
    d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = app_data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_root() -> Path:
    d = app_data_dir() / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d
