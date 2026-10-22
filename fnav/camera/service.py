"""Сервис камеры: источник → (регистратор, RTSP, MJPEG), супервизор с повтором, обслуживание хранилища."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fnav import paths
from fnav.models import CameraStatus, Settings

from .jpeg import LoresFrame
from .live import LiveHub, LiveMux
from .mjpeg import JpegWorker, MjpegHub
from .recorder import Recorder, TimeSync
from .rtsp import DEFAULT_URL, RtspPublisher
from .sources import FakeSource, FrameSink, Source
from .storage import GB, cleanup, disk_free, repair_latest, segment_reserve_bytes, stats

log = logging.getLogger("fnav.camera")

NO_SPACE = "No space left: all recordings are protected"

SourceFactory = Callable[[Settings, FrameSink], Source]


def load_settings(path: Path | None = None, fallback: Settings | None = None) -> Settings:
    path = path or paths.settings_file()
    try:
        return Settings.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except Exception:
        log.exception("cannot read %s, using %s", path, "previous settings" if fallback else "defaults")
        return fallback or Settings()


def default_source_factory(fake: bool) -> SourceFactory:
    def make(settings: Settings, sink: FrameSink) -> Source:
        if fake:
            return FakeSource(settings.camera, sink)
        from .picam import PicameraSource  # лениво: на ПК picamera2 нет

        return PicameraSource(settings.camera, sink)

    return make


class _Pipeline:
    def __init__(self) -> None:
        self.source: Source | None = None
        self.recorder: Recorder | None = None
        self.rtsp: RtspPublisher | None = None
        self.jpeg: JpegWorker | None = None
        self.started_mono = time.monotonic()


class CameraService(FrameSink):
    def __init__(
        self,
        *,
        fake: bool = False,
        root: Path | None = None,
        settings_path: Path | None = None,
        source_factory: SourceFactory | None = None,
        rtsp_url: str | None = DEFAULT_URL,
        segment_s_override: float | None = None,
        time_sync: TimeSync | None = None,
        free_fn: Callable[[Path], int] = disk_free,
        retry_s: float = 5.0,
        frame_timeout_s: float = 5.0,
        housekeeping_s: float = 30.0,
        live_grace_s: float = 5.0,
    ) -> None:
        self.fake = fake
        self.root = Path(root) if root else paths.recordings_dir()
        self.settings_path = Path(settings_path) if settings_path else paths.settings_file()
        self.settings = load_settings(self.settings_path)
        self.source_factory = source_factory or default_source_factory(fake)
        self.rtsp_url = rtsp_url
        self.segment_s_override = segment_s_override
        self.time_sync = time_sync or TimeSync()
        self.free_fn = free_fn
        self.retry_s = retry_s
        self.frame_timeout_s = frame_timeout_s
        self.housekeeping_s = housekeeping_s
        self.hub = MjpegHub()
        # живое видео (/live.ts): второй энкодер на lores, только пока есть зрители
        self.live = LiveHub(self.set_live_demand, grace_s=live_grace_s)
        self._live_wanted = False
        self._live_src: Source | None = None  # источник, на котором сейчас работает live-энкодер
        self._live_mux = LiveMux(self.settings.camera.fps)
        self._live_bitrate = self.settings.camera.live_bitrate_kbps

        self._p: _Pipeline | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._hk_wake = threading.Event()
        self._restart = threading.Event()
        self._source_error: str | None = None
        self._error: str | None = None
        self._no_space = False
        self._over_limit = False
        self._state_starting = False
        self._threads: list[threading.Thread] = []
        self._repaired = False
        self.free_gb: float | None = None
        self.recordings_count = 0
        self.recordings_bytes = 0

    # ───────────── публичный API ─────────────
    def start(self) -> None:
        self._stop.clear()
        for target, name in ((self._supervise, "cam-supervisor"), (self._housekeeping, "cam-housekeeping")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._hk_wake.set()
        for t in self._threads:
            t.join(15)
        self._threads.clear()
        self._stop_pipeline()

    def restart(self) -> None:
        log.info("pipeline restart requested")
        self._restart.set()
        self._wake.set()

    def reload(self) -> bool:
        """Перечитать настройки; True — изменились параметры камеры, конвейер перезапускается.

        Битрейт живого видео запись не трогает: перезапускается только live-энкодер (если он работает)."""
        new = load_settings(self.settings_path, fallback=self.settings)
        old_cam, new_cam = self.settings.camera, new.camera
        changed = _rec_params(new_cam) != _rec_params(old_cam)
        self.settings = new
        self._hk_wake.set()
        if changed:
            log.info("camera settings changed -> restart")
            self.restart()
        elif new_cam.live_bitrate_kbps != old_cam.live_bitrate_kbps:
            with self._lock:
                if self._live_src is not None:
                    log.info("live bitrate changed -> restart live encoder")
                    self._live_stop()
                    self._sync_live()
        return changed

    # ───────────── живое видео ─────────────
    @property
    def live_running(self) -> bool:
        return self._live_src is not None

    def set_live_demand(self, wanted: bool) -> str | None:
        """Есть ли зрители /live.ts (вызывается из потока). → текст ошибки или None."""
        with self._lock:
            if wanted:
                p = self._p
                if p is None or p.source is None:
                    return "Camera is not running"
                self._live_wanted = True
                err = self._sync_live()
                if err:
                    self._live_wanted = False
                return err
            self._live_wanted = False
            self._sync_live()
            return None

    def _sync_live(self) -> str | None:
        """Привести live-энкодер к желаемому состоянию (под self._lock). Ошибки — только в лог и наружу."""
        p = self._p
        src = p.source if p else None
        if self._live_wanted and src is not None and self._live_src is not src:
            start = getattr(src, "start_live", None)
            if start is None:
                return "Live stream is not supported by this camera source"
            cam = self.settings.camera
            self._live_mux.new_session(cam.fps)
            try:
                start(cam.live_bitrate_kbps, self._on_live_video)
            except Exception as e:
                log.exception("live encoder start failed")
                return f"Live stream: {e}"
            self._live_src = src
            self._live_bitrate = cam.live_bitrate_kbps
            log.info("live encoder started (%dx%d, %d kbps)", cam.lores.w, cam.lores.h, cam.live_bitrate_kbps)
        elif not self._live_wanted and self._live_src is not None:
            self._live_stop()
        return None

    def _live_stop(self) -> None:
        src, self._live_src = self._live_src, None
        if src is None:
            return
        try:
            src.stop_live()  # type: ignore[attr-defined]
        except Exception:
            log.exception("live encoder stop failed")
        log.info("live encoder stopped")

    def _on_live_video(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        """Поток энкодера: никаких ожиданий — только мультиплексирование и передача в asyncio."""
        try:
            r = self._live_mux.push(au, keyframe, ts_us)
            if r is not None:
                self.live.publish(*r)
        except Exception:
            log.exception("live mux failed")

    def protect_current(self) -> str | None:
        p = self._p
        if p is None or p.recorder is None:
            return None
        rel = p.recorder.protect_current()
        if rel:
            log.info("protected: %s", rel)
        return rel

    def status(self) -> CameraStatus:
        p = self._p
        rec = p.recorder if p else None
        src = p.source if p else None
        seg = rec.segment_rel if rec else None
        fps = kbps = None
        if rec is not None and time.monotonic() - rec.last_frame_mono < 3.0:
            fps, kbps = rec.rates()
        error = self._error or (rec.write_error if rec else None)
        if self._no_space:
            error = NO_SPACE
        if error:
            state = "error"
        elif p is None:
            state = "starting" if self._state_starting else "stopped"
        elif seg:
            state = "recording"
        else:
            state = "starting"
        return CameraStatus(
            state=state,
            recording=bool(seg),
            current_segment=seg,
            segment_started_utc=rec.segment_started_utc if seg and rec else None,
            fps_actual=fps,
            bitrate_kbps=kbps,
            sensor_mode=getattr(src, "sensor_mode", None),
            output_size=getattr(src, "output_size", None),
            frames_dropped=rec.frames_dropped if rec else 0,
            error=error,
            recordings_count=self.recordings_count,
            recordings_gb=round(self.recordings_bytes / GB, 3),
            free_gb=round(self.free_gb, 2) if self.free_gb is not None else None,
            time_synced=rec.segment_time_synced if seg and rec else self.time_sync.value,
        )

    # ───────────── FrameSink (вызывается из потока источника) ─────────────
    def on_video(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        p = self._p
        if p is None:
            return
        if p.recorder is not None:
            p.recorder.push(au, keyframe, ts_us)
        if p.rtsp is not None:
            p.rtsp.push(au, keyframe, ts_us)

    def on_lores(self, frame: LoresFrame) -> None:
        p = self._p
        if p is not None and p.jpeg is not None:
            p.jpeg.submit(frame)

    def wants_lores(self) -> bool:
        p = self._p
        return bool(p and p.jpeg and p.jpeg.wants_frame())

    def on_error(self, msg: str) -> None:
        self._source_error = msg
        self._wake.set()

    # ───────────── конвейер ─────────────
    def _segment_s(self) -> float:
        if self.segment_s_override:
            return self.segment_s_override
        return self.settings.camera.segment_minutes * 60.0

    def _start_pipeline(self) -> None:
        s = self.settings
        cam = s.camera
        p = _Pipeline()
        p.recorder = Recorder(
            self.root, segment_s=self._segment_s(), fsync_interval_s=cam.fsync_interval_s, gop=cam.gop,
            fps=cam.fps, time_sync=self.time_sync, request_keyframe=self._request_keyframe,
            on_segment_start=lambda _path: self._hk_wake.set(),
        )
        p.recorder.paused = self._no_space
        p.jpeg = JpegWorker(self.hub, cam.lores.fps, cam.lores.jpeg_quality)
        if self.rtsp_url:
            p.rtsp = RtspPublisher(self.rtsp_url, fps=cam.fps, width=cam.out_w, height=cam.out_h)
        p.recorder.start()
        p.jpeg.start()
        if p.rtsp:
            p.rtsp.start()
        self._source_error = None
        self._p = p  # sink начинает принимать кадры
        p.source = self.source_factory(s, self)
        p.source.start()
        p.started_mono = time.monotonic()
        with self._lock:
            if self._live_wanted:  # конвейер перезапустился, а зрители остались
                self._sync_live()

    def _request_keyframe(self) -> None:
        p = self._p
        if p and p.source:
            p.source.request_keyframe()

    def _stop_pipeline(self) -> None:
        with self._lock:
            p = self._p
            if p is None:
                return
            if self._live_src is not None and self._live_src is p.source:
                self._live_src = None  # source.stop() остановит и live-энкодер
            if p.source is not None:
                try:
                    p.source.stop()
                except Exception:
                    log.exception("source stop failed")
            self._p = None
            if p.rtsp is not None:  # не задерживаем перезапуск записи ожиданием ffmpeg
                threading.Thread(target=p.rtsp.stop, name="rtsp-stop", daemon=True).start()
            for part in (p.recorder, p.jpeg):
                if part is None:
                    continue
                try:
                    part.stop()
                except Exception:
                    log.exception("stop %s failed", type(part).__name__)

    def _supervise(self) -> None:
        if not self._repaired:
            self._repaired = True
            try:
                repair_latest(self.root)
            except Exception:
                log.exception("repair failed")
        while not self._stop.is_set():
            if self._restart.is_set():
                self._restart.clear()
                self._stop_pipeline()
            if self._p is None:
                self._state_starting = True
                try:
                    self._start_pipeline()
                    log.info("pipeline started")
                except Exception as e:
                    log.exception("pipeline start failed")
                    self._error = f"Camera: {e}"
                    self._stop_pipeline()
                    self._state_starting = False
                    self._wake.wait(self.retry_s)
                    self._wake.clear()
                    continue
                finally:
                    self._state_starting = False
            p = self._p
            assert p is not None
            rec = p.recorder
            problem = self._source_error
            now = time.monotonic()
            last = rec.last_frame_mono if rec else 0.0
            if problem is None and now - max(last, p.started_mono) > self.frame_timeout_s:
                problem = f"Camera: no frames for {self.frame_timeout_s:.0f} s"
            if problem:
                log.error("%s — restarting in %.0f s", problem, self.retry_s)
                self._error = problem
                self._stop_pipeline()
                if self._stop.wait(self.retry_s):
                    break
                continue
            if self._error and rec and rec.segment_rel:
                self._error = None  # снова пишем
            self._wake.wait(0.5)
            self._wake.clear()
        self._stop_pipeline()

    # ───────────── обслуживание хранилища ─────────────
    def housekeeping_once(self) -> None:
        s = self.settings
        reserve = segment_reserve_bytes(s.camera.bitrate_kbps, s.camera.segment_minutes)
        min_free = int(s.storage.min_free_gb * GB)
        p = self._p
        rec = p.recorder if p else None
        # лимит объёма — с местом под следующий сегмент: после очистки записи + сегмент ≤ лимита
        max_total = max(0, int(s.storage.max_recordings_gb * GB) - reserve)
        res = cleanup(self.root, min_free + reserve, current=rec.current_path if rec else None,
                      free_fn=self.free_fn, max_total_bytes=max_total)
        self.free_gb = res.free_gb
        if res.over_limit and not self._over_limit:
            log.warning("recordings take %.2f GB > limit %.1f GB: only protected recordings are left to delete",
                        res.total_bytes / GB, s.storage.max_recordings_gb)
        self._over_limit = res.over_limit
        if res.free_bytes < min_free:
            if not self._no_space:
                log.error("%s (free %.2f GB)", NO_SPACE, res.free_gb)
            self._no_space = True
        elif self._no_space and res.ok:
            log.info("free space restored (%.2f GB), recording resumes", res.free_gb)
            self._no_space = False
        if rec is not None:
            rec.paused = self._no_space
        self.recordings_count, self.recordings_bytes = stats(self.root)

    def _housekeeping(self) -> None:
        last_clean = 0.0
        while not self._stop.is_set():
            self.time_sync.refresh()
            now = time.monotonic()
            if self._hk_wake.is_set() or now - last_clean >= self.housekeeping_s:
                self._hk_wake.clear()
                last_clean = now
                try:
                    self.housekeeping_once()
                except Exception:
                    log.exception("housekeeping failed")
            self._hk_wake.wait(min(10.0, self.housekeeping_s))


def _rec_params(cam) -> object:
    """Параметры камеры, от которых зависит запись (всё, кроме битрейта живого видео)."""
    return cam.model_copy(update={"live_bitrate_kbps": 0})
