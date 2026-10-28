"""Общие помощники тестов fnav-watch: синтетические кадры, заглушки детекторов, ручные часы, «камера» для кольца."""

from __future__ import annotations

import threading
import time

import numpy as np

from fnav.camera.shmring import RingWriter
from fnav.watch.detect import Det, Face
from fnav.watch.frame import Frame

W, H = 768, 576
RW, RH = 768, 432  # кольцо задней камеры: 768 по ширине, высота по пропорциям потока (16:9)


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


def scene(rng: np.random.Generator, noise: float = 2.0, base: np.ndarray | None = None, h: int = H,
          w: int = W) -> np.ndarray:
    """Неподвижная сцена (градиент + текстура) с шумом сенсора."""
    if base is None:
        yy, xx = np.mgrid[0:h, 0:w]
        base = (60 + 80 * xx / w + 30 * np.sin(yy / 23.0)).astype(np.float32)
    img = base + rng.normal(0, noise, base.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def with_box(img: np.ndarray, box: tuple[float, float, float, float], value: int = 230) -> np.ndarray:
    out = img.copy()
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    out[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)] = value
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


def wait_until(fn, timeout: float = 10.0, what: str = "condition"):
    """Ждать (в потоке теста), пока fn() не вернёт истину; → её значение."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


class Feeder:
    """«Камера»: кладёт кадры в кольцо, пока fnav-watch просит (как CameraService.wants_lores/on_lores)."""

    def __init__(self, path, script) -> None:
        self.writer = RingWriter(path)
        self.script = script  # t → картинка Y (h, w)
        self.stop = threading.Event()
        self.t0 = time.monotonic()
        self.thr = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        while not self.stop.is_set():
            if self.writer.wants_frame():
                y = self.script(time.monotonic() - self.t0)
                data = np.full((y.shape[0] * 3 // 2, y.shape[1]), 128, np.uint8)
                data[: y.shape[0]] = y
                self.writer.write(data, "yuv420", y.shape[1], y.shape[0], {})
            time.sleep(1 / 30)


class StubFace:
    region = (0.2, 0.05, 0.8, 0.7)

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[float] = []
        self.t = 0.0

    def detect(self, bgr):
        self.calls.append(self.t)
        return self.fn(self.t)
