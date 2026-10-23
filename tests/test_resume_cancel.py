"""Cancellation, resuming, footage-reuse prevention across runs and error recovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from conftest import clip_intervals, load_report
from vcm.analysis import analyze_video
from vcm.ffmpeg_tools import ffmpeg, probe
from vcm.paths import temp_root
from vcm.pipeline import Events, Pipeline
from vcm.selection import plan_compilations
from vcm.settings import Settings


class CancelAfter(Events):
    """Cancels the run when a condition on the events is met."""

    def __init__(self, pipe_ref: list, after_compilations: int = 1, during_stage: str | None = None):
        self.pipe_ref = pipe_ref
        self.after = after_compilations
        self.during = during_stage
        self.done = 0
        self.fired = False

    def compilation_done(self, info):
        self.done += 1
        if self.during is None and self.done >= self.after and not self.fired:
            self.fired = True
            self.pipe_ref[0].cancel.cancel()

    def progress(self, overall, stage):
        if self.during and self.during in stage and not self.fired:
            self.fired = True
            self.pipe_ref[0].cancel.cancel()


def expected_compilations(path: str) -> int:
    an = analyze_video(probe(path))
    s = Settings()
    return len(plan_compilations(an, s.speed, s.clip_duration, s.clips_per_compilation).compilations)


def no_partial_files(folder: Path) -> bool:
    return not [p for p in folder.rglob("*") if p.name.endswith((".partial", ".tmp"))]


def no_temp_leftovers() -> bool:
    return not any(p.is_dir() for p in temp_root().glob("run-*"))


def assert_ledger_clean(rep: dict):
    statuses = {e["status"] for e in rep["allocation_ledger"]}
    assert statuses <= {"used", "rejected", "released"}, statuses
    iv = sorted(clip_intervals(rep), key=lambda r: r[1])
    for (_, a0, b0), (_, a1, b1) in zip(iv, iv[1:]):
        assert a1 >= b0 - 1e-4, "footage reused"


def test_cancel_between_compilations_then_resume(multi_comp_video, tmp_path):
    path, _ = multi_comp_video
    M = expected_compilations(path)
    assert M >= 2
    ref: list = [None]
    pipe = Pipeline(Settings(), str(tmp_path), CancelAfter(ref, after_compilations=1))
    ref[0] = pipe
    s = pipe.run([path])
    assert s.cancelled
    folder = tmp_path / "multi"
    files = sorted(p.name for p in folder.glob("*.mp4"))
    assert files == ["Compilation 1.mp4"]
    assert no_partial_files(folder) and no_temp_leftovers()
    rep = load_report(folder)
    assert rep["status"] == "cancelled"
    assert_ledger_clean(rep)
    size_before = (folder / "Compilation 1.mp4").stat().st_size

    # resume: same settings -> the remaining compilations, numbering continues
    s2 = Pipeline(Settings(), str(tmp_path)).run([path])
    assert not s2.cancelled and not s2.errors, s2.errors
    files = sorted((p.name for p in folder.glob("*.mp4")), key=lambda n: int(n.split()[1].split(".")[0]))
    assert files == [f"Compilation {i}.mp4" for i in range(1, M + 1)], "resume must reach the maximum count"
    assert (folder / "Compilation 1.mp4").stat().st_size == size_before, "completed files must not change"
    rep = load_report(folder)
    assert rep["status"] == "complete" and rep["compilations_completed"] == M
    assert_ledger_clean(rep)
    assert len(rep["runs"]) == 2 and rep["runs"][1]["resumed"]

    # a third run finds nothing more to do and creates nothing
    s3 = Pipeline(Settings(), str(tmp_path)).run([path])
    assert s3.compilations_created == [] and not s3.errors


def test_cancel_while_rendering_discards_partial_output(one_comp_video, tmp_path):
    path, _ = one_comp_video
    ref: list = [None]
    pipe = Pipeline(Settings(), str(tmp_path), CancelAfter(ref, during_stage="rendering"))
    ref[0] = pipe
    s = pipe.run([path])
    assert s.cancelled
    folder = tmp_path / Path(path).stem
    assert list(folder.glob("*.mp4")) == []
    assert no_partial_files(folder) and no_temp_leftovers()
    rep = load_report(folder)
    assert rep["status"] == "cancelled"
    assert not [e for e in rep["allocation_ledger"] if e["status"] == "planned"]
    # processing again afterwards works normally
    s2 = Pipeline(Settings(), str(tmp_path)).run([path])
    assert len(s2.compilations_created) == 1 and not s2.errors


def test_changed_settings_never_reuse_completed_footage(multi_comp_video, tmp_path):
    path, _ = multi_comp_video
    ref: list = [None]
    pipe = Pipeline(Settings(), str(tmp_path), CancelAfter(ref, after_compilations=1))
    ref[0] = pipe
    pipe.run([path])
    first = load_report(tmp_path / "multi")
    used = [(a, b) for _, a, b in clip_intervals(first)]
    assert len(used) == 10
    # continue with different clip length and ordering
    s = Settings(clip_duration=2.5, clips_per_compilation=6, distribution="sequential")
    out = Pipeline(s, str(tmp_path)).run([path])
    assert not out.errors, out.errors
    rep = load_report(tmp_path / "multi")
    assert rep["compilations_completed"] >= 2
    names = sorted(c["filename"] for c in rep["compilations"] if c["status"] == "completed")
    assert "Compilation 1.mp4" in names and "Compilation 2.mp4" in names
    later = [(a, b) for idx, a, b in clip_intervals(rep) if idx > 1]
    for a, b in later:
        for ua, ub in used:
            assert b <= ua + 1e-4 or a >= ub - 1e-4, "footage of compilation 1 was reused"
    assert_ledger_clean(rep)


def _make_bad_inputs(d: Path) -> list[Path]:
    d.mkdir(parents=True, exist_ok=True)
    corrupt = d / "corrupt.mp4"
    corrupt.write_bytes(np.random.default_rng(0).integers(0, 256, 200_000, dtype=np.uint8).tobytes())
    empty = d / "empty.mov"
    empty.write_bytes(b"")
    audio_only = d / "audio only.m4a"
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=5", "-c:a", "aac", str(audio_only)], check=True)
    return [corrupt, empty, audio_only]


def test_bad_files_are_reported_and_processing_continues(one_comp_video, tmp_path):
    bad = _make_bad_inputs(tmp_path / "in")
    good = tmp_path / "in" / "good clip.mp4"
    shutil.copy(one_comp_video[0], good)
    s = Pipeline(Settings(), str(tmp_path / "out")).run([str(p) for p in bad] + [str(good)])
    assert len(s.compilations_created) == 1
    joined = "\n".join(s.errors)
    for p in bad:
        assert p.name in joined
    assert s.videos == 4 and s.videos_ok == 1
    # originals untouched
    assert good.stat().st_size == Path(one_comp_video[0]).stat().st_size


def test_truncated_stream_is_handled(multi_comp_video, tmp_path):
    ts = tmp_path / "broken.ts"
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", multi_comp_video[0], "-c", "copy",
                    "-f", "mpegts", str(ts)], check=True)
    data = ts.read_bytes()
    ts.write_bytes(data[: int(len(data) * 0.6)])  # cut off the end
    s = Pipeline(Settings(), str(tmp_path / "out")).run([str(ts)])
    assert not s.cancelled
    rep = load_report(tmp_path / "out" / "broken")
    assert rep["status"] in ("complete", "no_compilations")
    for c in rep["compilations"]:
        if c["status"] == "completed":
            assert c["verification"]["passed"]


def test_existing_files_without_report_are_protected(one_comp_video, tmp_path):
    path, _ = one_comp_video
    folder = tmp_path / Path(path).stem
    folder.mkdir()
    (folder / "Compilation 1.mp4").write_bytes(b"user file")
    s = Pipeline(Settings(), str(tmp_path)).run([path])
    assert s.compilations_created == [] and s.errors
    assert "report" in s.errors[0]
    assert (folder / "Compilation 1.mp4").read_bytes() == b"user file"


def test_same_name_different_video_gets_own_folder(one_comp_video, short_video, tmp_path):
    a = tmp_path / "a" / "clip.mp4"
    b = tmp_path / "b" / "clip.mp4"
    a.parent.mkdir()
    b.parent.mkdir()
    shutil.copy(one_comp_video[0], a)
    shutil.copy(short_video[0], b)
    out = tmp_path / "out"
    Pipeline(Settings(), str(out)).run([str(a)])
    Pipeline(Settings(), str(out)).run([str(b)])
    assert (out / "clip" / "Compilation report.json").exists()
    assert (out / "clip (2)" / "Compilation report.json").exists()
    assert load_report(out / "clip")["source"]["path"] != load_report(out / "clip (2)")["source"]["path"]


# ----------------------------------------------------------------------------- failure recovery paths

def test_failed_verification_replaces_footage(one_comp_video, tmp_path, monkeypatch):
    """A compilation that fails verification is never kept; its bad clip is replaced by other footage."""
    import vcm.pipeline as pl
    from vcm.verify import VerificationResult
    real = pl.verify_compilation
    calls = {"n": 0}
    rejected = {}

    def flaky(job, out_path, style, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            rejected["interval"] = (job.clips[3].source_start, job.clips[3].source_end)
            return VerificationResult(passed=False, bad_clips=[3], problems=["simulated cut inside clip 4"])
        return real(job, out_path, style, *a, **kw)

    monkeypatch.setattr(pl, "verify_compilation", flaky)
    path, _ = one_comp_video
    s = Pipeline(Settings(), str(tmp_path)).run([path])
    folder = tmp_path / Path(path).stem
    rep = load_report(folder)
    assert not s.errors, s.errors
    assert rep["compilations_completed"] == 1, "12 clips fit, so one rejected clip can be replaced"
    a, b = rejected["interval"]
    for _, x, y in clip_intervals(rep):
        assert y <= a + 1e-4 or x >= b - 1e-4, "rejected footage was used again"
    assert any(e["status"] == "rejected" for e in rep["allocation_ledger"])
    assert any("rejected by verification" in sec["reason"] for sec in rep["skipped_sections"])
    comp = rep["compilations"][0]
    assert comp["attempts"] and comp["verification"]["passed"]
    assert sorted(p.name for p in folder.glob("*.mp4")) == ["Compilation 1.mp4"]


def test_unreadable_watermark_is_restyled_and_rerendered(one_comp_video, tmp_path, monkeypatch):
    import vcm.pipeline as pl
    from vcm.verify import VerificationResult
    real = pl.verify_compilation
    styles = []

    def strict(job, out_path, style, *a, **kw):
        styles.append(style)
        if len(styles) == 1:
            return VerificationResult(passed=False, watermark_failed=True, problems=["simulated low contrast"])
        return real(job, out_path, style, *a, **kw)

    monkeypatch.setattr(pl, "verify_compilation", strict)
    path, _ = one_comp_video
    s = Pipeline(Settings(), str(tmp_path)).run([path])
    assert not s.errors
    assert len(styles) == 2
    assert styles[1].fill_name != styles[0].fill_name or styles[1].outline_px > styles[0].outline_px
    rep = load_report(tmp_path / Path(path).stem)
    assert rep["compilations"][0]["watermark"]["colour_name"] == styles[1].fill_name


def test_systematic_render_failure_does_not_reject_footage(one_comp_video, tmp_path, monkeypatch):
    import vcm.pipeline as pl
    from vcm.ffmpeg_tools import FFmpegError

    def broken(*a, **kw):
        raise FFmpegError("simulated encoder failure", "details")

    monkeypatch.setattr(pl, "render", broken)
    path, _ = one_comp_video
    s = Pipeline(Settings(), str(tmp_path)).run([path])
    assert s.compilations_created == [] and s.errors
    rep = load_report(tmp_path / Path(path).stem)
    assert not [e for e in rep["allocation_ledger"] if e["status"] == "rejected"], "footage must stay usable"
    assert rep["errors"]
    # after the problem is gone, the same footage is still available
    monkeypatch.undo()
    s2 = Pipeline(Settings(), str(tmp_path)).run([path])
    assert len(s2.compilations_created) == 1 and not s2.errors
