"""Watermark layout, automatic colour/position choice and rendered-output verification."""

import numpy as np
import pytest

from vcm import watermark as wm
from vcm.ffmpeg_tools import FrameReader
from vcm.settings import DEFAULT_WATERMARK_TEXT

TEXT = DEFAULT_WATERMARK_TEXT


@pytest.mark.parametrize("w,h", [(1920, 1080), (1280, 720), (1080, 1920), (720, 1280), (640, 480), (320, 180),
                                 (3840, 2160)])
@pytest.mark.parametrize("size", ["medium", "large", "xlarge"])
def test_layout_is_large_and_inside_frame(w, h, size):
    st = wm.make_style(w, h, TEXT, size)
    x0, y0, x1, y1 = st.text_box
    assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h
    assert (x1 - x0) <= 0.86 * w + 1
    assert st.font_px >= 14
    assert (y1 - y0) / min(w, h) >= 0.03, "watermark must not be tiny"
    assert st.outline_px >= 2
    for pos in wm.POSITIONS:
        p = wm.place(st, w, h, pos)
        bx0, by0, bx1, by1 = p.text_box
        assert 0 <= bx0 and bx1 <= w and 0 <= by0 and by1 <= h


def test_overlay_contains_exact_text_pixels():
    st = wm.make_style(1280, 720, TEXT, "large")
    img, fill, ink = wm.render_overlay(st)
    assert img.mode == "RGBA" and img.size == (st.width, st.height)
    arr = np.asarray(img)
    assert fill.sum() > 1000 and ink.sum() > fill.sum()
    # fill pixels carry the fill colour, outline pixels are dark
    close = np.all(np.abs(arr[fill][:, :3].astype(int) - np.array(st.fill)) <= 6, axis=1)
    assert close.mean() > 0.97
    outline_only = ink & ~fill
    assert np.median(arr[outline_only][:, :3]) < 60
    other = wm.render_overlay(wm.make_style(1280, 720, "onlyfans.com/someoneelse", "large"))[1]
    assert other.shape != fill.shape or (other != fill).any()


def _solid(rgb, n=6, h=60, w=300):
    return [np.full((h, w, 3), rgb, np.uint8) for _ in range(n)]


@pytest.mark.parametrize("bg,forbidden", [
    ((57, 255, 20), {"neon green", "electric lime"}),
    ((255, 234, 0), {"vivid yellow", "electric lime"}),
    ((0, 238, 255), {"bright cyan"}),
    ((255, 46, 214), {"hot pink"}),
])
def test_colour_avoids_background_colour(bg, forbidden):
    rgb, name, scores = wm.choose_color(_solid(bg))
    assert name not in forbidden
    assert name != "white"


@pytest.mark.parametrize("bg", [(10, 10, 10), (128, 128, 128), (245, 245, 245), (120, 60, 40)])
def test_prefers_vivid_colour_over_white_and_black(bg):
    rgb, name, scores = wm.choose_color(_solid(bg))
    assert name in {n for n, _ in wm.VIVID_COLORS}
    assert all(s["name"] != "black" for s in scores)


def test_textured_background_gets_high_contrast_colour():
    rng = np.random.default_rng(0)
    crops = [rng.integers(0, 120, (60, 300, 3), dtype=np.uint8) for _ in range(8)]  # dark busy texture
    rgb, name, scores = wm.choose_color(crops)
    best = next(s for s in scores if s["name"] == name)
    assert best["score"] >= 50


