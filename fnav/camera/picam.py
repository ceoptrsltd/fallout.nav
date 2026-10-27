"""Источник с настоящей камеры (Picamera2 + аппаратный H264Encoder). Импорт picamera2 — ленивый."""

from __future__ import annotations

import contextlib
import logging
import struct
import time
from pathlib import Path
from typing import Any

from fnav.models import CameraSettings

from .jpeg import LoresFrame, TimestampOverlay
from .sources import FrameSink

log = logging.getLogger("fnav.camera.picam")

VIDIOC_S_CTRL = 0xC008561C
V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME = 0x009909E5

# Ночная ступень AE (замерено ночью в фургоне: 66 мс × 16 вдвое светлее штатных 66 мс × 8, 125 мс × 16 — ещё вдвое).
# Выдержку ограничивает FrameDurationLimits — его можно менять на ходу (едем/стоим), а таблицу тюнинга libcamera
# читает один раз на процесс, поэтому таблица всегда одна и та же.
NIGHT_GAIN = 16.0  # максимум аналогового усиления OV5647
NIGHT_SHUTTER_US = 1_000_000
V4L2_ROOT = Path("/sys/class/video4linux")


def sensor_model(root: Path = V4L2_ROOT) -> str | None:
    """Модель сенсора из имени V4L2-subdev ("ov5647 0-0036" → "ov5647") — до открытия камеры через libcamera."""
    for f in sorted(root.glob("v4l-subdev*/name")):
        with contextlib.suppress(OSError):
            name = f.read_text().split()
            if name and name[0][:2] in ("ov", "im"):  # ov5647, imx219, imx477, imx708…
                return name[0]
    return None


def night_tuning(tuning: dict) -> dict:
    """Дописать ко всем режимам экспозиции ступень «до NIGHT_SHUTTER_US, затем усиление до NIGHT_GAIN».

    Штатные ступени (и дневная картинка) не меняются: AE доходит до новой, только упёршись в старый максимум."""
    for algo in tuning.get("algorithms", []):
        agc = algo.get("rpi.agc")
        if agc is None:
            continue
        for ch in agc.get("channels", [agc]):
            for mode in ch.get("exposure_modes", {}).values():
                key = "shutter" if "shutter" in mode else "exposure"  # в новых libcamera — "exposure"
                if mode[key][-1] < NIGHT_SHUTTER_US:
                    mode[key].append(NIGHT_SHUTTER_US)
                    mode["gain"].append(max(NIGHT_GAIN, mode["gain"][-1]))
    return tuning


def load_night_tuning() -> dict | None:
    from picamera2 import Picamera2  # type: ignore[import-not-found]

    model = sensor_model()
    if model is None:
        log.warning("sensor model unknown: stock tuning (no night exposure)")
        return None
    try:
        return night_tuning(Picamera2.load_tuning_file(f"{model}.json"))
    except Exception as e:  # noqa: BLE001
        log.warning("tuning %s.json: %s: stock tuning (no night exposure)", model, e)
        return None


def full_fov_modes(modes: list[dict]) -> list[dict]:
    """Режимы, использующие всю матрицу (crop_limits = максимум), от больших к меньшим."""
    if not modes:
        return []
    max_w = max(m.get("crop_limits", (0, 0, 0, 0))[2] for m in modes)
    max_h = max(m.get("crop_limits", (0, 0, 0, 0))[3] for m in modes)
    full = [m for m in modes if tuple(m.get("crop_limits", (0, 0, 0, 0))[2:]) == (max_w, max_h)]
    return sorted(full, key=lambda m: m["size"][0] * m["size"][1], reverse=True)


def choose_mode(modes: list[dict], want: tuple[int, int]) -> dict:
    full = full_fov_modes(modes)
    for m in full:
        if tuple(m["size"]) == want:
            return m
    if full:
        return full[0]
    raise RuntimeError("no full field-of-view sensor mode")


