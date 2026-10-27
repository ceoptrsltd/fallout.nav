"""PNG icons of the admin web app (manifest, Home Screen, notifications) — docs/contracts/alarm.md §7.

    python web/admin/dev/make_icons.py        # writes web/admin/icons/*.png

The look of the admin: the green navigation arrow of the favicon with a glow, a thin amber compass ring,
on the dark panel colour. Drawn 4× larger and scaled down (Pillow polygons have no anti-aliasing).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "icons"
BG = (10, 12, 10)  # --bg
BG_HI = (24, 32, 22)  # centre of the radial gradient
GREEN = (93, 255, 138)  # --green
AMBER = (255, 182, 46)  # --amber
SS = 4  # supersampling

# the favicon arrow "M12 2 4.5 20.3 12 17l7.5 3.3z" (24-unit box)
ARROW = [(12.0, 2.0), (4.5, 20.3), (12.0, 17.0), (19.5, 20.3)]


def _radial(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    px = img.load()
    c = (size - 1) / 2
    for y in range(size):
        for x in range(size):
            t = min(1.0, math.hypot(x - c, y - c) / (size * 0.62))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(BG_HI, BG))
    return img


def _arrow_points(cx: float, cy: float, h: float) -> list[tuple[float, float]]:
    k = h / 18.3  # arrow height in units: 2 → 20.3
    return [(cx + (x - 12.0) * k, cy + (y - 11.15) * k) for x, y in ARROW]


def _emblem(size: int, scale: float, ring: bool = True) -> Image.Image:
    """Arrow (+ ring) of relative size `scale` on a transparent layer, size×size (already supersampled)."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = size / 2
    if ring:
        r = size * 0.36 * scale / 0.62
        w = max(2, round(size * 0.016))
        d.ellipse([c - r, c - r, c + r, c + r], outline=AMBER + (170,), width=w)
        for a in range(0, 360, 90):  # N E S W ticks
            ca, sa = math.cos(math.radians(a - 90)), math.sin(math.radians(a - 90))
            d.line([(c + ca * (r - w * 3.2), c + sa * (r - w * 3.2)), (c + ca * (r + w * 0.5), c + sa * (r + w * 0.5))],
                   fill=AMBER + (230,), width=round(w * 1.4))
    pts = _arrow_points(c, c, size * 0.5 * scale / 0.62)
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(pts, fill=GREEN + (200,))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.03))
    layer = Image.alpha_composite(layer, glow)
    ImageDraw.Draw(layer).polygon(pts, fill=GREEN + (255,))
    return layer


def _rounded_mask(size: int, radius: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def icon(px: int, *, rounded: bool, scale: float) -> Image.Image:
    big = px * SS
    img = _radial(big // 4).resize((big, big), Image.BICUBIC).convert("RGBA")
    img = Image.alpha_composite(img, _emblem(big, scale))
    if rounded:  # "any": a rounded tile with a hairline edge (maskable / Apple get a full square)
        ImageDraw.Draw(img).rounded_rectangle([SS, SS, big - 1 - SS, big - 1 - SS], radius=big * 0.2,
                                              outline=(58, 70, 52, 255), width=max(1, SS * px // 96))
        img.putalpha(ImageChops.multiply(img.getchannel("A"), _rounded_mask(big, big * 0.2)))
    return img.resize((px, px), Image.LANCZOS)


def badge(px: int) -> Image.Image:
    """Android status bar badge: white silhouette on transparent (only the alpha counts)."""
    big = px * SS
    img = Image.new("RGBA", (big, big), (255, 255, 255, 0))
    ImageDraw.Draw(img).polygon(_arrow_points(big / 2, big / 2, big * 0.78), fill=(255, 255, 255, 255))
    return img.resize((px, px), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    icon(192, rounded=True, scale=0.62).save(OUT / "icon-192.png", optimize=True)
    icon(512, rounded=True, scale=0.62).save(OUT / "icon-512.png", optimize=True)
    icon(512, rounded=False, scale=0.5).convert("RGB").save(OUT / "icon-maskable-512.png", optimize=True)  # safe zone
    icon(180, rounded=False, scale=0.6).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    badge(96).save(OUT / "badge-96.png", optimize=True)
    for p in sorted(OUT.glob("*.png")):
        print(p.relative_to(OUT.parents[2]), p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