def test_position_avoids_motion():
    rng = np.random.default_rng(1)
    n, h, w = 24, 270, 480
    frames = np.full((n, h, w, 3), 90, np.uint8)
    # strong action in the bottom half
    for i in range(n):
        frames[i, h // 2:, :, :] = rng.integers(0, 255, (h - h // 2, w, 3), dtype=np.uint8)
    clip_ids = np.zeros(n, int)
    st = wm.choose_style(frames, clip_ids, 1920, 1080, TEXT, "large")
    assert st.position.startswith("top")


def test_position_avoids_faces(monkeypatch):
    class FakeDetector:
        available = True
        _yunet = object()
        _haar = None

        def detect(self, rgb):
            h, w = rgb.shape[:2]
            return [(int(w * 0.62), int(h * 0.02), int(w * 0.3), int(h * 0.3))]  # face at top right

    monkeypatch.setattr(wm, "face_detector", lambda: FakeDetector())
    frames = np.full((12, 270, 480, 3), 100, np.uint8)
    # some motion everywhere except the top so that "top" would otherwise win
    rng = np.random.default_rng(2)
    for i in range(12):
        frames[i, 150:, :] = rng.integers(0, 255, (120, 480, 3), dtype=np.uint8)
    st = wm.choose_style(frames, np.zeros(12, int), 1920, 1080, TEXT, "large")
    assert st.position != "top-right"
    assert st.selection["faces_detected_in_preview"] > 0


def test_manual_position_and_custom_colour_are_respected():
    frames = np.full((8, 270, 480, 3), 30, np.uint8)
    st = wm.choose_style(frames, np.zeros(8, int), 1280, 720, TEXT, "medium", position_mode="top-left",
                         color_mode="custom", custom_color="#FF0000")
    assert st.position == "top-left" and st.fill == (255, 0, 0)


def _render_with_overlay(tmp_path, style, bg_rgb=(30, 30, 30), w=640, h=360, frames=30, draw=True):
    """Encode a short H.264 clip with the overlay burned in, decode the overlay region back."""
    import subprocess
    from vcm.ffmpeg_tools import ffmpeg
    png = tmp_path / "wm.png"
    wm.render_overlay(style)[0].save(png)
    out = tmp_path / "wm_test.mp4"
    color = "0x%02X%02X%02X" % bg_rgb
    vf = (f"[0:v][1:v]overlay={style.x}:{style.y}:shortest=1,format=yuv420p" if draw
          else "[0:v]format=yuv420p")
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"color=c={color}:s={w}x{h}:r=30:d={frames / 30}", "-loop", "1", "-i", str(png), "-filter_complex", vf,
           "-c:v", "libx264", "-crf", "18", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", str(out)]
    subprocess.run(cmd, check=True)
    v = wm.WatermarkVerifier(style, w, h)
    cw, ch, cx, cy = v.crop
    for _, _, crop in FrameReader(str(out), cw, ch, pix_fmt="rgb24", crop=(cw, ch, cx, cy), scale=False):
        v.add(crop)
    return v.result()


def test_verification_accepts_correct_watermark(tmp_path):
    st = wm.make_style(640, 360, TEXT, "large", fill=(57, 255, 20), position="bottom-right")
    res = _render_with_overlay(tmp_path, st)
    assert res.passed, res.problems
    assert res.present_all_frames and res.readable and res.glyph_correlation_min > 0.8


def test_verification_rejects_wrong_or_missing_text(tmp_path):
    drawn = wm.make_style(640, 360, "onlyfans.com/somebodyelse", "large", fill=(57, 255, 20), position="bottom-right")
    expected = wm.make_style(640, 360, TEXT, "large", fill=(57, 255, 20), position="bottom-right")
    # render the wrong text, verify against the expected text
    import subprocess
    from vcm.ffmpeg_tools import ffmpeg
    png = tmp_path / "wrong.png"
    wm.render_overlay(drawn)[0].save(png)
    out = tmp_path / "wrong.mp4"
    subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x1E1E1E:s=640x360:r=30:d=1", "-loop", "1", "-i", str(png), "-filter_complex",
                    f"[0:v][1:v]overlay={expected.x}:{expected.y}:shortest=1,format=yuv420p", "-c:v", "libx264",
                    str(out)], check=True)
    v = wm.WatermarkVerifier(expected, 640, 360)
    cw, ch, cx, cy = v.crop
    for _, _, crop in FrameReader(str(out), cw, ch, pix_fmt="rgb24", crop=(cw, ch, cx, cy), scale=False):
        v.add(crop)
    assert not v.result().passed
    # no watermark at all
    res = _render_with_overlay(tmp_path, expected, draw=False)
    assert not res.passed and not res.present_all_frames


def test_verification_flags_unreadable_style(tmp_path):
    # dark grey text with a black outline on a black background is not readable
    st = wm.make_style(640, 360, TEXT, "large", fill=(40, 40, 40), fill_name="dark grey")
    res = _render_with_overlay(tmp_path, st, bg_rgb=(0, 0, 0))
    assert not res.readable and not res.passed
