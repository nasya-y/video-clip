"""Command line entry points: batch processing, self-test and GUI smoke test."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from . import APP_NAME, __version__
from .settings import (CUT_SENSITIVITY, DISTRIBUTIONS, QUALITY_PRESETS, WATERMARK_POSITIONS, WATERMARK_SIZES,
                       Settings, load_settings)

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="VideoCompilationMaker", description=f"{APP_NAME} {__version__}")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("--cli", action="store_true", help="process videos without opening the window")
    p.add_argument("-i", "--input", nargs="+", default=[], help="video files and/or folders")
    p.add_argument("-o", "--output", help="output folder")
    p.add_argument("--speed", type=float, help="playback speed (default 1.3)")
    p.add_argument("--clip-duration", type=float, help="seconds of final output per clip (default 3)")
    p.add_argument("--clips", type=int, help="clips per compilation (default 10)")
    p.add_argument("--quality", choices=list(QUALITY_PRESETS), help="output quality (default high)")
    p.add_argument("--watermark-text", help="watermark text")
    p.add_argument("--watermark-size", choices=list(WATERMARK_SIZES))
    p.add_argument("--watermark-position", choices=list(WATERMARK_POSITIONS))
    p.add_argument("--watermark-color", help="'auto' (default) or a colour like #39FF14")
    p.add_argument("--sensitivity", choices=list(CUT_SENSITIVITY), help="cut detection sensitivity")
    p.add_argument("--distribution", choices=list(DISTRIBUTIONS))
    p.add_argument("--glass", choices=["on", "off"], help="fluted-glass effect over the picture")
    p.add_argument("--defaults", action="store_true", help="ignore saved settings and use the defaults")
    p.add_argument("--self-test", metavar="OUTDIR", help="generate a test video, process it and verify the result")
    p.add_argument("--gui-smoke", metavar="PNG", help="open the window, save a screenshot and exit")
    p.add_argument("--log-file", help="additional log file")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def settings_from_args(args: argparse.Namespace) -> Settings:
    s = Settings() if args.defaults else load_settings()
    if args.speed is not None:
        s.speed = args.speed
    if args.clip_duration is not None:
        s.clip_duration = args.clip_duration
    if args.clips is not None:
        s.clips_per_compilation = args.clips
    if args.quality:
        s.quality = args.quality
    if args.watermark_text is not None:
        s.watermark_text = args.watermark_text
    if args.watermark_size:
        s.watermark_size = args.watermark_size
    if args.watermark_position:
        s.watermark_position = args.watermark_position
    if args.watermark_color:
        if args.watermark_color.lower() == "auto":
            s.watermark_color_mode = "auto"
        else:
            s.watermark_color_mode = "custom"
            s.watermark_custom_color = args.watermark_color
    if args.sensitivity:
        s.cut_sensitivity = args.sensitivity
    if args.distribution:
        s.distribution = args.distribution
    if args.glass:
        s.glass_effect = args.glass == "on"
    return s.validated()


class _ConsoleEvents:
    def __init__(self) -> None:
        self.last = -1

    def log(self, level, message):
        pass  # already printed by the logging handler

    def progress(self, overall, stage):
        pct = int(overall * 100)
        if pct != self.last:
            self.last = pct
            log.info("[%3d%%] %s", pct, stage)

    def video_started(self, index, count, path):
        log.info("Video %d/%d: %s", index + 1, count, path)

    def compilation_done(self, info):
        log.info("Created %s", info.get("path"))

    def video_finished(self, summary):
        pass

    def error(self, message):
        log.error(message)


def run_cli(args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    if not args.input or not args.output:
        log.error("--cli needs --input and --output")
        return 2
    settings = settings_from_args(args)
    pipe = Pipeline(settings, args.output, _ConsoleEvents())
    try:
        summary = pipe.run(args.input)
    except KeyboardInterrupt:
        pipe.cancel.cancel()
        return 130
    log.info("Created %d compilation(s). Errors: %d", len(summary.compilations_created), len(summary.errors))
    for e in summary.errors:
        log.error(e)
    return 0 if not summary.errors else 1


def run_self_test(outdir: str) -> int:
    """Generate synthetic footage with known cuts and audio, process it and check the reports."""
    from .ffmpeg_tools import probe
    from .pipeline import Pipeline
    from .synth import make_video, standard_shots

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    result: dict = {"app": APP_NAME, "version": __version__, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "checks": {}}
    ok = True
    try:
        src_dir = out / "source"
        src_dir.mkdir(exist_ok=True)
        src = src_dir / "Self test vidéo 01.mp4"
        if not src.exists():
            gt = make_video(str(src), standard_shots(100.0, seed=21, avg_shot=6.5), 640, 360, fps=30, seed=21)
            result["ground_truth_cuts"] = gt.cuts
            result["ground_truth_transitions"] = gt.transitions
        settings = Settings()
        results_dir = out / "output"
        summary = Pipeline(settings, str(results_dir), _ConsoleEvents()).run([str(src)])
        result["compilations"] = summary.compilations_created
        result["errors"] = summary.errors
        ok &= not summary.errors
        ok &= len(summary.compilations_created) >= 1
        result["checks"]["created_at_least_one"] = len(summary.compilations_created) >= 1
        for rp in summary.reports:
            rep = json.loads(Path(rp).read_text(encoding="utf-8"))
            for comp in rep.get("compilations", []):
                if comp.get("status") != "completed":
                    continue
                passed = bool(comp.get("verification", {}).get("passed"))
                info = probe(comp["path"])
                dur_ok = abs(info.duration - 30.0) < 0.07
                result["checks"][comp["filename"]] = {"verification_passed": passed, "duration_s": info.duration,
                                                      "duration_ok": dur_ok, "video": info.video_codec,
                                                      "audio": info.audio_codec}
                ok &= passed and dur_ok and info.video_codec == "h264" and info.audio_codec == "aac"
        result["report_files"] = summary.reports
    except Exception as exc:  # pragma: no cover - reported in the result file
        log.exception("self-test crashed")
        result["exception"] = repr(exc)
        ok = False
    result["passed"] = bool(ok)
    (out / "selftest_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("SELF-TEST %s", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    # In a windowed (no console) build stdout/stderr can be None.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    from .logging_setup import setup_logging

    setup_logging(args.verbose, args.log_file)
    if args.self_test:
        return run_self_test(args.self_test)
    if args.cli:
        return run_cli(args)
    from .gui.app import run_gui

    return run_gui(screenshot=args.gui_smoke, initial_inputs=args.input)
