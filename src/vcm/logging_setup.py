"""Logging to a rotating file in the user's app-data folder (and the console when present)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

from .paths import log_dir

_configured = False


def setup_logging(verbose: bool = False, extra_file: Optional[str] = None) -> Path:
    global _configured
    path = log_dir() / "app.log"
    root = logging.getLogger()
    if _configured:
        return path
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if extra_file:
        xf = logging.FileHandler(extra_file, encoding="utf-8")
        xf.setLevel(logging.DEBUG)
        xf.setFormatter(fmt)
        root.addHandler(xf)
    if sys.stderr is not None:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(ch)
    for noisy in ("PIL",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
    return path
