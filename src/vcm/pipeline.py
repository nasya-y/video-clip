"""Processing orchestration: probe -> analyse -> plan -> (preview, watermark, render, verify)*.

The pipeline is UI-independent; the GUI runs it in a background thread and receives
events through the :class:`Events` callbacks.  Completed compilations are written to
a temporary file first, verified, and only then moved into place under their final
name, so cancelling or a crash never leaves a half-written ``Compilation N.mp4``.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
from PIL import Image

from . import APP_NAME, __version__
from .analysis import ANALYSIS_MAX_SIDE, AnalysisResult, analyze_video, file_fingerprint
from .ffmpeg_tools import CancelToken, Cancelled, FFmpegError, ffmpeg_version, probe
from .paths import cache_dir, temp_root
from .render import RenderJob, output_format, preview_frames, render
from .report import (REPORT_NAME, Report, compilation_name, existing_compilation_files, now_iso,
                     safe_folder_name)
from .selection import Clip, check_plan_invariants, plan_compilations
from .settings import Settings
from .verify import verify_compilation
from .watermark import WatermarkStyle, choose_style, make_style, place, render_overlay

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".webm", ".mts", ".m2ts", ".ts",
                    ".3gp", ".flv", ".mpg", ".mpeg", ".vob", ".ogv", ".mxf", ".dv"}

SPEED_METHOD = (
    "The playback speed is applied to the entire source timeline before any clip is selected: every "
    "frame time is divided by the speed factor and clips are chosen as exact {D:g}-second windows on "
    "this sped-up timeline ({L:g} s of original footage each). Each selected window is then rendered "
    "from the original file with the speed change applied exactly once (video setpts=PTS/{S:g}, audio "
    "atempo={S:g}). No intermediate sped-up copy is encoded, which avoids an extra generation of quality "
    "loss; the result is identical to cutting the windows from a fully sped-up copy."
)


class Events:
    """Callbacks used by the pipeline. Override what you need (all are optional)."""

    def log(self, level: int, message: str) -> None: ...
    def progress(self, overall: float, stage: str) -> None: ...
    def video_started(self, index: int, count: int, path: str) -> None: ...
    def compilation_done(self, info: dict) -> None: ...
    def video_finished(self, summary: dict) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass
class RunSummary:
    videos: int = 0
    videos_ok: int = 0
    compilations_created: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    video_summaries: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    output_dir: str = ""


def collect_videos(paths: Sequence[str]) -> list[str]:
    """Expand folders (top level only) into video files; skip this app's own output folders."""
    out: list[str] = []
    seen = set()
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            if (pp / REPORT_NAME).exists():
                continue
            for f in sorted(pp.iterdir(), key=lambda x: x.name.lower()):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                    key = os.path.normcase(str(f.resolve()))
                    if key not in seen:
                        seen.add(key)
                        out.append(str(f))
        elif pp.is_file():
            key = os.path.normcase(str(pp.resolve()))
            if key not in seen:
                seen.add(key)
                out.append(str(pp))
    return out


class TempWorkspace:
    """Managed temporary directory for one processing run (cleaned up afterwards)."""

    def __init__(self) -> None:
        self.cleanup_stale()
        self.path = temp_root() / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.path.mkdir(parents=True, exist_ok=True)

    def file(self, name: str) -> str:
        return str(self.path / name)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    @staticmethod
    def cleanup_stale(max_age_h: float = 12.0) -> None:
        root = temp_root()
        now = time.time()
        for d in root.glob("run-*"):
            try:
                if d.is_dir() and now - d.stat().st_mtime > max_age_h * 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


