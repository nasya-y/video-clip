"""Download a Windows x64 FFmpeg build into vendor/ffmpeg (used when packaging the app).

Source: https://github.com/BtbN/FFmpeg-Builds (GPL build with libx264, shared libraries).
The newest *release branch* build (e.g. n7.1) is preferred over the master snapshot.
Details of what was downloaded are written to vendor/ffmpeg/BUILD_INFO.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "vendor" / "ffmpeg"
API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
PATTERN = re.compile(r"^ffmpeg-n(\d+)\.(\d+)(?:\.(\d+))?-latest-win64-gpl-shared-\d+\.\d+\.zip$")
FALLBACK = "ffmpeg-master-latest-win64-gpl-shared.zip"


def _request(url: str) -> urllib.request.Request:
    headers = {"User-Agent": "video-compilation-maker-build"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def pick_asset(assets: list[dict]) -> dict:
    best = None
    for a in assets:
        m = PATTERN.match(a["name"])
        if m:
            ver = tuple(int(x or 0) for x in m.groups())
            if best is None or ver > best[0]:
                best = (ver, a)
    if best:
        return best[1]
    for a in assets:
        if a["name"] == FALLBACK:
            return a
    raise SystemExit("No suitable FFmpeg build found in the BtbN release")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="use an already downloaded build zip instead of downloading")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="ffmpeg-"))
    try:
        if args.zip:
            zpath = Path(args.zip)
            info = {"asset": zpath.name, "url": None, "release": None}
        else:
            with urllib.request.urlopen(_request(API), timeout=60) as r:
                rel = json.load(r)
            asset = pick_asset(rel.get("assets", []))
            zpath = tmp / asset["name"]
            print(f"Downloading {asset['name']} ({asset['size'] / 1e6:.0f} MB)")
            with urllib.request.urlopen(_request(asset["browser_download_url"]), timeout=600) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f, length=1 << 20)
            info = {"asset": asset["name"], "url": asset["browser_download_url"],
                    "release": rel.get("tag_name"), "published_at": rel.get("published_at")}
        info["sha256"] = hashlib.sha256(zpath.read_bytes()).hexdigest()
        if DEST.exists():
            shutil.rmtree(DEST)
        (DEST / "bin").mkdir(parents=True)
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            for n in names:
                base = n.rsplit("/", 1)[-1]
                if "/bin/" in n and (base.lower().endswith(".dll") or base.lower() in ("ffmpeg.exe", "ffprobe.exe")):
                    with z.open(n) as src, open(DEST / "bin" / base, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                elif base == "LICENSE.txt" and n.count("/") == 1:
                    with z.open(n) as src, open(DEST / "LICENSE.txt", "wb") as dst:
                        shutil.copyfileobj(src, dst)
        if not (DEST / "bin" / "ffmpeg.exe").exists() or not (DEST / "bin" / "ffprobe.exe").exists():
            raise SystemExit("ffmpeg.exe / ffprobe.exe missing from the archive")
        info["files"] = sorted(p.name for p in (DEST / "bin").iterdir())
        info["source_code"] = ("https://github.com/FFmpeg/FFmpeg (version as reported by ffmpeg -version); "
                               "build scripts and the exact library versions: https://github.com/BtbN/FFmpeg-Builds")
        (DEST / "BUILD_INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(json.dumps(info, indent=2))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
