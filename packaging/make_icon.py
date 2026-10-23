"""Generate the application icon (resources/icons/app.png and app.ico)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "resources" / "icons"


def make(size: int = 1024) -> Image.Image:
    s = size
    grad = Image.new("RGB", (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * s)
            r = int(124 * (1 - t) + 0 * t)
            g = int(92 * (1 - t) + 212 * t)
            b = int(255 * (1 - t) + 255 * t)
            px[x, y] = (r, g, b)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=255)
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(icon)
    # film strip holes
    for i in range(6):
        y = int(s * (0.12 + i * 0.15))
        for x in (int(s * 0.07), int(s * 0.86)):
            d.rounded_rectangle((x, y, x + int(s * 0.07), y + int(s * 0.08)), radius=int(s * 0.015),
                                fill=(255, 255, 255, 90))
    # play triangle with soft shadow
    tri = [(s * 0.36, s * 0.27), (s * 0.36, s * 0.73), (s * 0.74, s * 0.50)]
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x + s * 0.015, y + s * 0.02) for x, y in tri], fill=(0, 0, 40, 120))
    icon = Image.alpha_composite(icon, shadow.filter(ImageFilter.GaussianBlur(s * 0.02)))
    ImageDraw.Draw(icon).polygon(tri, fill=(255, 255, 255, 255))
    # "30" badge
    font = ImageFont.truetype(str(ROOT / "resources" / "fonts" / "Poppins-Bold.ttf"), int(s * 0.2))
    d = ImageDraw.Draw(icon)
    bx0, by0, bx1, by1 = s * 0.56, s * 0.66, s * 0.93, s * 0.92
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=int(s * 0.06), fill=(18, 20, 26, 235))
    tb = d.textbbox((0, 0), "30", font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    d.text(((bx0 + bx1 - tw) / 2 - tb[0], (by0 + by1 - th) / 2 - tb[1]), "30", font=font, fill=(57, 255, 20, 255))
    return icon


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    big = make(1024)
    big.resize((256, 256), Image.LANCZOS).save(OUT / "app.png")
    big.save(OUT / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icons written to", OUT)
