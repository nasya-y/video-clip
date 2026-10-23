"""Watermark styling, automatic colour/position choice and rendered-output verification.

The watermark is drawn once with Pillow (bold font, dark outline, soft drop shadow)
into a transparent PNG and overlaid by FFmpeg for the whole compilation, so the text,
colour, size and position are identical in every frame.  Because the exact pixels of
the overlay are known, the rendered video can be checked frame by frame: the glyph
shapes must be present in the expected place with the expected colour and contrast.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, replace
from typing import Optional, Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .paths import face_model_path, font_path
from .settings import WATERMARK_SIZES

log = logging.getLogger(__name__)

# Vivid, high-visibility fills. White is only used if clearly more visible.
VIVID_COLORS: list[tuple[str, tuple[int, int, int]]] = [
    ("neon green", (57, 255, 20)),
    ("bright cyan", (0, 238, 255)),
    ("vivid yellow", (255, 234, 0)),
    ("hot pink", (255, 46, 214)),
    ("electric orange", (255, 140, 0)),
    ("electric lime", (200, 255, 0)),
]
NEUTRAL_COLORS: list[tuple[str, tuple[int, int, int]]] = [("white", (255, 255, 255))]
POSITIONS = ["bottom-right", "bottom-left", "top-right", "top-left", "bottom-center", "top-center"]
_POSITION_BIAS = {p: 0.02 * i for i, p in enumerate(POSITIONS)}


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(rgb: Sequence[int]) -> str:
    return "#%02X%02X%02X" % tuple(int(c) for c in rgb[:3])


def _lab(rgb_u8: np.ndarray) -> np.ndarray:
    """RGB uint8 (..., 3) -> CIE Lab float32 (..., 3) with L in 0..100."""
    arr = np.ascontiguousarray(rgb_u8, dtype=np.float32).reshape(-1, 1, 3) / 255.0
    lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
    return lab.reshape(np.shape(rgb_u8)[:-1] + (3,))


def _rel_luminance(rgb: np.ndarray) -> np.ndarray:
    c = np.asarray(rgb, np.float32) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def contrast_ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


@dataclass
class WatermarkStyle:
    text: str
    font_px: int
    fill: tuple[int, int, int]
    fill_name: str
    outline_px: int
    outline: tuple[int, int, int] = (0, 0, 0)
    shadow_offset: int = 2
    shadow_blur: int = 2
    shadow_alpha: float = 0.65
    plate: bool = False          # semi-transparent dark backing box (used if readability fails)
    position: str = "bottom-right"
    # placement in output pixels (top-left of the overlay image) and overlay size
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    # glyph ink box inside the frame (x0, y0, x1, y1)
    text_box: tuple[int, int, int, int] = (0, 0, 0, 0)
    selection: dict = field(default_factory=dict)

    def report(self) -> dict:
        d = {
            "text": self.text,
            "colour_hex": rgb_to_hex(self.fill),
            "colour_name": self.fill_name,
            "outline_colour_hex": rgb_to_hex(self.outline),
            "outline_px": self.outline_px,
            "drop_shadow": {"offset_px": self.shadow_offset, "blur_px": self.shadow_blur,
                            "opacity": self.shadow_alpha},
            "backing_plate": self.plate,
            "font": "Poppins Bold",
            "font_size_px": self.font_px,
            "position": self.position,
            "overlay_xy_px": [self.x, self.y],
            "overlay_size_px": [self.width, self.height],
            "text_box_px": list(self.text_box),
            "text_height_fraction_of_frame": None,
        }
        d.update({k: v for k, v in self.selection.items()})
        return d


class _Fonts:
    _cache: dict[int, ImageFont.FreeTypeFont] = {}

    @classmethod
    def get(cls, px: int) -> ImageFont.FreeTypeFont:
        if px not in cls._cache:
            try:
                cls._cache[px] = ImageFont.truetype(str(font_path()), px)
            except OSError:  # pragma: no cover - bundled font missing
                log.warning("Bundled font missing, falling back to default font")
                cls._cache[px] = ImageFont.load_default(px)
        return cls._cache[px]


def _style_metrics(text: str, font_px: int, outline_px: int, shadow_offset: int, shadow_blur: int,
                   plate: bool) -> tuple[int, int, int, int, tuple[int, int, int, int]]:
    """Overlay image size, text origin and ink box inside the overlay."""
    font = _Fonts.get(font_px)
    x0, y0, x1, y1 = font.getbbox(text, stroke_width=outline_px)
    pad = shadow_offset + 2 * shadow_blur + 2
    if plate:
        pad += int(round(font_px * 0.35))
    w = (x1 - x0) + 2 * pad + shadow_offset
    h = (y1 - y0) + 2 * pad + shadow_offset
    ox, oy = pad - x0, pad - y0
    ink = (pad, pad, pad + (x1 - x0), pad + (y1 - y0))
    return w, h, ox, oy, ink


def make_style(frame_w: int, frame_h: int, text: str, size_key: str, fill=(57, 255, 20),
               fill_name: str = "neon green", position: str = "bottom-right",
               outline_scale: float = 1.0, plate: bool = False) -> WatermarkStyle:
    frac = WATERMARK_SIZES.get(size_key, WATERMARK_SIZES["large"])["font_frac"]
    short = min(frame_w, frame_h)
    font_px = max(14, int(round(short * frac)))
    # keep the text comfortably inside the frame width
    while font_px > 10:
        outline_px = max(2, int(round(font_px * 0.10 * outline_scale)))
        w, h, _, _, ink = _style_metrics(text, font_px, outline_px, max(1, font_px // 18),
                                         max(1, font_px // 14), plate)
        if (ink[2] - ink[0]) <= 0.86 * frame_w:
            break
        font_px -= 1
    outline_px = max(2, int(round(font_px * 0.10 * outline_scale)))
    style = WatermarkStyle(text=text, font_px=font_px, fill=tuple(fill), fill_name=fill_name,
                           outline_px=outline_px, shadow_offset=max(1, font_px // 18),
                           shadow_blur=max(1, font_px // 14), plate=plate)
    return place(style, frame_w, frame_h, position)


def place(style: WatermarkStyle, frame_w: int, frame_h: int, position: str) -> WatermarkStyle:
    w, h, _, _, ink = _style_metrics(style.text, style.font_px, style.outline_px, style.shadow_offset,
                                     style.shadow_blur, style.plate)
    margin = max(8, int(round(min(frame_w, frame_h) * 0.045)))
    iw, ih = ink[2] - ink[0], ink[3] - ink[1]
    vert, horiz = position.split("-")
    if horiz == "left":
        ix = margin
    elif horiz == "right":
        ix = frame_w - margin - iw
    else:
        ix = (frame_w - iw) // 2
    iy = margin if vert == "top" else frame_h - margin - ih
    x, y = ix - ink[0], iy - ink[1]
    return replace(style, position=position, x=int(x), y=int(y), width=int(w), height=int(h),
                   text_box=(int(ix), int(iy), int(ix + iw), int(iy + ih)))


def render_overlay(style: WatermarkStyle) -> tuple[Image.Image, np.ndarray, np.ndarray]:
    """Return (RGBA overlay, fill mask, ink mask) - masks are bool arrays of the overlay size."""
    w, h, ox, oy, _ = _style_metrics(style.text, style.font_px, style.outline_px, style.shadow_offset,
                                     style.shadow_blur, style.plate)
    font = _Fonts.get(style.font_px)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if style.plate:
        plate = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(plate)
        r = int(style.font_px * 0.3)
        d.rounded_rectangle((style.shadow_offset, style.shadow_offset, w - style.shadow_offset - 1,
                             h - style.shadow_offset - 1), radius=r, fill=(0, 0, 0, 150))
        img = Image.alpha_composite(img, plate)
    # drop shadow
    sh = Image.new("L", (w, h), 0)
    ImageDraw.Draw(sh).text((ox + style.shadow_offset, oy + style.shadow_offset), style.text, font=font,
                            fill=255, stroke_width=style.outline_px, stroke_fill=255)
    sh = sh.filter(ImageFilter.GaussianBlur(style.shadow_blur))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(sh.point(lambda v: int(v * style.shadow_alpha)))
    img = Image.alpha_composite(img, shadow)
    # outlined text
    txt = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(txt).text((ox, oy), style.text, font=font, fill=tuple(style.fill) + (255,),
                             stroke_width=style.outline_px, stroke_fill=tuple(style.outline) + (255,))
    img = Image.alpha_composite(img, txt)
    fill_l = Image.new("L", (w, h), 0)
    ImageDraw.Draw(fill_l).text((ox, oy), style.text, font=font, fill=255)
    ink_l = Image.new("L", (w, h), 0)
    ImageDraw.Draw(ink_l).text((ox, oy), style.text, font=font, fill=255, stroke_width=style.outline_px,
                               stroke_fill=255)
    fill_mask = np.asarray(fill_l) >= 250
    ink_mask = np.asarray(ink_l) >= 128
    return img, fill_mask, ink_mask


# --------------------------------------------------------------------------- automatic choices

def score_colors(bg_pixels_rgb: Sequence[np.ndarray], candidates=None) -> list[dict]:
    """Visibility of each candidate colour against background crops (one array per frame)."""
    candidates = candidates or (VIVID_COLORS + NEUTRAL_COLORS)
    labs = [_lab(px.reshape(-1, 3)) for px in bg_pixels_rgb if px.size]
    out = []
    for name, rgb in candidates:
        c = _lab(np.array([[rgb]], np.uint8))[0, 0]
        per_frame = []
        for lab in labs:
            de = np.sqrt(((lab - c) ** 2).sum(axis=-1))
            per_frame.append(float(np.percentile(de, 20)))
        if per_frame:
            pf = np.asarray(per_frame)
            score = 0.6 * float(np.percentile(pf, 10)) + 0.4 * float(np.median(pf))
        else:
            score = 0.0
        out.append({"name": name, "hex": rgb_to_hex(rgb), "rgb": rgb, "score": round(score, 2),
                    "vivid": (name, rgb) in VIVID_COLORS})
    out.sort(key=lambda d: -d["score"])
    return out


def choose_color(bg_pixels_rgb: Sequence[np.ndarray], exclude: Sequence[str] = ()) -> tuple[tuple[int, int, int], str, list[dict]]:
    scores = [s for s in score_colors(bg_pixels_rgb) if s["name"] not in exclude]
    vivid = [s for s in scores if s["vivid"]]
    neutral = [s for s in scores if not s["vivid"]]
    best = vivid[0] if vivid else scores[0]
    # white only if clearly more visible than the best vivid colour
    if neutral and vivid and neutral[0]["score"] > 1.25 * vivid[0]["score"] + 5:
        best = neutral[0]
    return tuple(best["rgb"]), best["name"], scores


class FaceDetector:
    def __init__(self) -> None:
        self._yunet = None
        self._haar = None
        self._size = None
        try:
            if hasattr(cv2, "FaceDetectorYN") and face_model_path().is_file():
                self._yunet = cv2.FaceDetectorYN.create(str(face_model_path()), "", (320, 320), 0.6, 0.3, 50)
        except cv2.error as exc:  # pragma: no cover
            log.info("YuNet face detector unavailable: %s", exc)
            self._yunet = None
        if self._yunet is None and hasattr(cv2, "CascadeClassifier"):
            try:
                path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                cc = cv2.CascadeClassifier(path)
                if not cc.empty():
                    self._haar = cc
            except Exception:  # pragma: no cover
                self._haar = None

    @property
    def available(self) -> bool:
        return self._yunet is not None or self._haar is not None

    def detect(self, rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        if self._yunet is not None:
            if self._size != (w, h):
                self._yunet.setInputSize((w, h))
                self._size = (w, h)
            _, faces = self._yunet.detect(bgr)
            if faces is None:
                return []
            return [tuple(int(v) for v in f[:4]) for f in faces]
        if self._haar is not None:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            found = self._haar.detectMultiScale(gray, 1.1, 5, minSize=(max(16, w // 30), max(16, w // 30)))
            return [tuple(int(v) for v in f) for f in found]
        return []


_face_detector: Optional[FaceDetector] = None


def face_detector() -> FaceDetector:
    global _face_detector
    if _face_detector is None:
        _face_detector = FaceDetector()
    return _face_detector


def _box_scaled(box, sx: float, sy: float, w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return (max(0, int(x0 * sx)), max(0, int(y0 * sy)), min(w, int(np.ceil(x1 * sx))), min(h, int(np.ceil(y1 * sy))))


def choose_style(frames_rgb: np.ndarray, clip_ids: np.ndarray, out_w: int, out_h: int, text: str,
                 size_key: str, position_mode: str = "auto", color_mode: str = "auto",
                 custom_color: str = "#39FF14", exclude_colors: Sequence[str] = (),
                 outline_scale: float = 1.0, plate: bool = False) -> WatermarkStyle:
    """Pick position and colour from preview frames of the compilation (without watermark).

    ``frames_rgb`` are evenly spaced preview frames (N, h, w, 3) of the assembled
    compilation, ``clip_ids`` tells which clip each preview frame belongs to.
    """
    n, ph, pw = frames_rgb.shape[:3]
    sx, sy = pw / out_w, ph / out_h
    base = make_style(out_w, out_h, text, size_key, outline_scale=outline_scale, plate=plate)
    candidates = POSITIONS if position_mode == "auto" else [position_mode]

    # where is the action?  frame-difference energy between neighbouring preview frames
    gray = frames_rgb.astype(np.float32).mean(axis=3)
    motion = np.zeros((ph, pw), np.float32)
    pairs = 0
    for i in range(1, n):
        if clip_ids[i] == clip_ids[i - 1]:
            motion += np.abs(gray[i] - gray[i - 1])
            pairs += 1
    if pairs:
        motion /= pairs
    motion_mean = float(motion.mean()) + 1e-3
    # where are the faces?
    det = face_detector()
    face_map = np.zeros((ph, pw), np.float32)
    face_count = 0
    if det.available:
        step = max(1, n // 40)
        sampled = 0
        for i in range(0, n, step):
            sampled += 1
            for (fx, fy, fw, fh) in det.detect(np.ascontiguousarray(frames_rgb[i])):
                face_count += 1
                ex, ey = int(fw * 0.25), int(fh * 0.35)
                face_map[max(0, fy - ey):min(ph, fy + fh + ey), max(0, fx - ex):min(pw, fx + fw + ex)] += 1.0
        if sampled:
            face_map /= sampled
    # detail (busy texture makes text harder to read and usually is content)
    detail = np.zeros((ph, pw), np.float32)
    for i in range(0, n, max(1, n // 20)):
        detail += np.abs(cv2.Laplacian(gray[i], cv2.CV_32F))
    detail /= max(1, len(range(0, n, max(1, n // 20))))
    detail_mean = float(detail.mean()) + 1e-3
    yy, xx = np.mgrid[0:ph, 0:pw]
    center_prior = 1.0 - np.hypot((xx - pw / 2) / (pw / 2), (yy - ph / 2) / (ph / 2)) / np.sqrt(2)

    evaluations = []
    for pos in candidates:
        st = place(base, out_w, out_h, pos)
        x0, y0, x1, y1 = _box_scaled(st.text_box, sx, sy, pw, ph)
        if x1 <= x0 or y1 <= y0:
            continue
        crops = [frames_rgb[i, y0:y1, x0:x1] for i in range(n)]
        if color_mode == "custom":
            rgb = hex_to_rgb(custom_color)
            cscore = score_colors(crops, [("custom", rgb)])[0]["score"]
            cname = "custom"
        else:
            rgb, cname, cs = choose_color(crops, exclude_colors)
            cscore = next(s["score"] for s in cs if s["name"] == cname)
        face_cost = float(face_map[y0:y1, x0:x1].mean())
        motion_cost = float(motion[y0:y1, x0:x1].mean()) / motion_mean
        detail_cost = float(detail[y0:y1, x0:x1].mean()) / detail_mean
        centre_cost = float(center_prior[y0:y1, x0:x1].mean())
        readability = min(1.0, cscore / 60.0)
        cost = (4.0 * face_cost + 1.0 * motion_cost + 0.4 * detail_cost + 0.8 * centre_cost
                + 1.0 * (1.0 - readability) + _POSITION_BIAS.get(pos, 0.0))
        evaluations.append({"position": pos, "cost": round(cost, 4), "face_overlap": round(face_cost, 4),
                            "motion_ratio": round(motion_cost, 3), "detail_ratio": round(detail_cost, 3),
                            "colour": cname, "colour_hex": rgb_to_hex(rgb), "colour_visibility": round(cscore, 2),
                            "_rgb": rgb})
    evaluations.sort(key=lambda e: e["cost"])
    best = evaluations[0]
    style = place(replace(base, fill=tuple(best["_rgb"]), fill_name=best["colour"]), out_w, out_h, best["position"])
    for e in evaluations:
        e.pop("_rgb", None)
    style.selection = {
        "position_mode": position_mode,
        "colour_mode": color_mode,
        "faces_detected_in_preview": face_count,
        "face_detector": "YuNet" if det._yunet is not None else ("Haar cascade" if det._haar is not None else "none"),
        "position_candidates": evaluations,
    }
    return style


# --------------------------------------------------------------------------- verification

@dataclass
class WatermarkCheck:
    passed: bool
    readable: bool
    present_all_frames: bool
    frames_checked: int
    frames_present: int
    glyph_iou_min: float
    glyph_correlation_min: float
    colour_delta_e_median: float
    fill_vs_outline_contrast: float
    background_separation_p10: float
    text_height_px: int
    text_height_fraction: float
    size_ok: bool
    problems: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 3)
        return d


class WatermarkVerifier:
    """Streaming check of decoded crops of the overlay rectangle in the rendered video."""

    def __init__(self, style: WatermarkStyle, frame_w: int, frame_h: int):
        self.style = style
        self.frame_w, self.frame_h = frame_w, frame_h
        _, fill_mask, ink_mask = render_overlay(style)
        vx0, vy0 = max(0, -style.x), max(0, -style.y)
        vx1 = min(style.width, frame_w - style.x)
        vy1 = min(style.height, frame_h - style.y)
        self.crop = (int(vx1 - vx0), int(vy1 - vy0), int(style.x + vx0), int(style.y + vy0))
        fill_mask = fill_mask[vy0:vy1, vx0:vx1]
        ink_mask = ink_mask[vy0:vy1, vx0:vx1]
        self.fill_mask, self.ink_mask = fill_mask, ink_mask
        k3 = np.ones((3, 3), np.uint8)
        erode_px = 1 if style.font_px < 40 else 2
        fill_core = cv2.erode(fill_mask.astype(np.uint8), k3, iterations=erode_px).astype(bool)
        self.fill_core = fill_core if fill_core.sum() >= 20 else fill_mask
        outline_only = ink_mask & ~cv2.dilate(fill_mask.astype(np.uint8), k3, iterations=1).astype(bool)
        oc = cv2.erode(outline_only.astype(np.uint8), k3, iterations=1).astype(bool) if style.outline_px >= 4 else outline_only
        self.outline_core = oc if oc.sum() >= 20 else outline_only
        ink_u8 = ink_mask.astype(np.uint8)
        inner = max(2, style.shadow_offset + style.shadow_blur + 1)
        outer = inner + max(2, style.font_px // 8)
        self.ring = cv2.dilate(ink_u8, k3, iterations=outer).astype(bool) & \
            ~cv2.dilate(ink_u8, k3, iterations=inner).astype(bool)
        self.target = _lab(np.array([[style.fill]], np.uint8))[0, 0]
        # expected luminance pattern inside the glyphs+outline (fully opaque there)
        overlay = np.asarray(render_overlay(style)[0].convert("RGB"), np.float32)[vy0:vy1, vx0:vx1]
        exp = _rel_luminance(overlay)[ink_mask]
        self.expected_luma = (exp - exp.mean()) / (exp.std() + 1e-6)
        self.glyph_ncc: list[float] = []
        self.n = 0
        self.present = 0
        self.ious: list[float] = []
        self.des: list[float] = []
        self.contrasts: list[float] = []
        self.seps: list[float] = []

    def add(self, crop: np.ndarray) -> None:
        if crop.shape[:2] != self.fill_mask.shape:
            self.n += 1
            self.ious.append(0.0)
            self.glyph_ncc.append(0.0)
            return
        self.n += 1
        lab = _lab(crop)
        de = np.sqrt(((lab - self.target) ** 2).sum(axis=-1))
        de_fill = de[self.fill_core]
        self.des.append(float(np.median(de_fill)))
        close = de < 22.0
        inter = np.logical_and(close, self.fill_mask).sum()
        union = np.logical_or(close & self.ink_mask, self.fill_mask).sum()
        iou = float(inter / max(1, union))
        self.ious.append(iou)
        # the exact glyph shapes must be there: luminance pattern correlation inside the text area
        got = _rel_luminance(crop[self.ink_mask])
        got = (got - got.mean()) / (got.std() + 1e-6)
        ncc = float(np.mean(got * self.expected_luma))
        self.glyph_ncc.append(ncc)
        if ncc >= 0.7 and float(np.median(de_fill)) <= 20.0:
            self.present += 1
        lum_fill = float(np.median(_rel_luminance(crop[self.fill_core])))
        lum_outline = float(np.median(_rel_luminance(crop[self.outline_core]))) if self.outline_core.any() else 0.0
        self.contrasts.append(contrast_ratio(lum_fill, lum_outline))
        if self.ring.any():
            bg_lab = lab[self.ring]
            sep_fill = float(np.percentile(np.sqrt(((bg_lab - self.target) ** 2).sum(axis=-1)), 20))
            bg_lum = _rel_luminance(crop[self.ring])
            hi = np.maximum(bg_lum, lum_outline)
            lo = np.minimum(bg_lum, lum_outline)
            outline_sep = float(np.percentile((hi + 0.05) / (lo + 0.05), 20))
            # visible if the fill stands out from the background, or the dark outline does
            self.seps.append(max(sep_fill, 12.0 * (outline_sep - 1.0)))
        else:
            self.seps.append(100.0)

    def result(self) -> WatermarkCheck:
        style, n = self.style, self.n
        problems: list[str] = []
        text_h = style.text_box[3] - style.text_box[1]
        frac_h = text_h / max(1, min(self.frame_w, self.frame_h))
        size_ok = (text_h >= 12 and frac_h >= 0.03
                   and (style.text_box[2] - style.text_box[0]) <= 0.92 * self.frame_w
                   and style.text_box[0] >= 0 and style.text_box[1] >= 0
                   and style.text_box[2] <= self.frame_w and style.text_box[3] <= self.frame_h)
        if n == 0:
            return WatermarkCheck(False, False, False, 0, 0, 0.0, 0.0, 99.0, 0.0, 0.0, int(text_h), float(frac_h),
                                  bool(size_ok), ["watermark region could not be decoded"])
        present_all = self.present == n
        contrast_min = float(np.min(self.contrasts)) if self.contrasts else 0.0
        sep_p10 = float(np.percentile(self.seps, 10)) if self.seps else 0.0
        readable = contrast_min >= 4.5 and sep_p10 >= 18.0
        if not present_all:
            problems.append(f"watermark text not clearly present in {n - self.present} of {n} frames")
        if contrast_min < 4.5:
            problems.append(f"fill/outline contrast too low ({contrast_min:.1f}:1)")
        if sep_p10 < 18.0:
            problems.append(f"watermark blends into the background (separation {sep_p10:.1f})")
        if not size_ok:
            problems.append("watermark size or placement outside the safe range")
        passed = present_all and readable and size_ok
        return WatermarkCheck(passed, readable, present_all, n, self.present,
                              float(np.min(self.ious)) if self.ious else 0.0,
                              float(np.min(self.glyph_ncc)) if self.glyph_ncc else 0.0,
                              float(np.median(self.des)) if self.des else 99.0, contrast_min, sep_p10,
                              int(text_h), float(frac_h), bool(size_ok), problems)


def verify_watermark(crops_rgb, style: WatermarkStyle, frame_w: int, frame_h: int) -> WatermarkCheck:
    v = WatermarkVerifier(style, frame_w, frame_h)
    for c in crops_rgb:
        v.add(c)
    return v.result()
