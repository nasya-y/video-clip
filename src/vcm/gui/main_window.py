"""Main application window."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..paths import log_dir, resource_path
from ..pipeline import VIDEO_EXTENSIONS, RunSummary, collect_videos
from ..settings import (CUT_SENSITIVITY, DEFAULT_WATERMARK_TEXT, DISTRIBUTIONS, QUALITY_PRESETS, WATERMARK_POSITIONS,
                        WATERMARK_SIZES, Settings, load_settings, save_settings)
from .worker import ProcessingWorker

log = logging.getLogger(__name__)


def _card(title: str, step: Optional[int] = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 12, 16, 14)
    lay.setSpacing(8)
    head = QHBoxLayout()
    head.setSpacing(10)
    if step is not None:
        badge = QLabel(str(step))
        badge.setObjectName("StepBadge")
        badge.setAlignment(Qt.AlignCenter)
        head.addWidget(badge)
    t = QLabel(title)
    t.setObjectName("CardTitle")
    head.addWidget(t)
    head.addStretch(1)
    lay.addLayout(head)
    return frame, lay


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Hint")
    lab.setWordWrap(True)
    return lab


class VideoList(QListWidget):
    """List of source videos that accepts dropped files and folders."""

    def __init__(self, on_drop):
        super().__init__()
        self._on_drop = on_drop
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumHeight(110)
        self._placeholder = "Drop videos or folders here\nor use the buttons below"

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            self._on_drop(paths)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            from PySide6.QtGui import QPainter
            p = QPainter(self.viewport())
            p.setPen(QColor("#6B7286"))
            p.drawText(self.viewport().rect(), Qt.AlignCenter, self._placeholder)
            p.end()


class MainWindow(QMainWindow):
    def __init__(self, initial_inputs: Optional[list[str]] = None):
        super().__init__()
        self.settings = load_settings()
        self.thread: Optional[QThread] = None
        self.worker: Optional[ProcessingWorker] = None
        self.created: list[dict] = []
        self.run_started = 0.0
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_settings)
        self.setWindowTitle(f"{APP_NAME}")
        icon_path = resource_path("icons", "app.png")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(1000, 620)
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        if avail is not None and avail.width() > 0:
            self.resize(min(1200, avail.width() - 40), min(860, avail.height() - 40))
        else:
            self.resize(1200, 860)
        self._build_menu()
        self._build_ui()
        self._load_settings_into_ui()
        if initial_inputs:
            self.add_paths(initial_inputs)
        self._update_state()

    # ------------------------------------------------------------------ UI construction
    def _build_menu(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        for text, slot in (("Add videos…", self.on_add_files), ("Add folder…", self.on_add_folder),
                           ("Choose output folder…", self.on_browse_output)):
            a = QAction(text, self)
            a.triggered.connect(slot)
            m_file.addAction(a)
        m_file.addSeparator()
        a = QAction("Exit", self)
        a.triggered.connect(self.close)
        m_file.addAction(a)
        m_help = mb.addMenu("&Help")
        a = QAction("Open log folder", self)
        a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir()))))
        m_help.addAction(a)
        a = QAction("About and licences", self)
        a.triggered.connect(self.on_about)
        m_help.addAction(a)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("Header")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(22, 10, 22, 10)
        lay.setSpacing(14)
        icon = QLabel()
        icon.setObjectName("HeaderIcon")
        pm = QPixmap(str(resource_path("icons", "app.png")))
        if not pm.isNull():
            icon.setPixmap(pm.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(icon)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        sub = QLabel("Turns your videos into 30-second highlight compilations automatically.")
        sub.setObjectName("AppSubtitle")
        col.addWidget(title)
        col.addWidget(sub)
        lay.addLayout(col)
        lay.addStretch(1)
        badge = QLabel("●  Works offline · nothing is uploaded")
        badge.setObjectName("OfflineBadge")
        lay.addWidget(badge, 0, Qt.AlignVCenter)
        return header

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(14)
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(14)

        # 1. videos
        card, lay = _card("Select videos", 1)
        self.video_list = VideoList(self.add_paths)
        lay.addWidget(self.video_list, 1)
        row = QHBoxLayout()
        self.btn_add = QPushButton("Add videos…")
        self.btn_add.clicked.connect(self.on_add_files)
        self.btn_folder = QPushButton("Add folder…")
        self.btn_folder.clicked.connect(self.on_add_folder)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self.on_remove)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.on_clear)
        for b in (self.btn_add, self.btn_folder, self.btn_remove, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("Hint")
        row.addWidget(self.lbl_count)
        lay.addLayout(row)
        left.addWidget(card, 1)

        # 2. output
        card, lay = _card("Output folder", 2)
        row = QHBoxLayout()
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Choose where the compilations will be saved")
        self.out_edit.textChanged.connect(self._settings_changed)
        self.btn_out = QPushButton("Browse…")
        self.btn_out.clicked.connect(self.on_browse_output)
        row.addWidget(self.out_edit, 1)
        row.addWidget(self.btn_out)
        lay.addLayout(row)
        lay.addWidget(_hint("Each video gets its own folder with Compilation 1.mp4, Compilation 2.mp4, … "
                            "and a JSON report. Original files are never changed."))
        left.addWidget(card)
        top.addLayout(left, 10)

        # 3. settings (compact two-column grid)
        card, lay = _card("Settings", 3)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.25, 4.0)
        self.sp_speed.setSingleStep(0.05)
        self.sp_speed.setDecimals(2)
        self.sp_speed.setSuffix(" ×")
        self.sp_clip = QDoubleSpinBox()
        self.sp_clip.setRange(0.5, 60.0)
        self.sp_clip.setSingleStep(0.5)
        self.sp_clip.setDecimals(1)
        self.sp_clip.setSuffix(" s")
        self.sp_count = QSpinBox()
        self.sp_count.setRange(1, 100)
        self.lbl_total = QLabel("30.0 s")
        self.lbl_total.setObjectName("Derived")
        self.lbl_total.setWordWrap(True)
        self.cb_quality = QComboBox()
        for k, v in QUALITY_PRESETS.items():
            self.cb_quality.addItem(v["label"], k)

        def lab(text: str) -> QLabel:
            return QLabel(text)

        r = 0
        g.addWidget(lab("Playback speed"), r, 0)
        g.addWidget(self.sp_speed, r, 1)
        g.addWidget(lab("Clip length"), r, 2)
        g.addWidget(self.sp_clip, r, 3)
        r += 1
        g.addWidget(lab("Number of clips"), r, 0)
        g.addWidget(self.sp_count, r, 1)
        g.addWidget(lab("Quality"), r, 2)
        g.addWidget(self.cb_quality, r, 3)
        r += 1
        g.addWidget(lab("Compilation"), r, 0)
        g.addWidget(self.lbl_total, r, 1, 1, 3)
        r += 1
        sec = QLabel("Watermark")
        sec.setObjectName("SectionLabel")
        g.addWidget(sec, r, 0, 1, 4)
        r += 1
        self.ed_text = QLineEdit()
        g.addWidget(lab("Text"), r, 0)
        g.addWidget(self.ed_text, r, 1, 1, 3)
        r += 1
        self.cb_size = QComboBox()
        for k, v in WATERMARK_SIZES.items():
            self.cb_size.addItem(v["label"], k)
        self.cb_pos = QComboBox()
        for k, v in WATERMARK_POSITIONS.items():
            self.cb_pos.addItem(v, k)
        g.addWidget(lab("Size"), r, 0)
        g.addWidget(self.cb_size, r, 1)
        g.addWidget(lab("Position"), r, 2)
        g.addWidget(self.cb_pos, r, 3)
        r += 1
        colour_row = QHBoxLayout()
        self.cb_colour = QComboBox()
        self.cb_colour.addItem("Automatic – vivid, high contrast", "auto")
        self.cb_colour.addItem("Custom colour", "custom")
        self.btn_swatch = QPushButton("")
        self.btn_swatch.setObjectName("Swatch")
        self.btn_swatch.setToolTip("Choose the custom watermark colour")
        self.btn_swatch.clicked.connect(self.on_pick_colour)
        colour_row.addWidget(self.cb_colour, 1)
        colour_row.addWidget(self.btn_swatch)
        g.addWidget(lab("Colour"), r, 0)
        g.addLayout(colour_row, r, 1, 1, 3)
        r += 1
        sec2 = QLabel("Advanced")
        sec2.setObjectName("SectionLabel")
        g.addWidget(sec2, r, 0, 1, 4)
        r += 1
        self.cb_sens = QComboBox()
        for k, v in CUT_SENSITIVITY.items():
            self.cb_sens.addItem(v, k)
        self.cb_dist = QComboBox()
        for k, v in DISTRIBUTIONS.items():
            self.cb_dist.addItem(v, k)
        self.cb_dist.setToolTip("\n".join(DISTRIBUTIONS.values()))
        self.sp_fps = QSpinBox()
        self.sp_fps.setRange(10, 120)
        self.sp_fps.setSuffix(" fps")
        g.addWidget(lab("Cut detection"), r, 0)
        g.addWidget(self.cb_sens, r, 1)
        g.addWidget(lab("Max frame rate"), r, 2)
        g.addWidget(self.sp_fps, r, 3)
        r += 1
        g.addWidget(lab("Clip order"), r, 0)
        g.addWidget(self.cb_dist, r, 1, 1, 3)
        lay.addLayout(g)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(_hint("Defaults: 1.3× speed · 10 clips × 3 s = 30 s · H.264 + AAC MP4"), 1)
        self.btn_defaults = QPushButton("Restore defaults")
        self.btn_defaults.clicked.connect(self.on_defaults)
        row.addWidget(self.btn_defaults)
        lay.addLayout(row)
        for w in (self.sp_speed, self.sp_clip):
            w.valueChanged.connect(self._settings_changed)
        self.sp_count.valueChanged.connect(self._settings_changed)
        self.sp_fps.valueChanged.connect(self._settings_changed)
        self.ed_text.textChanged.connect(self._settings_changed)
        for cb in (self.cb_quality, self.cb_size, self.cb_pos, self.cb_colour, self.cb_sens, self.cb_dist):
            cb.currentIndexChanged.connect(self._settings_changed)
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(8)
        for w in (self.sp_speed, self.sp_clip, self.sp_count, self.sp_fps):
            w.setMinimumWidth(80)
        top.addWidget(card, 11)

        # 4. progress details (the bar and Start/Cancel live in the fixed footer)
        card, lay = _card("Progress", 4)
        self.lbl_video = QLabel("Current video: –")
        self.lbl_video.setObjectName("Stat")
        self.lbl_stage = QLabel("Add videos, choose an output folder and press Start.")
        self.lbl_stage.setObjectName("Hint")
        self.lbl_stage.setWordWrap(True)
        self.lbl_stage.setMinimumWidth(100)
        self.lbl_done = QLabel("Compilations completed: 0")
        self.lbl_done.setObjectName("Big")
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("Hint")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setVisible(False)
        lay.addWidget(self.lbl_video)
        lay.addWidget(self.lbl_stage)
        lay.addWidget(self.lbl_done)
        lay.addWidget(self.lbl_summary)
        self.errors = QListWidget()
        self.errors.setObjectName("Errors")
        self.errors.setMaximumHeight(80)
        self.errors.setWordWrap(True)
        self.errors.setVisible(False)
        lay.addWidget(self.errors)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(2000)
        self.details.setVisible(False)
        self.details.setMinimumHeight(110)
        lay.addWidget(self.details)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_details = QPushButton("Show details")
        self.btn_details.setObjectName("Link")
        self.btn_details.clicked.connect(self.on_toggle_details)
        row.addWidget(self.btn_details)
        lay.addLayout(row)
        bottom.addWidget(card, 11)

        # 5. results
        card, lay = _card("Results", 5)
        self.lbl_results = QLabel("No compilations created yet.")
        self.lbl_results.setObjectName("Stat")
        self.lbl_results.setWordWrap(True)
        lay.addWidget(self.lbl_results)
        self.results = QListWidget()
        self.results.setIconSize(QSize(96, 54))
        self.results.setMinimumHeight(90)
        self.results.itemDoubleClicked.connect(lambda it: self._open_path(it.data(Qt.UserRole)))
        self.results.currentItemChanged.connect(lambda *_: self._update_state())
        lay.addWidget(self.results, 1)
        row = QHBoxLayout()
        self.btn_open_out = QPushButton("Open output folder")
        self.btn_open_out.clicked.connect(self.on_open_output)
        self.btn_preview = QPushButton("▶ Preview")
        self.btn_preview.clicked.connect(self.on_preview)
        self.btn_report = QPushButton("Report")
        self.btn_report.clicked.connect(self.on_open_report)
        row.addWidget(self.btn_open_out)
        row.addWidget(self.btn_preview)
        row.addWidget(self.btn_report)
        row.addStretch(1)
        lay.addLayout(row)
        bottom.addWidget(card, 10)

        outer.addLayout(top, 0)
        outer.addLayout(bottom, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        root.addWidget(self._build_footer())
        self.setCentralWidget(central)

    def _build_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName("Footer")
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(18, 10, 18, 12)
        lay.setSpacing(12)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        lay.addWidget(self.progress, 1)
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self.on_start)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("Danger")
        self.btn_cancel.clicked.connect(self.on_cancel)
        lay.addWidget(self.btn_start)
        lay.addWidget(self.btn_cancel)
        return footer

    # ------------------------------------------------------------------ settings <-> UI
    def _load_settings_into_ui(self) -> None:
        s = self.settings
        self._loading = True
        self.sp_speed.setValue(s.speed)
        self.sp_clip.setValue(s.clip_duration)
        self.sp_count.setValue(s.clips_per_compilation)
        self.sp_fps.setValue(s.max_fps)
        self.ed_text.setText(s.watermark_text)
        for cb, val in ((self.cb_quality, s.quality), (self.cb_size, s.watermark_size),
                        (self.cb_pos, s.watermark_position), (self.cb_colour, s.watermark_color_mode),
                        (self.cb_sens, s.cut_sensitivity), (self.cb_dist, s.distribution)):
            i = cb.findData(val)
            cb.setCurrentIndex(max(0, i))
        self._custom_colour = s.watermark_custom_color
        self.out_edit.setText(s.output_dir)
        self.video_list.clear()
        for p in s.input_paths:
            if os.path.isfile(p):
                self._add_item(p)
        self._loading = False
        self._refresh_derived()

    def _ui_to_settings(self) -> Settings:
        s = Settings(
            speed=self.sp_speed.value(), clip_duration=self.sp_clip.value(),
            clips_per_compilation=self.sp_count.value(), quality=self.cb_quality.currentData(),
            max_fps=self.sp_fps.value(), cut_sensitivity=self.cb_sens.currentData(),
            distribution=self.cb_dist.currentData(), watermark_text=self.ed_text.text(),
            watermark_size=self.cb_size.currentData(), watermark_position=self.cb_pos.currentData(),
            watermark_color_mode=self.cb_colour.currentData(), watermark_custom_color=self._custom_colour,
            input_paths=self.input_paths(), output_dir=self.out_edit.text().strip(),
            last_browse_dir=self.settings.last_browse_dir,
        )
        return s.validated()

    def _settings_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        self._refresh_derived()
        self._save_timer.start()
        self._update_state()

    def _refresh_derived(self) -> None:
        total = self.sp_clip.value() * self.sp_count.value()
        src = self.sp_clip.value() * self.sp_speed.value()
        self.lbl_total.setText(f"{total:.1f} s   (each clip uses {src:.2f} s of original footage)")
        custom = self.cb_colour.currentData() == "custom"
        self.btn_swatch.setEnabled(custom)
        col = self._custom_colour if custom else "#2A3040"
        self.btn_swatch.setStyleSheet(f"QPushButton#Swatch {{ background: {col}; border: 1px solid #4A5370; }}")

    def _save_settings(self) -> None:
        try:
            self.settings = self._ui_to_settings()
            save_settings(self.settings)
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    # ------------------------------------------------------------------ video list
    def input_paths(self) -> list[str]:
        return [self.video_list.item(i).data(Qt.UserRole) for i in range(self.video_list.count())]

    def _add_item(self, path: str) -> None:
        p = Path(path)
        try:
            size = p.stat().st_size / 1e6
            label = f"{p.name}    ·  {size:,.0f} MB"
        except OSError:
            label = p.name
        it = QListWidgetItem(label)
        it.setData(Qt.UserRole, str(p))
        it.setToolTip(str(p))
        self.video_list.addItem(it)

    def add_paths(self, paths) -> None:
        existing = {os.path.normcase(x) for x in self.input_paths()}
        added = 0
        for f in collect_videos(paths):
            if os.path.normcase(f) not in existing:
                self._add_item(f)
                existing.add(os.path.normcase(f))
                added += 1
        if paths and added == 0 and not self.input_paths():
            self.lbl_stage.setText("No supported video files were found in that selection.")
        self._settings_changed()

    @Slot()
    def on_add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Select videos", self._browse_dir(),
                                                f"Videos ({exts});;All files (*.*)")
        if files:
            self.settings.last_browse_dir = str(Path(files[0]).parent)
            self.add_paths(files)

    @Slot()
    def on_add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select a folder with videos", self._browse_dir())
        if d:
            self.settings.last_browse_dir = d
            self.add_paths([d])

    def _browse_dir(self) -> str:
        d = self.settings.last_browse_dir
        return d if d and os.path.isdir(d) else str(Path.home())

    @Slot()
    def on_remove(self) -> None:
        for it in self.video_list.selectedItems():
            self.video_list.takeItem(self.video_list.row(it))
        self._settings_changed()

    @Slot()
    def on_clear(self) -> None:
        self.video_list.clear()
        self._settings_changed()

    @Slot()
    def on_browse_output(self) -> None:
        start = self.out_edit.text() or self._browse_dir()
        d = QFileDialog.getExistingDirectory(self, "Choose the output folder", start)
        if d:
            self.out_edit.setText(d)

    @Slot()
    def on_pick_colour(self) -> None:
        c = QColorDialog.getColor(QColor(self._custom_colour), self, "Watermark colour")
        if c.isValid():
            self._custom_colour = c.name().upper()
            self._settings_changed()

    @Slot()
    def on_defaults(self) -> None:
        cur = self._ui_to_settings()
        self.settings = cur.reset_processing_defaults()
        self._load_settings_into_ui()
        self._settings_changed()

    # ------------------------------------------------------------------ processing
    def _busy(self) -> bool:
        return self.thread is not None

    def _update_state(self) -> None:
        busy = self._busy()
        has_inputs = self.video_list.count() > 0
        has_out = bool(self.out_edit.text().strip())
        self.btn_start.setEnabled(not busy and has_inputs and has_out and bool(self.ed_text.text().strip()))
        self.btn_cancel.setEnabled(busy)
        for w in (self.btn_add, self.btn_folder, self.btn_remove, self.btn_clear, self.btn_out, self.out_edit,
                  self.btn_defaults, self.sp_speed, self.sp_clip, self.sp_count, self.cb_quality, self.ed_text,
                  self.cb_size, self.cb_pos, self.cb_colour, self.cb_sens, self.cb_dist, self.sp_fps):
            w.setEnabled(not busy)
        if not busy:
            self.btn_swatch.setEnabled(self.cb_colour.currentData() == "custom")
        else:
            self.btn_swatch.setEnabled(False)
        self.video_list.setAcceptDrops(not busy)
        n = self.video_list.count()
        self.lbl_count.setText(f"{n} video{'s' if n != 1 else ''}")
        out = self.out_edit.text().strip()
        self.btn_open_out.setEnabled(bool(out) and os.path.isdir(out))
        cur = self.results.currentItem()
        self.btn_preview.setEnabled(cur is not None)
        self.btn_report.setEnabled(cur is not None)

    @Slot()
    def on_start(self) -> None:
        if self._busy():
            return
        settings = self._ui_to_settings()
        problems = settings.problems()
        out = settings.output_dir
        if not out:
            problems.append("Choose an output folder.")
        if not self.input_paths():
            problems.append("Add at least one video.")
        if problems:
            QMessageBox.warning(self, APP_NAME, "\n".join(problems))
            return
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"The output folder cannot be created:\n{exc}")
            return
        self.settings = settings
        save_settings(settings)
        self.errors.clear()
        self.errors.setVisible(False)
        self.lbl_summary.setVisible(False)
        self.created_this_run = 0
        self.lbl_done.setText("Compilations completed: 0")
        self.progress.setValue(0)
        self.progress.setFormat("Starting…")
        self.run_started = time.monotonic()
        self.worker = ProcessingWorker(settings, self.input_paths(), out)
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.video_started.connect(self.on_video_started)
        self.worker.compilation_done.connect(self.on_compilation_done)
        self.worker.error.connect(self.on_error)
        self.worker.log_message.connect(self.on_log)
        self.worker.finished.connect(self.on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_done)
        self.thread.start()
        self._update_state()

    @Slot()
    def on_cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.lbl_stage.setText("Cancelling… (finished compilations are kept, the current one is discarded)")

    @Slot(float, str)
    def on_progress(self, overall: float, stage: str) -> None:
        self.progress.setValue(int(overall * 1000))
        elapsed = time.monotonic() - self.run_started
        eta = ""
        if overall > 0.03:
            remaining = elapsed * (1 - overall) / overall
            eta = f" · about {_fmt_secs(remaining)} left"
        self.progress.setFormat(f"{overall * 100:.0f}%{eta}")
        self.lbl_stage.setText(stage)

    @Slot(int, int, str)
    def on_video_started(self, index: int, count: int, path: str) -> None:
        self.lbl_video.setText(f"Current video ({index + 1} of {count}): {Path(path).name}")

    @Slot(object)
    def on_compilation_done(self, info: dict) -> None:
        self.created_this_run += 1
        self.lbl_done.setText(f"Compilations completed: {self.created_this_run}")
        self._add_result(info)

    def _add_result(self, info: dict) -> None:
        path = info.get("path", "")
        label = f"{Path(path).stem}  —  {Path(info.get('video', '')).name}\n" \
                f"{info.get('duration', 0):.2f} s · watermark {info.get('colour', '')}, {info.get('position', '')}"
        it = QListWidgetItem(label)
        it.setData(Qt.UserRole, path)
        it.setToolTip(path)
        thumb = info.get("thumbnail")
        if thumb and os.path.isfile(thumb):
            it.setIcon(QIcon(thumb))
        self.results.addItem(it)
        self.results.setCurrentItem(it)

    @Slot(str)
    def on_error(self, message: str) -> None:
        self.errors.setVisible(True)
        self.errors.addItem(message.split("\n")[0])
        self.errors.item(self.errors.count() - 1).setToolTip(message)
        self.details.appendPlainText("ERROR: " + message)

    @Slot(int, str)
    def on_log(self, level: int, message: str) -> None:
        prefix = "" if level <= logging.INFO else ("WARNING: " if level == logging.WARNING else "ERROR: ")
        self.details.appendPlainText(time.strftime("%H:%M:%S ") + prefix + message)

    @Slot(object)
    def on_finished(self, summary: RunSummary) -> None:
        n = len(summary.compilations_created)
        took = _fmt_secs(time.monotonic() - self.run_started)
        if summary.cancelled:
            self.progress.setFormat("Cancelled")
            self.lbl_stage.setText(f"Cancelled after {took}. Completed compilations were kept.")
        else:
            self.progress.setValue(1000)
            self.progress.setFormat("Done")
            self.lbl_stage.setText(f"Finished in {took}.")
        head = (f"Created {n} compilation{'s' if n != 1 else ''} from {summary.videos} video"
                f"{'s' if summary.videos != 1 else ''}.")
        if summary.errors:
            head += f"  {len(summary.errors)} problem(s) – see Progress."
        self.lbl_results.setText(head)
        lines = []
        for vs in summary.video_summaries:
            rem = vs.get("remaining") or {}
            cap = vs.get("capacity") or {}
            if rem.get("message"):
                most = cap.get("max_compilations_from_whole_video")
                lines.append(f"• {Path(vs['video']).name}: {vs.get('compilations_total', 0)} compilation(s) in "
                             f"total" + (f" (at most {most} possible)" if most is not None else "") + ". "
                             + (cap.get("message", "") + " " if cap.get("message") else "") + rem["message"])
            elif vs.get("errors"):
                lines.append(f"• {Path(vs['video']).name}: {vs['errors'][0]}")
        self.lbl_summary.setText("\n".join(lines))
        self.lbl_summary.setVisible(bool(lines))

    @Slot()
    def _thread_done(self) -> None:
        if self.thread is not None:
            self.thread.deleteLater()
        if self.worker is not None:
            self.worker.deleteLater()
        self.thread = None
        self.worker = None
        self._update_state()

    @Slot()
    def on_toggle_details(self) -> None:
        vis = not self.details.isVisible()
        self.details.setVisible(vis)
        self.btn_details.setText("Hide details" if vis else "Show details")

    # ------------------------------------------------------------------ results
    def _open_path(self, path: Optional[str]) -> None:
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @Slot()
    def on_open_output(self) -> None:
        self._open_path(self.out_edit.text().strip())

    @Slot()
    def on_preview(self) -> None:
        it = self.results.currentItem()
        if it is not None:
            self._open_path(it.data(Qt.UserRole))

    @Slot()
    def on_open_report(self) -> None:
        it = self.results.currentItem()
        if it is not None:
            from ..report import REPORT_NAME
            self._open_path(str(Path(it.data(Qt.UserRole)).parent / REPORT_NAME))

    @Slot()
    def on_about(self) -> None:
        from ..ffmpeg_tools import ffmpeg_version
        try:
            ff = ffmpeg_version()
        except Exception as exc:
            ff = f"not found ({exc})"
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>Creates 30-second highlight compilations from your own videos. All processing happens on "
            "this computer; no video is uploaded and no internet connection is needed.</p>"
            f"<p><b>Bundled components</b><br>{ff}<br>Qt / PySide6 (LGPLv3), OpenCV (Apache 2.0), NumPy (BSD), "
            "Pillow (MIT-CMU), Poppins font (SIL OFL 1.1), YuNet face model (MIT).</p>"
            "<p>FFmpeg is licensed under the GPL; see THIRD_PARTY_NOTICES.txt and the licenses folder "
            "in the installation directory for licence texts and source code locations.</p>")

    # ------------------------------------------------------------------ close
    def closeEvent(self, e) -> None:
        if self._busy():
            r = QMessageBox.question(self, APP_NAME, "Processing is still running. Cancel it and quit?\n"
                                     "Completed compilations are kept.")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            if self.worker is not None:
                self.worker.cancel()
            if self.thread is not None:
                self.thread.quit()
                self.thread.wait(15000)
        self._save_settings()
        super().closeEvent(e)


def _fmt_secs(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"
