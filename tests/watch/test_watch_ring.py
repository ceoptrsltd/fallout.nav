"""Кольцо lores-кадров в общей памяти (fnav/camera/shmring.py): круг записи-чтения, цена без читателя, камера."""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from fnav.camera import shmring
from fnav.camera.shmring import RingReader, RingWriter

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "camera"))


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def yuv(w: int, h: int, stride: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, (h * 3 // 2, stride), dtype=np.uint8)


def test_round_trip_yuv_with_stride(tmp_path):
    clock = Clock()
    path = tmp_path / "ring"
    reader = RingReader(path, cap=96 * 72 * 3, clock=clock)
    writer = RingWriter(path, clock=clock)
    reader.want(4.0)
    assert writer.wants_frame()  # файл появился — писатель его открыл
    exp = {"ExposureTime": 66000.0, "AnalogueGain": 8.0, "DigitalGain": 1.0, "Lux": 3.5}
    frames = []
    for i in range(5):  # больше, чем слотов: кольцо проворачивается
        clock.t += 0.25
        assert writer.wants_frame()
        a = yuv(96, 72, 128, i)
        frames.append(a)
        assert writer.write(a, "yuv420", 96, 72, exp)
        f = reader.read(i)
        assert f is not None and f.seq == i + 1
        assert (f.kind, f.w, f.h, f.stride) == ("yuv420", 96, 72, 128)
        assert np.array_equal(f.data, a)
        assert f.exposure == exp
        assert f.ts == clock.t
    assert reader.read(5) is None  # новее нет
    assert reader.latest == 5


def test_round_trip_rgb_without_exposure(tmp_path):
    clock = Clock()
    reader = RingReader(tmp_path / "ring", cap=64 * 48 * 3, clock=clock)
    writer = RingWriter(tmp_path / "ring", clock=clock)
    reader.want(4.0)
    assert writer.wants_frame()
    rgb = np.random.default_rng(1).integers(0, 255, (48, 64, 3), dtype=np.uint8)
    assert writer.write(rgb, "rgb", 64, 48, None)
    f = reader.read()
    assert f is not None and f.kind == "rgb" and f.data.shape == (48, 64, 3)
    assert np.array_equal(f.data, rgb)
    assert f.exposure == {}


def test_rate_limit_and_hold(tmp_path):
    clock = Clock()
    reader = RingReader(tmp_path / "ring", cap=1000, clock=clock)
    writer = RingWriter(tmp_path / "ring", clock=clock)
    reader.want(4.0, hold_s=3.0)
    assert writer.wants_frame()
    writer.write(np.zeros((6, 10), np.uint8), "yuv420", 10, 4)
    clock.t += 1 / 15  # следующий кадр камеры — рано
    assert not writer.wants_frame()
    clock.t += 0.2  # ≥ 0.8/fps после записи
    assert writer.wants_frame()
    clock.t += 3.0  # читатель не продлевал «хочу» — копирование прекращается
    assert not writer.wants_frame()
    reader.want(4.0)
    assert writer.wants_frame()
    reader.stop()  # перестал смотреть — сразу
    assert not writer.wants_frame()


def test_no_reader_is_cheap_and_creates_nothing(tmp_path):
    path = tmp_path / "ring"
    writer = RingWriter(path)
    n = 20000
    t0 = time.perf_counter()
    for _ in range(n):
        assert not writer.wants_frame()
    per_call = (time.perf_counter() - t0) / n
    assert per_call < 20e-6, f"{per_call * 1e6:.1f} us per frame without a reader"
    assert not path.exists()
    assert not writer.write(np.zeros((6, 10), np.uint8), "yuv420", 10, 4)  # файла нет — ничего не пишем

    # читатель есть, но не просит кадров — тоже дёшево (одно чтение из mmap)
    RingReader(path, cap=1000).open()
    writer._next_check = 0.0
    writer.wants_frame()
    t0 = time.perf_counter()
    for _ in range(n):
        assert not writer.wants_frame()
    per_call = (time.perf_counter() - t0) / n
    assert per_call < 20e-6, f"{per_call * 1e6:.1f} us per frame with an idle reader"


def test_torn_slot_is_rejected(tmp_path):
    clock = Clock()
    reader = RingReader(tmp_path / "ring", cap=1000, clock=clock)
    writer = RingWriter(tmp_path / "ring", clock=clock)
    reader.want(4.0)
    writer.wants_frame()
    writer.write(np.ones((6, 10), np.uint8), "yuv420", 10, 4)
    m = reader._m
    off = m.slot_off(1)
    struct.pack_into("<Q", m.mm, off, 4)  # писатель начал этот же слот для кадра 4
    assert reader.read() is None
    struct.pack_into("<Q", m.mm, off, 1)
    assert reader.read() is not None


def test_bad_file_is_replaced(tmp_path):
    path = tmp_path / "ring"
    path.write_bytes(b"garbage" * 100)
    reader = RingReader(path, cap=1000)
    reader.want(4.0)
    assert path.stat().st_size == shmring.file_size(1000)
    writer = RingWriter(path)
    assert writer.wants_frame()


def test_writer_error_never_raises(tmp_path):
    clock = Clock()
    reader = RingReader(tmp_path / "ring", cap=1000, clock=clock)
    writer = RingWriter(tmp_path / "ring", clock=clock)
    reader.want(4.0)
    writer.wants_frame()
    assert not writer.write(np.zeros((6, 10), np.uint8), "nv12", 10, 4)  # неизвестный формат — молча False


def test_camera_fake_source_feeds_the_ring(tmp_path):
    """fnav-camera --fake: FakeSource → CameraService → кольцо, пока читатель просит; JPEG-снимки — как раньше."""
    pytest.importorskip("av")
    from camhelpers import small_cam  # noqa: F401 — проверка, что тесты камеры доступны

    from fnav.camera.recorder import TimeSync
    from fnav.camera.service import CameraService
    from fnav.camera.sources import FakeSource

    def factory(settings, sink):
        return FakeSource(settings.camera, sink, size=(160, 120), lores_size=(96, 72))

    ring_path = tmp_path / "ring"
    svc = CameraService(root=tmp_path / "rec", settings_path=tmp_path / "settings.json", source_factory=factory,
                        rtsp_url=None, time_sync=TimeSync(lambda: True), ring_path=ring_path)
    reader = RingReader(ring_path, cap=96 * 72 * 3)
    svc.start()
    try:
        deadline = time.monotonic() + 15
        while svc.ring.frames == 0 and time.monotonic() < deadline:
            reader.want(5.0)
            time.sleep(0.1)
        f = reader.read()
        assert f is not None, "no frame in the ring"
        assert (f.kind, f.w, f.h) == ("rgb", 96, 72)
        n = svc.ring.frames
        time.sleep(1.0)
        assert svc.ring.frames - n <= 8  # ~5 кадров/с, а не 15
        while svc.hub.jpeg is None and time.monotonic() < deadline:  # JPEG для /snapshot.jpg никуда не делся
            time.sleep(0.1)
        assert svc.hub.jpeg is not None
        reader.stop()
        time.sleep(0.3)
        n = svc.ring.frames
        time.sleep(0.6)
        assert svc.ring.frames == n  # не смотрят — не копируем
    finally:
        svc.stop()
