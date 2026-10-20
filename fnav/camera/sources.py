"""Источники кадров. Общий интерфейс для настоящей камеры и синтетического источника.

Источник отдаёт в ``FrameSink``:
  * ``on_video(au, keyframe, ts_us)`` — H.264 access unit (Annex-B), метка времени сенсора в мкс;
  * ``on_lores(LoresFrame)`` — малый кадр для MJPEG (только если ``wants_lores()``);
  * ``on_error(msg)`` — фатальная ошибка источника (сервис перезапустит конвейер).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from fractions import Fraction
from typing import Protocol

import numpy as np

from fnav.models import CameraSettings

from .jpeg import LoresFrame, load_font

log = logging.getLogger("fnav.camera.source")


class FrameSink(Protocol):
    def on_video(self, au: bytes, keyframe: bool, ts_us: int) -> None: ...
    def on_lores(self, frame: LoresFrame) -> None: ...
    def wants_lores(self) -> bool: ...
    def on_error(self, msg: str) -> None: ...


class Source(Protocol):
    sensor_mode: str | None
    output_size: str | None

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def request_keyframe(self) -> None: ...


H264_ENCODERS = ("libx264", "libopenh264", "h264_v4l2m2m", "h264_mf", "h264")


def open_h264_encoder(w: int, h: int, fps: int, gop: int, bitrate_kbps: int):
    """Первый доступный в сборке PyAV H.264-кодер, настроенный как аппаратный на Pi (без B-кадров)."""
    import av

    last: Exception | None = None
    for name in H264_ENCODERS:
        try:
            cc = av.CodecContext.create(name, "w")
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        cc.width, cc.height = w, h
        cc.pix_fmt = "yuv420p"
        cc.time_base = Fraction(1, fps)
        cc.framerate = Fraction(fps, 1)
        cc.bit_rate = bitrate_kbps * 1000
        cc.gop_size = gop
        cc.max_b_frames = 0
        if name == "libx264":
            cc.options = {"preset": "ultrafast", "tune": "zerolatency",
                          "x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0:repeat-headers=1"}
        try:
            cc.open()
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        return cc, name
    raise RuntimeError(f"no H.264 encoder available in PyAV: {last}")


class FakeSource:
    """Синтетическая картинка (движущиеся полосы, квадрат, таймкод) → H.264 через PyAV.

    realtime=False — без задержек, метки времени синтетические (ускоренное время для тестов).
    """

    def __init__(self, cam: CameraSettings, sink: FrameSink, *, realtime: bool = True,
                 max_frames: int | None = None, size: tuple[int, int] | None = None,
                 lores_size: tuple[int, int] | None = None) -> None:
        self.cam = cam
        self.sink = sink
        self.realtime = realtime
        self.max_frames = max_frames
        self.w, self.h = size or (cam.out_w, cam.out_h)
        self.lw, self.lh = lores_size or (cam.lores.w, cam.lores.h)
        self.sensor_mode = f"fake {cam.sensor_w}x{cam.sensor_h}"
        self.output_size = f"{self.w}x{self.h}"
        self._stop = threading.Event()
        self._force_key = False
        self._thr: threading.Thread | None = None
        self.encoder_name: str | None = None
        self.frames = 0
        self.done = threading.Event()

    def start(self) -> None:
        self._cc, self.encoder_name = open_h264_encoder(self.w, self.h, self.cam.fps, self.cam.gop,
                                                        self.cam.bitrate_kbps)
        log.info("fake source: %s %dx%d@%d, encoder %s", self.sensor_mode, self.w, self.h,
                 self.cam.fps, self.encoder_name)
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="fake-src", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thr:
            self._thr.join(5)

    def request_keyframe(self) -> None:
        self._force_key = True

    # ── рисование ──
    def _prepare(self) -> None:
        from PIL import ImageFont  # noqa: F401 — проверка наличия Pillow

        w, h = self.w, self.h
        x = np.linspace(0, 1, w, dtype=np.float32)
        y = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        base = np.empty((h, w, 3), np.uint8)
        base[..., 0] = (x * 200 + 30).astype(np.uint8)
        base[..., 1] = (y * 180 + 40).astype(np.uint8)
        base[..., 2] = 110
        self._base = base
        self._font = load_font(max(12, h // 14))

    def _render(self, i: int, t: float) -> np.ndarray:
        from PIL import Image, ImageDraw

        w, h = self.w, self.h
        img = np.roll(self._base, (i * 4) % w, axis=1)
        im = Image.fromarray(img, "RGB")
        d = ImageDraw.Draw(im)
        bs = max(8, h // 6)
        px = int((w - bs) * (0.5 + 0.5 * np.sin(i / 20.0)))
        py = int((h - bs) * (0.5 + 0.5 * np.cos(i / 31.0)))
        d.rectangle((px, py, px + bs, py + bs), fill=(240, 240, 240), outline=(0, 0, 0), width=3)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if self.realtime else f"t={t:9.3f}"
        d.text((10, 8), f"FNAV FAKE  {stamp}  #{i}", fill=(255, 255, 0), font=self._font,
               stroke_width=2, stroke_fill=(0, 0, 0))
        return np.asarray(im)

    def _run(self) -> None:
        import av
        from av.video.frame import PictureType
        from PIL import Image

        try:
            self._prepare()
            period = 1.0 / self.cam.fps
            t0 = time.monotonic()
            i = 0
            ts_map: dict[int, int] = {}
            while not self._stop.is_set():
                if self.max_frames is not None and i >= self.max_frames:
                    break
                if self.realtime:
                    target = t0 + i * period
                    delay = target - time.monotonic()
                    if delay > 0 and self._stop.wait(delay):
                        break
                    ts_us = int((time.monotonic() - t0) * 1e6)  # как метка сенсора: момент «съёмки»
                else:
                    ts_us = int(i * period * 1e6)
                rgb = self._render(i, ts_us / 1e6)
                vf = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuv420p")
                vf.pts = i
                if self._force_key:
                    self._force_key = False
                    vf.pict_type = PictureType.I
                ts_map[i] = ts_us
                for pkt in self._cc.encode(vf):
                    pts_us = ts_map.pop(pkt.pts, ts_us) if pkt.pts is not None else ts_us
                    self.sink.on_video(bytes(pkt), bool(pkt.is_keyframe), pts_us)
                if self.sink.wants_lores():
                    lo = np.asarray(Image.fromarray(rgb, "RGB").resize((self.lw, self.lh)))
                    self.sink.on_lores(LoresFrame("rgb", lo, self.lw, self.lh, ts_us))
                i += 1
                self.frames = i
        except Exception as e:
            log.exception("fake source failed")
            self.sink.on_error(f"Fake source: {e}")
        finally:
            self.done.set()
