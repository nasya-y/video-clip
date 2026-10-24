# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: builds dist/VideoCompilationMaker/ with
#   VideoCompilationMaker.exe      - the windowed application
#   VideoCompilationMaker-cli.exe  - the same program with a console (batch mode / self-test)
# FFmpeg (vendor/ffmpeg/bin) and all resources are bundled; nothing needs to be installed separately.

import re
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

# Drop files the app never uses:
#  * OpenCV's own FFmpeg DLL for VideoCapture (~25 MB),
#  * second copies of the FFmpeg DLLs that dependency analysis of ffmpeg.exe/ffprobe.exe puts next
#    to python312.dll - the executables load the copies in ffmpeg/bin (~190 MB),
#  * Qt's software OpenGL renderer; the interface uses plain widgets (~21 MB).
_FFMPEG_DLL = re.compile(r"^(avcodec|avformat|avfilter|avdevice|avutil|swresample|swscale|postproc)-\d+\.dll$", re.I)


def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    name = dest.rsplit("/", 1)[-1].lower()
    if "opencv_videoio_ffmpeg" in name or name == "opengl32sw.dll":
        return False
    if "/" not in dest and _FFMPEG_DLL.match(name):
        return False
    return True


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

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
