"""Выбор режима сенсора, JPEG из YUV420 (как lores на Pi 4), наложение времени."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from fnav.camera.jpeg import LoresFrame, TimestampOverlay, encode_jpeg, yuv420_planes
from fnav.camera.picam import choose_mode, full_fov_modes

# режимы OV5647 на устройстве (rpicam-hello --list-cameras)
OV5647_MODES = [
    {"size": (640, 480), "crop_limits": (16, 0, 2560, 1920), "bit_depth": 10, "fps": 62.5},
    {"size": (1296, 972), "crop_limits": (0, 0, 2592, 1944), "bit_depth": 10, "fps": 46.34},
    {"size": (1920, 1080), "crop_limits": (348, 434, 1928, 1080), "bit_depth": 10, "fps": 32.81},
    {"size": (2592, 1944), "crop_limits": (0, 0, 2592, 1944), "bit_depth": 10, "fps": 15.63},
]


def test_choose_full_fov_mode():
    full = full_fov_modes(OV5647_MODES)
    assert [m["size"] for m in full] == [(2592, 1944), (1296, 972)]
    assert choose_mode(OV5647_MODES, (2592, 1944))["size"] == (2592, 1944)
    # если нужного размера нет — самый большой с полным полем зрения, но не обрезанный 1920x1080
    assert choose_mode(OV5647_MODES[:3], (2592, 1944))["size"] == (1296, 972)


def make_yuv(w: int, h: int, stride: int) -> np.ndarray:
    arr = np.zeros((h * 3 // 2, stride), np.uint8)
    arr[:h, :w] = 200  # светлый Y
    arr[h:, :] = 128  # нейтральная цветность
    return arr


def test_yuv420_planes_with_stride():
    arr = make_yuv(96, 72, 128)
    y, u, v = yuv420_planes(arr, 96, 72)
    assert y.shape == (72, 96) and u.shape == (36, 48) and v.shape == (36, 48)


def test_jpeg_from_yuv420_and_rgb():
    from io import BytesIO

    from PIL import Image

    j = encode_jpeg(LoresFrame("yuv420", make_yuv(96, 72, 128), 96, 72), 70)
    im = Image.open(BytesIO(j))
    assert im.size == (96, 72)
    px = np.asarray(im.convert("L"))
    assert 190 < px.mean() < 240
    j2 = encode_jpeg(LoresFrame("rgb", np.full((72, 96, 3), 50, np.uint8), 96, 72), 70)
    assert j2[:2] == b"\xff\xd8"


def test_timestamp_overlay_draws_into_y():
    y = np.full((576, 768), 100, np.uint8)
    TimestampOverlay(20).apply_y(y, datetime(2026, 10, 20, 12, 34, 56))
    assert (y == 235).sum() > 50 and (y == 16).sum() > 50
    assert (y[300:, :] == 100).all()  # только в углу