class _Progress:
    def __init__(self, events: Events, n_videos: int):
        self.events = events
        self.n = max(1, n_videos)
        self.video = 0
        self.last_emit = 0.0

    def set(self, video_frac: float, stage: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_emit < 0.1:
            return
        self.last_emit = now
        overall = (self.video + max(0.0, min(1.0, video_frac))) / self.n
        self.events.progress(overall, stage)


class Pipeline:
    def __init__(self, settings: Settings, output_dir: str, events: Optional[Events] = None,
                 cancel: Optional[CancelToken] = None):
        self.settings = settings.validated()
        self.output_dir = Path(output_dir)
        self.events = events or Events()
        self.cancel = cancel or CancelToken()

    # ------------------------------------------------------------------ logging helpers
    def _log(self, msg: str, level: int = logging.INFO) -> None:
        log.log(level, msg)
        try:
            self.events.log(level, msg)
        except Exception:  # pragma: no cover - UI callback errors must not stop processing
            pass

    # ------------------------------------------------------------------ main entry
    def run(self, inputs: Sequence[str]) -> RunSummary:
        summary = RunSummary(output_dir=str(self.output_dir))
        videos = collect_videos(inputs)
        summary.videos = len(videos)
        if not videos:
            summary.errors.append("No video files were found in the selection.")
            self.events.error(summary.errors[-1])
            return summary
        problems = self.settings.problems()
        if problems:
            summary.errors.extend(problems)
            for p in problems:
                self.events.error(p)
            return summary
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ws = TempWorkspace()
        prog = _Progress(self.events, len(videos))
        self._log(f"{APP_NAME} {__version__} - {len(videos)} video(s), output: {self.output_dir}")
        try:
            self._log(f"Using {ffmpeg_version()}")
        except Exception:
            pass
        try:
            for vi, path in enumerate(videos):
                prog.video = vi
                self.events.video_started(vi, len(videos), path)
                try:
                    vs = self.process_video(path, ws, prog)
                    summary.video_summaries.append(vs)
                    summary.compilations_created.extend(vs.get("new_compilations", []))
                    if vs.get("report"):
                        summary.reports.append(vs["report"])
                    if vs.get("ok"):
                        summary.videos_ok += 1
                    else:
                        for e in vs.get("errors", []):
                            summary.errors.append(f"{Path(path).name}: {e}")
                    self.events.video_finished(vs)
                except Cancelled:
                    summary.cancelled = True
                    self._log("Processing cancelled by the user.")
                    break
                except Exception as exc:  # keep going with the next video
                    msg = f"{Path(path).name}: {exc}"
                    details = getattr(exc, "details", "")
                    log.error("Video failed: %s\n%s\n%s", path, details, traceback.format_exc())
                    summary.errors.append(msg)
                    self.events.error(msg + (f"\n{details}" if details else ""))
                    vs = {"video": path, "ok": False, "errors": [str(exc)], "new_compilations": []}
                    summary.video_summaries.append(vs)
                    self.events.video_finished(vs)
            if not summary.cancelled:
                prog.video = len(videos)
                prog.set(0.0, "Finished", force=True)
        finally:
            ws.cleanup()
        return summary

    # ------------------------------------------------------------------ one video
    def _resolve_folder(self, src: str, fingerprint: str) -> tuple[Path, Optional[Report]]:
        base = safe_folder_name(Path(src).stem)
        for n in range(1, 1000):
            folder = self.output_dir / (base if n == 1 else f"{base} ({n})")
            if not folder.exists():
                return folder, None
            rep = Report.load(folder)
            if rep is not None:
                if rep.fingerprint == fingerprint:
                    return folder, rep
                continue  # belongs to a different video with the same name
            if existing_compilation_files(folder):
                raise RuntimeError(
                    f"The folder '{folder}' already contains compilation files but no '{REPORT_NAME}'. "
                    "To guarantee that no footage is reused, this video was skipped. Move those files "
                    "elsewhere (or restore the report) and start again.")
            return folder, None
        raise RuntimeError("Could not find a free output folder name.")

    def process_video(self, path: str, ws: TempWorkspace, prog: _Progress) -> dict:
        st = self.settings
        name = Path(path).name
        cancel = self.cancel
        result: dict = {"video": path, "ok": False, "errors": [], "new_compilations": [], "report": None}
        prog.set(0.0, f"{name}: reading file information", force=True)
        try:
            info = probe(path, cancel)
        except FFmpegError as exc:
            raise RuntimeError(f"{exc} {exc.details}".strip()) from exc
        if not info.has_video:
            raise RuntimeError("The file has no video stream.")
        if info.duration <= 0:
            self._log(f"{name}: duration unknown, it will be measured during analysis", logging.WARNING)
        fingerprint = file_fingerprint(path)
        folder, report = self._resolve_folder(path, fingerprint)
        resumed = report is not None
        source_info = {"filename": name, "path": str(Path(path).resolve()), "fingerprint": fingerprint,
                       **info.summary()}
        if report is None:
            report = Report(folder)
            report.init_new(source_info, st.processing_dict())
        else:
            report.data["source"].update(source_info)
            report.data["settings"] = st.processing_dict()
            report.drop_planned()
            self._log(f"{name}: continuing from the existing report ({len(report.completed())} compilation(s) already done)")
        result["report"] = str(report.path)
        run_entry = {"started_at": now_iso(), "settings": st.processing_dict(), "resumed": resumed}
        report.data.setdefault("runs", []).append(run_entry)
        report.data["app"] = {"name": APP_NAME, "version": __version__, "ffmpeg": ffmpeg_version()}
        report.data["status"] = "in_progress"

        # ---- analysis (35% of the per-video progress)
        self._log(f"{name}: analysing the whole video ({info.duration:.1f} s)")
        t0 = time.monotonic()
        an = analyze_video(info, cancel, progress=lambda f: prog.set(0.35 * f, f"{name}: analysing footage {int(f * 100)}%"),
                           sensitivity=st.cut_sensitivity)
        self._log(f"{name}: analysis done in {time.monotonic() - t0:.1f} s - {an.n} frames, "
                  f"{len(an.cut_times())} cuts, {an.eligible_seconds():.1f} s eligible")
        if getattr(an, "decode_warnings", 0):
            report.add_warning(f"The decoder reported {an.decode_warnings} warnings while reading the source "
                               "(the file may be slightly damaged).")
        S, D, N = st.speed, st.clip_duration, st.clips_per_compilation
        report.data["playback_speed_applied"] = S
        report.data["speed_method"] = SPEED_METHOD.format(D=D, L=round(D * S, 6), S=S)
        report.data["timeline"] = {
            "source_duration_s": round(an.end_time, 4),
            "sped_up_duration_s": round(an.end_time / S, 4),
            "clip_window_timeline_s": D,
            "clip_window_source_s": round(D * S, 6),
        }
        report.data["analysis"] = {
            "frames_analysed": an.n,
            "analysis_resolution": [an.width, an.height],
            "measured_frame_rate": round(1.0 / float(np.median(an.durations)), 4) if an.n > 1 else None,
            "cut_sensitivity": st.cut_sensitivity,
            "cuts_detected": len(an.cut_reason),
            "cuts": an.cut_reason,
            "gradual_transitions": [
                {"source_start_s": round(float(an.t[a]), 4),
                 "source_end_s": round(float(an.t[min(b, an.n - 1)] + an.durations[min(b, an.n - 1)]), 4)}
                for a, b in _merge_ranges(an.dissolve_ranges)],
            "eligible_footage_source_s": round(an.eligible_seconds(), 4),
            "eligible_footage_timeline_s": round(an.eligible_seconds() / S, 4),
            "method": "full decode at reduced resolution; HSV + pixel difference, DIS optical flow and "
                      "flow-compensated residual with adaptive thresholds; blend test for dissolves/fades",
        }
        report.data["skipped_sections"] = [s for s in report.data.get("skipped_sections", [])
                                           if "rejected by verification" in s.get("reason", "")]
        report.data["skipped_sections"] += an.ineligible_sections()

        fmt = output_format(info, an, st)
        report.data["output_format"] = fmt.to_dict()
        for note in fmt.notes:
            report.add_warning(note)

        # ---- plan
        pending = self._plan(an, report)
        report.save()
        if not pending:
            self._log(f"{name}: not enough eligible footage for a complete compilation")
        try:
            self._render_all(pending, info, an, fmt, report, ws, prog, result, name)
        except Cancelled:
            report.drop_planned()
            report.data["status"] = "cancelled"
            run_entry["cancelled_at"] = now_iso()
            report.save()
            raise

        # ---- final accounting
        self._finalize_report(report, an, run_entry)
        report.save()
        prog.set(1.0, f"{name}: done", force=True)
        n_done = len(report.completed())
        for c in report.compilations():
            if c.get("status") == "failed":
                msg = f"Compilation {c.get('index')}: {c.get('failure_reason', 'failed')}"
                if msg not in result["errors"]:
                    result["errors"].append(msg)
        result.update({
            "ok": not result["errors"],
            "folder": str(folder),
            "compilations_total": n_done,
            "remaining": report.data.get("remaining"),
        })
        self._log(f"{name}: {len(result['new_compilations'])} new compilation(s), {n_done} in total. "
                  f"{report.data['remaining']['message']}")
        return result

    def _render_all(self, pending, info, an, fmt, report: Report, ws: TempWorkspace, prog: _Progress,
                    result: dict, name: str) -> None:
        cancel = self.cancel
        replans = 0
        done = 0
        total = max(1, len(pending))
        consecutive_failures = 0
        while pending:
            cancel.check()
            index, clips = pending.pop(0)
            base_frac = 0.35 + 0.65 * done / total

            def sub(f: float, stage: str, _b=base_frac) -> None:
                prog.set(_b + 0.65 * f / total, f"{name}: compilation {index} - {stage}")

            outcome = self._build_compilation(index, clips, info, an, fmt, report, ws, sub)
            if outcome["status"] == "completed":
                done += 1
                consecutive_failures = 0
                result["new_compilations"].append(outcome["path"])
                self.events.compilation_done(outcome)
                report.save()
                continue
            if outcome["status"] == "replan" and replans < 6:
                replans += 1
                self._log(f"{name}: compilation {index} failed verification ({outcome['reason']}); "
                          "replacing the affected footage and planning again", logging.WARNING)
                pending = self._plan(an, report)
                total = max(total, done + len(pending))
                report.save()
                continue
            # unrecoverable for this compilation
            consecutive_failures += 1
            report.mark_failed(index, outcome.get("reason", "unknown error"))
            report.add_error(f"Compilation {index} attempt could not be completed: {outcome.get('reason')}")
            report.save()
            if consecutive_failures >= 2:
                report.add_error("Stopped processing this video after repeated failures.")
                result["errors"].append(f"compilation {index} failed repeatedly - processing of this video stopped")
                report.drop_planned()
                break
            pending = self._plan(an, report)
            total = max(total, done + len(pending))

    # ------------------------------------------------------------------ planning
    def _plan(self, an: AnalysisResult, report: Report, start_index: Optional[int] = None) -> list[tuple[int, list[Clip]]]:
        st = self.settings
        report.drop_planned()
        used = report.used_intervals()
        plan = plan_compilations(an, st.speed, st.clip_duration, st.clips_per_compilation, st.distribution, used)
        problems = check_plan_invariants(plan.compilations, an, st.speed, st.clip_duration, used)
        if problems:  # should never happen - refuse to render an invalid plan
            raise RuntimeError("Internal planning check failed: " + "; ".join(problems[:3]))
        first = max(report.next_index(), start_index or 0)
        pending = []
        for m, clips in enumerate(plan.compilations):
            idx = first + m
            report.record_planned(idx, [c.to_dict() for c in clips])
            pending.append((idx, clips))
        report.data["plan"] = {
            "max_clips_in_remaining_footage": plan.max_clips,
            "compilations_planned": len(plan.compilations),
            "clips_per_compilation": st.clips_per_compilation,
            "leftover_clips": plan.leftover_clips,
            "footage_excluded_by_ledger_timeline_s": round(plan.excluded_by_ledger_s, 4),
            "distribution": st.distribution,
        }
        return pending

    # ------------------------------------------------------------------ one compilation
    def _build_compilation(self, index: int, clips: list[Clip], info, an: AnalysisResult, fmt, report: Report,
                           ws: TempWorkspace, sub: Callable[[float, str], None]) -> dict:
        st = self.settings
        cancel = self.cancel
        job = RenderJob(info, an, clips, st.speed, st.clip_duration, fmt)
        folder = report.folder
        self._check_disk_space(folder, fmt)
        sub(0.0, "preparing preview")
        frames, clip_ids = preview_frames(job, 480, 4.0, cancel)
        style = choose_style(frames, clip_ids, fmt.width, fmt.height, st.watermark_text, st.watermark_size,
                             st.watermark_position, st.watermark_color_mode, st.watermark_custom_color)
        thumb = self._save_thumbnail(frames, clip_ids, report, index)
        # every interval allocated to any other compilation of this source (done, rejected or planned)
        others = [(float(e["source_start_s"]), float(e["source_end_s"]))
                  for e in report.data.get("allocation_ledger", [])
                  if e.get("status") in ("used", "rejected", "planned") and e.get("compilation") != index]
        tmp_out = ws.file(f"comp-{uuid.uuid4().hex[:8]}.mp4")
        attempts = []
        render_retry_used = False
        excluded_colours: list[str] = []
        outline_scale, plate = 1.0, False
        for attempt in range(1, 5):
            cancel.check()
            png = ws.file(f"wm-{index}-{attempt}.png")
            img, _, _ = render_overlay(style)
            img.save(png)
            t0 = time.monotonic()
            sub(0.1, f"rendering (attempt {attempt})" if attempt > 1 else "rendering")
            try:
                render(job, png, (style.x, style.y), tmp_out, st, f"Compilation {index}", cancel,
                       progress=lambda f: sub(0.1 + 0.6 * f, f"rendering {int(f * 100)}%"))
            except FFmpegError as exc:
                attempts.append({"attempt": attempt, "result": "render error", "error": str(exc), "details": exc.details})
                self._log(f"Render error: {exc}\n{exc.details}", logging.WARNING)
                if not render_retry_used:
                    render_retry_used = True
                    continue
                # repeated render failure: the footage itself may be damaged
                rejected = [(c.source_start, c.source_end) for c in clips]
                report.record_failed_attempt(index, "render failed twice", {"attempts": attempts}, rejected)
                return {"status": "replan", "reason": "render failed twice; footage rejected"}
            render_s = time.monotonic() - t0
            sub(0.7, "verifying")
            ver = verify_compilation(job, tmp_out, style, st.cut_sensitivity, others, cancel,
                                     progress=lambda f: sub(0.7 + 0.29 * f, f"verifying {int(f * 100)}%"))
            if ver.passed:
                final = self._commit(tmp_out, folder, index)
                entry = self._compilation_entry(index, final, job, style, ver, render_s, attempts, thumb)
                report.mark_completed(index, entry)
                return {"status": "completed", "path": str(final), "index": index, "video": info.path,
                        "duration": job.duration, "thumbnail": thumb, "colour": style.fill_name,
                        "position": style.position}
            attempts.append({"attempt": attempt, "result": "verification failed", "problems": ver.problems})
            self._log(f"Compilation {index} attempt {attempt} failed verification: {'; '.join(ver.problems)}",
                      logging.WARNING)
            if ver.bad_clips:
                rejected = [(clips[k].source_start, clips[k].source_end) for k in ver.bad_clips]
                report.record_failed_attempt(index, "clip failed verification", ver.to_dict(), rejected)
                _remove(tmp_out)
                return {"status": "replan", "reason": f"{len(rejected)} clip(s) failed verification"}
            if ver.watermark_failed:
                # make the watermark more visible and render again
                excluded_colours.append(style.fill_name)
                if attempt == 1:
                    outline_scale = 1.5
                else:
                    plate = True
                style = choose_style(frames, clip_ids, fmt.width, fmt.height, st.watermark_text, st.watermark_size,
                                     style.position, st.watermark_color_mode, st.watermark_custom_color,
                                     exclude_colors=excluded_colours if st.watermark_color_mode == "auto" else (),
                                     outline_scale=outline_scale, plate=plate)
                continue
            if ver.render_failed and not render_retry_used:
                render_retry_used = True
                continue
            break
        _remove(tmp_out)
        report.record_failed_attempt(index, "verification failed", {"attempts": attempts})
        return {"status": "failed", "reason": "the compilation did not pass verification after several attempts"}

    def _commit(self, tmp_out: str, folder: Path, index: int) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / compilation_name(index)
        if final.exists():
            raise RuntimeError(f"Refusing to overwrite existing file {final}")
        part = folder / (final.name + ".partial")
        shutil.move(tmp_out, part)
        os.replace(part, final)
        return final

    def _compilation_entry(self, index: int, final: Path, job: RenderJob, style: WatermarkStyle, ver,
                           render_s: float, attempts: list, thumb: Optional[str]) -> dict:
        fb = job.boundaries
        fps = job.fmt.fps
        clips = []
        for k, c in enumerate(job.clips):
            d = c.to_dict()
            d.update({"clip": k + 1, "output_start_s": round(float(fb[k] / fps), 6),
                      "output_end_s": round(float(fb[k + 1] / fps), 6), "output_frames": fb[k + 1] - fb[k]})
            clips.append(d)
        wm = style.report()
        wm["text_height_fraction_of_frame"] = round((style.text_box[3] - style.text_box[1]) / min(job.fmt.width, job.fmt.height), 4)
        return {
            "filename": final.name,
            "path": str(final),
            "created_at": now_iso(),
            "duration_s": round(job.duration, 6),
            "frames": job.total_frames,
            "frame_rate": f"{fps.numerator}/{fps.denominator}",
            "resolution": [job.fmt.width, job.fmt.height],
            "file_size_bytes": final.stat().st_size,
            "clip_count": len(job.clips),
            "clips": clips,
            "watermark": wm,
            "verification": ver.to_dict(),
            "attempts": attempts,
            "render_seconds": round(render_s, 2),
            "thumbnail": thumb,
        }

    def _save_thumbnail(self, frames: np.ndarray, clip_ids: np.ndarray, report: Report, index: int) -> Optional[str]:
        try:
            d = cache_dir() / "thumbs"
            d.mkdir(parents=True, exist_ok=True)
            pick = int(np.nonzero(clip_ids == 0)[0][len(np.nonzero(clip_ids == 0)[0]) // 2]) if (clip_ids == 0).any() else 0
            img = Image.fromarray(frames[pick])
            img.thumbnail((320, 320))
            p = d / f"{(report.fingerprint or 'x')[:16]}-{index}.jpg"
            img.save(p, quality=85)
            return str(p)
        except Exception as exc:  # pragma: no cover - thumbnails are cosmetic
            log.info("thumbnail failed: %s", exc)
            return None

    def _check_disk_space(self, folder: Path, fmt) -> None:
        target = folder if folder.exists() else self.output_dir
        try:
            free = shutil.disk_usage(target).free
            temp_free = shutil.disk_usage(temp_root()).free
        except OSError:
            return
        est = int(self.settings.compilation_duration * (fmt.width * fmt.height * float(fmt.fps)) * 0.2 / 8) + 5_000_000
        need = 3 * est + 100_000_000
        if free < need or temp_free < need:
            raise RuntimeError(f"Not enough free disk space (need about {need // 1_000_000} MB).")

    def _finalize_report(self, report: Report, an: AnalysisResult, run_entry: dict) -> None:
        st = self.settings
        used = report.used_intervals()
        rest = plan_compilations(an, st.speed, st.clip_duration, st.clips_per_compilation, st.distribution, used)
        completed = report.completed()
        k = rest.max_clips
        N = st.clips_per_compilation
        if k >= N:
            msg = (f"{k} more clip(s) could still be cut ({k // N} compilation(s)); some compilations "
                   "failed - see errors.")
        elif k > 0:
            msg = (f"Remaining eligible footage is enough for {k} more clip(s) ({k * st.clip_duration:g} s); "
                   f"{N - k} more would be needed for another complete compilation.")
        else:
            msg = "No further clips can be cut from the remaining eligible footage."
        report.data["remaining"] = {
            "unused_eligible_footage_timeline_s": round(rest.eligible_timeline_s, 4),
            "unused_eligible_footage_source_s": round(rest.eligible_timeline_s * st.speed, 4),
            "additional_clips_possible": k,
            "clips_needed_for_next_compilation": max(0, N - k) if k < N else 0,
            "message": msg,
        }
        report.data["compilations_completed"] = len(completed)
        report.data["compilation_files"] = [c.get("filename") for c in completed]
        failed = [c for c in report.compilations() if c.get("status") == "failed"]
        report.data["status"] = "complete" if not failed else "completed_with_errors"
        if not completed:
            report.data["status"] = "no_compilations" if not failed else "failed"
        run_entry["finished_at"] = now_iso()
        run_entry["compilations_completed_total"] = len(completed)


def _merge_ranges(ranges: np.ndarray) -> list[tuple[int, int]]:
    if len(ranges) == 0:
        return []
    rs = sorted((int(a), int(b)) for a, b in ranges)
    out = [rs[0]]
    for a, b in rs[1:]:
        if a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
