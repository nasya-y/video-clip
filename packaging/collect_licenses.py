"""Gather licence texts of everything that ships in the packaged application into build/licenses."""

from __future__ import annotations

import importlib.metadata as md
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "licenses"

PACKAGES = ["numpy", "opencv-python-headless", "pillow", "PySide6-Essentials", "shiboken6"]


def copy_dist_licenses(name: str) -> list[str]:
    copied = []
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        return copied
    for f in dist.files or []:
        low = f.name.lower()
        if any(k in low for k in ("license", "licence", "copying", "notice")) and not low.endswith((".py", ".pyc")):
            src = Path(dist.locate_file(f))
            if src.is_file():
                dst = OUT / f"{dist.metadata['Name']}-{dist.version}" / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
                copied.append(str(dst.relative_to(OUT)))
    return copied


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    report = {}
    for p in PACKAGES:
        report[p] = copy_dist_licenses(p)
    ff = ROOT / "vendor" / "ffmpeg"
    if (ff / "LICENSE.txt").exists():
        (OUT / "FFmpeg").mkdir(exist_ok=True)
        shutil.copy(ff / "LICENSE.txt", OUT / "FFmpeg" / "LICENSE.txt")
        if (ff / "BUILD_INFO.json").exists():
            shutil.copy(ff / "BUILD_INFO.json", OUT / "FFmpeg" / "BUILD_INFO.json")
    # Qt for Python is LGPLv3; FFmpeg/x264 builds are GPLv3 (full texts shipped with the app)
    common = ROOT / "packaging" / "licenses"
    (OUT / "PySide6-Qt").mkdir(exist_ok=True)
    shutil.copy(common / "LGPL-3.0-only.txt", OUT / "PySide6-Qt" / "LGPL-3.0-only.txt")
    shutil.copy(common / "GPL-3.0-only.txt", OUT / "PySide6-Qt" / "GPL-3.0-only.txt")
    (OUT / "FFmpeg").mkdir(exist_ok=True)
    shutil.copy(common / "GPL-3.0-only.txt", OUT / "FFmpeg" / "GPL-3.0-only.txt")
    shutil.copy(common / "x264-COPYING.txt", OUT / "FFmpeg" / "x264-COPYING.txt")
    (OUT / "Poppins-font").mkdir()
    shutil.copy(ROOT / "resources" / "fonts" / "OFL.txt", OUT / "Poppins-font" / "OFL.txt")
    (OUT / "YuNet-face-model").mkdir()
    shutil.copy(ROOT / "resources" / "models" / "YUNET_LICENSE.txt", OUT / "YuNet-face-model" / "LICENSE.txt")
    shutil.copy(ROOT / "LICENSE", OUT / "Video-Compilation-Maker-LICENSE.txt")
    shutil.copy(ROOT / "THIRD_PARTY_NOTICES.md", OUT / "THIRD_PARTY_NOTICES.md")
    for k, v in report.items():
        print(f"{k}: {len(v)} licence file(s) from package metadata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
