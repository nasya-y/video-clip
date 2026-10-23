"""Per-video JSON report, which doubles as the persistent footage allocation ledger."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from . import APP_NAME, __version__
from .selection import OVERLAP_TOL as EPS

log = logging.getLogger(__name__)

REPORT_NAME = "Compilation report.json"
REPORT_VERSION = 1
_COMP_RE = re.compile(r"^Compilation (\d+)\.mp4$", re.IGNORECASE)


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def compilation_name(index: int) -> str:
    return f"Compilation {index}.mp4"


def existing_compilation_files(folder: Path) -> dict[int, Path]:
    out = {}
    if folder.is_dir():
        for p in folder.iterdir():
            m = _COMP_RE.match(p.name)
            if m and p.is_file():
                out[int(m.group(1))] = p
    return out


def safe_folder_name(stem: str) -> str:
    """Folder name for a video: its file name without extension, made Windows-safe."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip().rstrip(".")
    if not name:
        name = "video"
    if name.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = f"{name}_"
    return name[:120]


class Report:
    """Holds the report dict and writes it atomically after every change."""

    def __init__(self, folder: Path, data: Optional[dict] = None):
        self.folder = folder
        self.path = folder / REPORT_NAME
        self.data: dict[str, Any] = data or {}

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, folder: Path) -> Optional["Report"]:
        path = folder / REPORT_NAME
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Unreadable report %s: %s", path, exc)
            return None
        if not isinstance(data, dict) or "source" not in data:
            return None
        return cls(folder, data)

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = now_iso()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ ledger helpers
    @property
    def fingerprint(self) -> Optional[str]:
        return (self.data.get("source") or {}).get("fingerprint")

    def compilations(self) -> list[dict]:
        return self.data.setdefault("compilations", [])

    def completed(self) -> list[dict]:
        return [c for c in self.compilations() if c.get("status") == "completed"]

    def used_intervals(self) -> list[tuple[float, float]]:
        """Source intervals that may never be used again (completed or rejected footage)."""
        out = []
        for e in self.data.get("allocation_ledger", []):
            if e.get("status") in ("used", "rejected"):
                out.append((float(e["source_start_s"]), float(e["source_end_s"])))
        return out

    def next_index(self) -> int:
        """Lowest number after all completed compilations (a failed number is reused)."""
        used = [int(c["index"]) for c in self.completed()]
        files = existing_compilation_files(self.folder)
        return max(used + list(files.keys()) + [0]) + 1

    def init_new(self, source: dict, settings: dict) -> None:
        self.data = {
            "report_version": REPORT_VERSION,
            "app": {"name": APP_NAME, "version": __version__},
            "created_at": now_iso(),
            "status": "in_progress",
            "source": source,
            "settings": settings,
            "allocation_ledger": [],
            "compilations": [],
            "skipped_sections": [],
            "warnings": [],
            "errors": [],
            "runs": [],
        }

    def add_warning(self, msg: str) -> None:
        w = self.data.setdefault("warnings", [])
        if msg not in w:
            w.append(msg)

    def add_error(self, msg: str) -> None:
        self.data.setdefault("errors", []).append({"time": now_iso(), "message": msg})

    def drop_planned(self) -> None:
        """Forget allocations of compilations that were planned but never completed."""
        self.data["allocation_ledger"] = [e for e in self.data.get("allocation_ledger", [])
                                          if e.get("status") in ("used", "rejected")]
        self.data["compilations"] = [c for c in self.compilations() if c.get("status") in ("completed", "failed")]

    def comp(self, index: int) -> Optional[dict]:
        for c in self.compilations():
            if int(c.get("index", -1)) == index:
                return c
        return None

    def record_planned(self, index: int, clips: list[dict], attempt: int = 1) -> None:
        self.data.setdefault("allocation_ledger", []).extend(
            {"compilation": index, "attempt": attempt, "clip": i + 1, "source_start_s": c["source_start_s"],
             "source_end_s": c["source_end_s"], "timeline_start_s": c["timeline_start_s"],
             "timeline_end_s": c["timeline_end_s"], "source_start_frame": c["source_start_frame"],
             "status": "planned"}
            for i, c in enumerate(clips))
        comp = self.comp(index)
        if comp is None:
            comp = {"index": index, "filename": compilation_name(index), "attempts": []}
            self.compilations().append(comp)
            self.compilations().sort(key=lambda c: int(c.get("index", 0)))
        comp["status"] = "planned"
        comp["clips"] = clips

    def mark_completed(self, index: int, entry: dict) -> None:
        comp = self.comp(index)
        if comp is None:
            comp = {"index": index, "attempts": []}
            self.compilations().append(comp)
        comp.update(entry)
        comp["status"] = "completed"
        for e in self.data.get("allocation_ledger", []):
            if e.get("compilation") == index and e.get("status") == "planned":
                e["status"] = "used"

    def record_failed_attempt(self, index: int, reason: str, details: Optional[dict] = None,
                              rejected: Optional[list[tuple[float, float]]] = None) -> None:
        """A rendered attempt failed verification: release its footage, reject bad intervals."""
        comp = self.comp(index)
        if comp is not None:
            comp.setdefault("attempts", []).append({"time": now_iso(), "reason": reason,
                                                    "clips": comp.get("clips"), "details": details})
        bad = rejected or []
        for e in self.data.get("allocation_ledger", []):
            if e.get("compilation") == index and e.get("status") == "planned":
                s0 = float(e["source_start_s"])
                if any(abs(s0 - a) < EPS for a, _ in bad):
                    e["status"] = "rejected"
                    e["reason"] = reason
                else:
                    e["status"] = "released"
        for a, b in bad:
            self.data.setdefault("skipped_sections", []).append(
                {"source_start_s": round(a, 4), "source_end_s": round(b, 4),
                 "reason": f"rejected by verification of compilation {index}: {reason}"})

    def mark_failed(self, index: int, reason: str) -> None:
        comp = self.comp(index)
        if comp is not None:
            comp["status"] = "failed"
            comp["failure_reason"] = reason
        for e in self.data.get("allocation_ledger", []):
            if e.get("compilation") == index and e.get("status") == "planned":
                e["status"] = "released"
