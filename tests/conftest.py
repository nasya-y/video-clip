"""Shared fixtures: isolated app-data folder and cached synthetic test videos."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Keep settings/cache/temp/logs of the test run away from the real user profile.
_DATA = Path(tempfile.mkdtemp(prefix="vcm-test-data-"))
os.environ["VCM_DATA_DIR"] = str(_DATA)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vcm.synth import GroundTruth, Shot, make_video, standard_shots  # noqa: E402

_VIDEO_DIR = Path(tempfile.mkdtemp(prefix="vcm-test-videos-"))


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_VIDEO_DIR, ignore_errors=True)
    shutil.rmtree(_DATA, ignore_errors=True)


class VideoFactory:
    """Creates synthetic videos once per test session and returns (path, ground truth)."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, GroundTruth]] = {}

    def get(self, name: str, shots, **kwargs) -> tuple[str, GroundTruth]:
        if name not in self._cache:
            path = _VIDEO_DIR / name
            gt = make_video(str(path), shots, **kwargs)
            self._cache[name] = (str(path), gt)
        return self._cache[name]


@pytest.fixture(scope="session")
def videos() -> VideoFactory:
    return VideoFactory()


# ----------------------------------------------------------------------------- standard sources

def one_comp_shots() -> list[Shot]:
    # 16 + 16 + 18 s with a hard cut and a jump cut -> 4 + 4 + 4 = 12 clips -> exactly 1 compilation
    return [Shot(16, scene=1, camera="pan", sprites=3),
            Shot(16, scene=2, camera="handheld", sprites=4),
            Shot(18, scene=2, camera="static", sprites=3, transition="jump", jump_offset=0.08, time_skip=1.2)]


@pytest.fixture(scope="session")
def short_video(videos):
    return videos.get("short.mp4", [Shot(12, scene=1), Shot(8, scene=2)], width=320, height=180, fps=30, seed=2)


@pytest.fixture(scope="session")
def one_comp_video(videos):
    # non-ASCII characters and spaces in the file name on purpose
    return videos.get("Vidéo d'été 夏 テスト.mp4", one_comp_shots(), width=480, height=270, fps=30, seed=4)


@pytest.fixture(scope="session")
def multi_comp_video(videos):
    return videos.get("multi.mp4", standard_shots(150.0, seed=8, avg_shot=6.5), width=480, height=270, fps=30, seed=8)


def two_shot(total: float = 45.0) -> list[Shot]:
    half = total / 2
    return [Shot(half, scene=3, camera="pan", sprites=4), Shot(total - half, scene=4, camera="handheld", sprites=3)]


def load_report(folder: Path) -> dict:
    return json.loads((folder / "Compilation report.json").read_text(encoding="utf-8"))


def clip_intervals(report: dict) -> list[tuple[int, float, float]]:
    out = []
    for c in report["compilations"]:
        if c.get("status") == "completed":
            for clip in c["clips"]:
                out.append((c["index"], clip["source_start_s"], clip["source_end_s"]))
    return out
