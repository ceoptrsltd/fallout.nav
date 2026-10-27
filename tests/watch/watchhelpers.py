"""Общие помощники тестов fnav-watch: синтетические кадры, заглушки детекторов, ручные часы."""

from __future__ import annotations

import numpy as np

from fnav.watch.detect import Det, Face
from fnav.watch.frame import Frame

W, H = 768, 576


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def gray_frame(t: float, y: np.ndarray, exposure: dict | None = None) -> Frame:
    """Кадр YUV420 (h*3/2, w) с заданной яркостью и серыми цветами."""
    h, w = y.shape
    data = np.full((h * 3 // 2, w), 128, np.uint8)
    data[:h] = y
    return Frame(t, "yuv420", data, w, h, exposure or {}, wall=t)


def scene(rng: np.random.Generator, noise: float = 2.0, base: np.ndarray | None = None) -> np.ndarray:
    """Неподвижная сцена (градиент + текстура) с шумом сенсора."""
    if base is None:
        yy, xx = np.mgrid[0:H, 0:W]
        base = (60 + 80 * xx / W + 30 * np.sin(yy / 23.0)).astype(np.float32)
    img = base + rng.normal(0, noise, base.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def with_box(img: np.ndarray, box: tuple[float, float, float, float], value: int = 230) -> np.ndarray:
    out = img.copy()
    x1, y1, x2, y2 = box
    out[int(y1 * H):int(y2 * H), int(x1 * W):int(x2 * W)] = value
    return out


def person(cx: float, bottom: float, h: float, score: float = 0.8) -> Det:
    w = h * 0.4 * H / W
    return Det((cx - w / 2, bottom - h, cx + w / 2, bottom), score)


def face(cx: float, cy: float, w: float, score: float = 0.9, contrast: float = 30.0) -> Face:
    """Правдоподобное лицо: глаза на 0.38 высоты, нос в центре, рот на 0.75 (доли кадра)."""
    hh = w * W / H * 1.15  # высота рамки в долях высоты кадра
    x1, y1 = cx - w / 2, cy - hh / 2
    lm = [(x1 + 0.3 * w, y1 + 0.38 * hh), (x1 + 0.7 * w, y1 + 0.38 * hh), (cx, y1 + 0.58 * hh),
          (x1 + 0.35 * w, y1 + 0.78 * hh), (x1 + 0.65 * w, y1 + 0.78 * hh)]
    return Face((x1, y1, x1 + w, y1 + hh), score, lm, contrast)


class StubPerson:
    """Детектор людей по сценарию: fn(t) → список Det."""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[float] = []
        self.t = 0.0

    def detect(self, bgr):
        self.calls.append(self.t)
        return self.fn(self.t)


class StubFace:
    region = (0.2, 0.05, 0.8, 0.7)

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[float] = []
        self.t = 0.0

    def detect(self, bgr):
        self.calls.append(self.t)
        return self.fn(self.t)
