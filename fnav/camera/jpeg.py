"""Кодирование lores-кадров в JPEG (simplejpeg на Pi, Pillow — запасной вариант) и наложение времени."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime

import numpy as np

log = logging.getLogger("fnav.camera.jpeg")

try:  # на Pi ставится вместе с picamera2 (python3-simplejpeg)
    import simplejpeg  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    simplejpeg = None


@dataclass
class LoresFrame:
    """kind: "rgb" (h, w, 3) или "yuv420" (h*3/2, stride) — как отдаёт Picamera2."""

    kind: str
    data: np.ndarray
    w: int
    h: int
    ts_us: int = 0


def yuv420_planes(arr: np.ndarray, w: int, h: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    stride = arr.shape[1]
    y = arr[:h, :w]
    u = arr[h:h + h // 4].reshape(h // 2, stride // 2)[:, : w // 2]
    v = arr[h + h // 4:h + h // 2].reshape(h // 2, stride // 2)[:, : w // 2]
    return y, u, v


def yuv420_to_rgb(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """BT.601 limited range → RGB (для Pillow)."""
    h, w = y.shape
    uu = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)[:h, :w].astype(np.float32) - 128.0
    vv = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)[:h, :w].astype(np.float32) - 128.0
    yy = (y.astype(np.float32) - 16.0) * 1.164
    r = yy + 1.596 * vv
    g = yy - 0.392 * uu - 0.813 * vv
    b = yy + 2.017 * uu
    return np.clip(np.dstack((r, g, b)), 0, 255).astype(np.uint8)


def encode_jpeg(frame: LoresFrame, quality: int) -> bytes:
    if frame.kind == "yuv420":
        y, u, v = yuv420_planes(frame.data, frame.w, frame.h)
        if simplejpeg is not None:
            return simplejpeg.encode_jpeg_yuv_planes(
                np.ascontiguousarray(y), np.ascontiguousarray(u), np.ascontiguousarray(v), quality=quality)
        rgb = yuv420_to_rgb(y, u, v)
    else:
        rgb = np.ascontiguousarray(frame.data[:, :, :3])
        if simplejpeg is not None:
            return simplejpeg.encode_jpeg(rgb, quality=quality, colorspace="RGB")
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ───────────────────────────── наложение времени ─────────────────────────────


def load_font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # старый Pillow
        return ImageFont.load_default()


class TimestampOverlay:
    """Рисует «ГГГГ-ММ-ДД ЧЧ:ММ:СС» в яркостную плоскость (Y) — подходит для YUV420 main/lores.

    Маска текста кэшируется на секунду, наложение — одна операция numpy.
    """

    def __init__(self, height_px: int) -> None:
        self.size = max(10, int(height_px))
        self._font = load_font(self.size)
        self._key: str | None = None
        self._mask: np.ndarray | None = None

    def _render(self, text: str) -> np.ndarray:
        from PIL import Image, ImageDraw

        img = Image.new("L", (self.size * len(text), int(self.size * 1.5)), 0)
        d = ImageDraw.Draw(img)
        d.text((2, 1), text, fill=255, font=self._font, stroke_width=max(1, self.size // 12), stroke_fill=128)
        bbox = img.getbbox() or (0, 0, 1, 1)
        return np.asarray(img.crop((0, 0, bbox[2] + 2, bbox[3] + 2)))

    def apply_y(self, y: np.ndarray, now: datetime | None = None) -> None:
        text = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        if text != self._key:
            self._key = text
            self._mask = self._render(text)
        m = self._mask
        assert m is not None
        x0 = y0 = max(4, self.size // 3)
        hh = min(m.shape[0], y.shape[0] - y0)
        ww = min(m.shape[1], y.shape[1] - x0)
        if hh <= 0 or ww <= 0:
            return
        region = y[y0:y0 + hh, x0:x0 + ww]
        mm = m[:hh, :ww]
        # 255 → белый текст, 128 → тёмная обводка
        region[mm > 192] = 235
        region[(mm > 64) & (mm <= 192)] = 16