class PicameraSource:
    def __init__(self, cam: CameraSettings, sink: FrameSink) -> None:
        self.cam = cam
        self.sink = sink
        self.picam2: Any = None
        self.encoder: Any = None
        self._live_enc: Any = None
        self.sensor_mode: str | None = None
        self.output_size = f"{cam.out_w}x{cam.out_h}"
        self._overlay_main: TimestampOverlay | None = None
        self._overlay_lores: TimestampOverlay | None = None
        self._lores_fmt = "YUV420"
        self._t0 = time.monotonic()
        self.max_frame_us = self._frame_us()
        self.exposure: dict[str, float] = {}  # ExposureTime/AnalogueGain/DigitalGain/Lux последнего кадра
        self._n = 0

    def _frame_us(self) -> int:
        return round(1e6 / self.cam.fps)

    def _frame_limits(self) -> tuple[int, int]:
        return (self._frame_us(), max(self._frame_us(), self.max_frame_us))

    def set_max_exposure(self, us: int) -> None:
        """Предел выдержки для AE (длиннее кадра — кадр удлиняется). Можно на ходу: запись не перезапускается."""
        self.max_frame_us = int(us)
        p = self.picam2
        if p is not None:
            p.set_controls({"FrameDurationLimits": self._frame_limits()})

    # ── конфигурация ──
    def _controls(self) -> dict:
        cam = self.cam
        ctl: dict[str, Any] = {"FrameDurationLimits": self._frame_limits(), "ExposureValue": float(cam.ev)}
        try:
            from libcamera import controls  # type: ignore[import-not-found]

            ctl["AeExposureMode"] = {
                "normal": controls.AeExposureModeEnum.Normal,
                "short": controls.AeExposureModeEnum.Short,
                "long": controls.AeExposureModeEnum.Long,
            }[cam.exposure_mode]
        except Exception:  # noqa: BLE001
            ctl["AeExposureMode"] = {"normal": 0, "short": 1, "long": 2}[cam.exposure_mode]
        return ctl

    def _make_config(self, sensor: dict) -> Any:
        from libcamera import Transform  # type: ignore[import-not-found]

        cam = self.cam
        return self.picam2.create_video_configuration(
            main={"size": (cam.out_w, cam.out_h), "format": "YUV420"},
            lores={"size": (cam.lores.w, cam.lores.h), "format": "YUV420"},
            sensor=sensor,
            transform=Transform(hflip=int(cam.hflip), vflip=int(cam.vflip)),
            controls=self._controls(),
            encode="main",
            display=None,
        )

    def start(self) -> None:
        from picamera2 import Picamera2  # type: ignore[import-not-found]
        from picamera2.encoders import H264Encoder  # type: ignore[import-not-found]
        from picamera2.outputs import Output  # type: ignore[import-not-found]

        cam = self.cam
        self.picam2 = Picamera2(tuning=load_night_tuning())
        want = (cam.sensor_w, cam.sensor_h)
        try:
            cfg = self._make_config({"output_size": want, "bit_depth": 10})
            self.picam2.configure(cfg)
            got = tuple(self.picam2.camera_configuration()["sensor"]["output_size"])
            if got != want:
                raise RuntimeError(f"sensor mode {got} != {want}")
        except Exception as e:  # noqa: BLE001
            log.warning("direct sensor config failed (%s), enumerating sensor modes", e)
            mode = choose_mode(self.picam2.sensor_modes, want)
            cfg = self._make_config({"output_size": tuple(mode["size"]), "bit_depth": mode["bit_depth"]})
            self.picam2.configure(cfg)
        actual = self.picam2.camera_configuration()
        s = actual["sensor"]
        self.sensor_mode = f"{s['output_size'][0]}x{s['output_size'][1]}"
        m = actual["main"]["size"]
        self.output_size = f"{m[0]}x{m[1]}"
        crop_max = self.picam2.camera_properties.get("ScalerCropMaximum")
        if crop_max:  # всё поле зрения выбранного режима, без цифрового зума
            self.picam2.set_controls({"ScalerCrop": tuple(crop_max)})
        log.info("ScalerCropMaximum=%s PixelArraySize=%s", crop_max,
                 self.picam2.camera_properties.get("PixelArraySize"))
        lo = actual["lores"]
        self._lores_fmt = lo.get("format", "YUV420")
        log.info("camera configured: sensor %s/%s bit, main %s, lores %s %s", self.sensor_mode,
                 s.get("bit_depth"), self.output_size, lo["size"], self._lores_fmt)

        if cam.overlay_timestamp:
            self._overlay_main = TimestampOverlay(max(16, cam.out_h // 30))
            self._overlay_lores = TimestampOverlay(max(12, cam.lores.h // 30))
            self.picam2.pre_callback = self._pre_callback
        self.picam2.post_callback = self._post_callback

        sink = self.sink
        t0 = self._t0

        class _Out(Output):  # type: ignore[misc, valid-type]
            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                if audio:
                    return
                ts = int(timestamp) if timestamp is not None else int((time.monotonic() - t0) * 1e6)
                sink.on_video(bytes(frame), bool(keyframe), ts)

        kwargs: dict[str, Any] = {"bitrate": cam.bitrate_kbps * 1000, "repeat": True, "iperiod": cam.gop}
        try:
            self.encoder = H264Encoder(framerate=cam.fps, **kwargs)
        except TypeError:  # старый Picamera2
            self.encoder = H264Encoder(**kwargs)
        self.picam2.start_recording(self.encoder, _Out())
        log.info("camera started (%s, %d fps, %d kbps, gop %d)", self.output_size, cam.fps,
                 cam.bitrate_kbps, cam.gop)

    # ── живое видео: второй аппаратный энкодер на lores (запись main не трогаем) ──
    def start_live(self, bitrate_kbps: int, on_video) -> None:
        from picamera2.encoders import H264Encoder  # type: ignore[import-not-found]
        from picamera2.outputs import Output  # type: ignore[import-not-found]

        p = self.picam2
        if p is None:
            raise RuntimeError("camera is not running")
        if self._live_enc is not None:
            return
        t0 = self._t0

        class _LiveOut(Output):  # type: ignore[misc, valid-type]
            # поток опроса V4L2: только быстрые операции, иначе встанет камера (буферы lores не вернутся)
            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                if audio:
                    return
                ts = int(timestamp) if timestamp is not None else int((time.monotonic() - t0) * 1e6)
                on_video(bytes(frame), bool(keyframe), ts)

        fps = self.cam.fps
        kwargs: dict[str, Any] = {"bitrate": int(bitrate_kbps) * 1000, "repeat": True, "iperiod": fps}
        try:
            enc = H264Encoder(framerate=fps, **kwargs)
        except TypeError:  # старый Picamera2
            enc = H264Encoder(**kwargs)
        # start_encoder запускает энкодер и только потом добавляет его в рассылку кадров;
        # если запуск упал — main это не затрагивает
        p.start_encoder(enc, _LiveOut(), name="lores")
        self._live_enc = enc

    def stop_live(self) -> None:
        enc, self._live_enc = self._live_enc, None
        p = self.picam2
        if enc is None or p is None:
            return
        # Сначала убрать из рассылки (под замком камеры): Encoder.stop() держит свой замок, пока ждёт
        # поток опроса, и поток камеры, отдающий ему кадр, ждал бы вместе с ним — пострадала бы запись.
        with p.lock:
            p.encoders.discard(enc)
        enc.stop()

    def stop(self) -> None:
        self._live_enc = None  # stop_recording остановит все энкодеры, включая live
        p, self.picam2 = self.picam2, None
        if p is None:
            return
        try:
            p.stop_recording()
        except Exception:
            log.exception("stop_recording failed")
        try:
            p.close()
        except Exception:
            log.exception("picam2.close failed")

    def request_keyframe(self) -> None:
        enc = self.encoder
        if enc is None:
            return
        fn = getattr(enc, "force_key_frame", None)
        if callable(fn):
            fn()
            return
        vd = getattr(enc, "vd", None)
        if vd is not None:
            import fcntl as _fcntl

            _fcntl.ioctl(vd, VIDIOC_S_CTRL, struct.pack("Ii", V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME, 1))

    # ── колбэки камеры (поток libcamera — только быстрые операции) ──
    def _y_view(self, arr: Any, w: int, h: int) -> Any:
        if arr.ndim == 1:
            arr = arr.reshape((h * 3 // 2, -1))
        return arr[:h, :w]

    def _pre_callback(self, request: Any) -> None:
        from picamera2 import MappedArray  # type: ignore[import-not-found]

        try:
            cam = self.cam
            with MappedArray(request, "main") as m:
                self._overlay_main.apply_y(self._y_view(m.array, cam.out_w, cam.out_h))  # type: ignore[union-attr]
            with MappedArray(request, "lores") as m:
                self._overlay_lores.apply_y(self._y_view(m.array, cam.lores.w, cam.lores.h))  # type: ignore[union-attr]
        except Exception:
            log.exception("overlay failed")

    def _post_callback(self, request: Any) -> None:
        self._n += 1
        if self._n % 8 == 1:
            with contextlib.suppress(Exception):
                md = request.get_metadata()
                self.exposure = {k: float(md[k]) for k in ("ExposureTime", "AnalogueGain", "DigitalGain", "Lux")
                                 if md.get(k) is not None}
        if not self.sink.wants_lores():
            return
        try:
            w, h = self.cam.lores.w, self.cam.lores.h
            arr = request.make_array("lores")
            if self._lores_fmt == "YUV420" or arr.ndim == 1 or arr.ndim == 2:
                if arr.ndim == 1:
                    arr = arr.reshape((h * 3 // 2, -1))
                self.sink.on_lores(LoresFrame("yuv420", arr, w, h))
            else:  # Pi 5: XBGR8888/XRGB8888 (в памяти B,G,R,X для XRGB/RGB888)
                rgb = arr[:, :w, :3]
                if self._lores_fmt in ("XRGB8888", "RGB888"):
                    rgb = rgb[:, :, ::-1]
                self.sink.on_lores(LoresFrame("rgb", rgb.copy(), w, h))
        except Exception:
            log.exception("lores capture failed")
