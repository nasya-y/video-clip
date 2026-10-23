"""Print the key build/test results into the CI log (so they can be read without downloading artifacts)."""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def show_json(title: str, path: Path) -> None:
    print(f"===== {title} ({path}) =====")
    if path.is_file():
        try:
            print(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False)[:20000])
        except ValueError:
            print(path.read_text(encoding="utf-8", errors="replace")[:20000])
    else:
        print("(missing)")


def main() -> int:
    show_json("packaged exe self-test", ROOT / "selftest-exe" / "selftest_result.json")
    show_json("installed app self-test", ROOT / "selftest-installed" / "selftest_result.json")
    show_json("FFmpeg build", ROOT / "vendor" / "ffmpeg" / "BUILD_INFO.json")
    dist = ROOT / "dist"
    print("===== dist =====")
    if dist.is_dir():
        for p in sorted(dist.iterdir()):
            size = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            print(f"{p.name}: {size / 1e6:.1f} MB")
    reports = sorted((ROOT / "selftest-exe" / "output").rglob("Compilation report.json"))
    for r in reports:
        data = json.loads(r.read_text(encoding="utf-8"))
        for c in data.get("compilations", []):
            v = c.get("verification", {})
            print(f"===== {c.get('filename')}: status={c.get('status')} verification_passed={v.get('passed')}")
            for name, chk in (v.get("checks") or {}).items():
                if isinstance(chk, dict):
                    print(f"  {name}: {'PASS' if chk.get('passed') else 'FAIL'}")
    shot = ROOT / "test-results" / "screenshot-gui-windows.png"
    if shot.is_file():
        try:
            from PIL import Image
            img = Image.open(shot).convert("RGB")
            img.thumbnail((1000, 1000))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            data = base64.b64encode(buf.getvalue()).decode()
            print(f"===== SCREENSHOT-BEGIN {img.size[0]}x{img.size[1]} =====")
            for i in range(0, len(data), 1000):
                print("B64:" + data[i:i + 1000])
            print("===== SCREENSHOT-END =====")
        except Exception as exc:  # pragma: no cover
            print(f"screenshot could not be encoded: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
