# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: builds dist/VideoCompilationMaker/ with
#   VideoCompilationMaker.exe      - the windowed application
#   VideoCompilationMaker-cli.exe  - the same program with a console (batch mode / self-test)
# FFmpeg (vendor/ffmpeg/bin) and all resources are bundled; nothing needs to be installed separately.

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
FFMPEG_BIN = ROOT / "vendor" / "ffmpeg" / "bin"
LICENSES = ROOT / "build" / "licenses"
IS_WIN = sys.platform == "win32"

if not FFMPEG_BIN.is_dir():
    raise SystemExit("vendor/ffmpeg/bin is missing - run packaging/fetch_ffmpeg.py first")

datas = [
    (str(ROOT / "resources"), "resources"),
    (str(FFMPEG_BIN), "ffmpeg/bin"),
]
if LICENSES.is_dir():
    datas.append((str(LICENSES), "licenses"))

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["vcm.gui.app", "vcm.gui.main_window", "vcm.gui.worker"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas", "IPython", "pytest",
              "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql", "PySide6.QtTest",
              "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtPdf"],
    noarchive=False,
)

# OpenCV ships its own FFmpeg DLL for VideoCapture, which this app never uses - drop it (~25 MB).
a.binaries = [b for b in a.binaries if "opencv_videoio_ffmpeg" not in b[0].lower()]

pyz = PYZ(a.pure)

icon = str(ROOT / "resources" / "icons" / "app.ico")
version = str(ROOT / "packaging" / "version_info.txt") if IS_WIN else None

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VideoCompilationMaker",
    console=False,
    icon=icon,
    version=version,
    upx=False,
)
exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VideoCompilationMaker-cli",
    console=True,
    icon=icon,
    version=version,
    upx=False,
)
coll = COLLECT(
    exe_gui, exe_cli,
    a.binaries, a.datas,
    upx=False,
    name="VideoCompilationMaker",
)
