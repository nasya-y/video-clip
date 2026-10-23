"""Runs the processing pipeline in a background thread and relays events as Qt signals."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot

from ..ffmpeg_tools import CancelToken
from ..pipeline import Events, Pipeline, RunSummary
from ..settings import Settings

log = logging.getLogger(__name__)


class _SignalEvents(Events):
    def __init__(self, worker: "ProcessingWorker"):
        self.w = worker

    def log(self, level: int, message: str) -> None:
        self.w.log_message.emit(level, message)

    def progress(self, overall: float, stage: str) -> None:
        self.w.progress.emit(float(overall), stage)

    def video_started(self, index: int, count: int, path: str) -> None:
        self.w.video_started.emit(index, count, path)

    def compilation_done(self, info: dict) -> None:
        self.w.compilation_done.emit(info)

    def video_finished(self, summary: dict) -> None:
        self.w.video_finished.emit(summary)

    def error(self, message: str) -> None:
        self.w.error.emit(message)


class ProcessingWorker(QObject):
    log_message = Signal(int, str)
    progress = Signal(float, str)
    video_started = Signal(int, int, str)
    compilation_done = Signal(object)
    video_finished = Signal(object)
    error = Signal(str)
    finished = Signal(object)

    def __init__(self, settings: Settings, inputs: list[str], output_dir: str):
        super().__init__()
        self.settings = settings
        self.inputs = list(inputs)
        self.output_dir = output_dir
        self.cancel_token = CancelToken()

    @Slot()
    def run(self) -> None:
        try:
            pipe = Pipeline(self.settings, self.output_dir, _SignalEvents(self), self.cancel_token)
            summary = pipe.run(self.inputs)
        except Exception as exc:  # never let the worker thread die silently
            log.exception("Processing crashed")
            summary = RunSummary(errors=[f"Unexpected error: {exc}"], output_dir=self.output_dir)
            self.error.emit(summary.errors[0])
        self.finished.emit(summary)

    def cancel(self) -> None:
        self.cancel_token.cancel()
