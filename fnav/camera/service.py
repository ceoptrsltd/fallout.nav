"""Сервис камеры: источник → (регистратор, RTSP, MJPEG, кольцо lores для fnav-watch), супервизор с повтором,
обслуживание хранилища. Два экземпляра: передняя (Picamera2) и задняя (IP-камера по RTSP, ``camera_id="rear"``:
записи в recordings/rear/, свой порт, своё кольцо, путь MediaMTX rear)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from fnav import paths
from fnav.core.secretsfile import KEY_REAR_CAMERA_PASSWORD, read_secrets
from fnav.models import CameraId, CameraSettings, CameraStatus, Settings

from .jpeg import LoresFrame
from .live import LiveHub, LiveMux
from .mjpeg import JpegWorker, MjpegHub
from .recorder import PROTECT_EDGE_S, Recorder, TimeSync, lock_segment
from .rtsp import DEFAULT_URL, RtspPublisher
from .rtsp_pull import AAC_FRAME, AAC_RATE
from .shmring import RingWriter
from .shmring import default_path as ring_default_path
from .sound import SoundMonitor
from .sources import FakeSource, FrameSink, Source
from .storage import GB, cleanup, disk_free, loop_minimum, repair_latest, segment_reserve_bytes, stats

log = logging.getLogger("fnav.camera")

NO_SPACE = "No space left: all recordings are protected"
REAR_DIR = "rear"  # записи задней камеры: {recordings_dir}/rear/YYYY-MM-DD/…
AAC_FRAME_PTS = AAC_FRAME * 90000 // AAC_RATE  # кадр AAC живого видео, 90 кГц
REAR_RTSP_URL = "rtsp://127.0.0.1:8554/rear"
REAR_PORT = 8083
BUSY_S = 120.0  # чужой файл моложе — текущий сегмент другой камеры: очистка его не трогает
LAST_SEGMENT_S = 600.0  # сегмента сейчас нет (камера пропала, конвейер перезапущен) — защищаем прошлый, если он моложе
LOOP_MIN_S = 3600.0  # петлю (незащищённые записи) лимит объёма не сокращает меньше часа записи (camera.md)

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


def max_frame_us(cam: CameraSettings, moving: bool | None) -> int:
    """Предел длительности кадра (= выдержки) для AE: стоим — свой, едем или неизвестно — свой; не короче кадра."""
    ms = cam.max_exposure_ms_parked if moving is False else cam.max_exposure_ms_driving
    return max(round(1e6 / cam.fps), ms * 1000)


def rear_camera_settings(s: Settings) -> CameraSettings:
    """Параметры конвейера для задней камеры: частота/битрейт — оценки (поток задаёт камера), lores — свои,
    длина сегмента и fsync — общие с передней. GOP у IP-камер длинный — ждём ключевой кадр терпеливее."""
    r = s.rear_camera
    return s.camera.model_copy(update={
        "fps": r.fps, "gop": r.fps * 4, "bitrate_kbps": r.bitrate_kbps, "lores": r.lores,
        "hflip": False, "vflip": False, "overlay_timestamp": False,
    })


def read_rear_password(path: Path | None = None) -> str:
    try:
        return read_secrets(path).get(KEY_REAR_CAMERA_PASSWORD, "")
    except OSError:
        return ""


def default_source_factory(fake: bool, camera_id: CameraId = "front") -> SourceFactory:
    def make(settings: Settings, sink: FrameSink) -> Source:
        if camera_id == "rear":
            if fake:
                return FakeSource(rear_camera_settings(settings), sink, size=(1280, 720))
            from .rtspsrc import RtspCameraSource

            return RtspCameraSource(settings.rear_camera, sink, password=read_rear_password())
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
        ring_path: Path | None = None,
        camera_id: CameraId = "front",
        sound_dir: Path | None = None,
    ) -> None:
        self.fake = fake
        self.camera_id: CameraId = camera_id
        self.rear = camera_id == "rear"
        self.rel_prefix = f"{REAR_DIR}/" if self.rear else ""  # пути записей — от общего каталога записей
        self.root = Path(root) if root else (
            paths.recordings_dir() / REAR_DIR if self.rear else paths.recordings_dir())
        self.settings_path = Path(settings_path) if settings_path else paths.settings_file()
        self.settings = load_settings(self.settings_path)
        self.source_factory = source_factory or default_source_factory(fake, camera_id)
        self._password = read_rear_password() if self.rear else ""
        self.rtsp_url = rtsp_url
        self.segment_s_override = segment_s_override
        self.time_sync = time_sync or TimeSync()
        self.free_fn = free_fn
        self.retry_s = retry_s
        # IP-камера по Wi-Fi подключается дольше (RTSP, до 10 с)
        self.frame_timeout_s = max(frame_timeout_s, 15.0) if self.rear else frame_timeout_s
        self._fails = 0  # подряд неудачных запусков: задняя камера недоступна — пауза растёт до 30 с
        self.housekeeping_s = housekeeping_s
        self.hub = MjpegHub()
        # живое видео (/live.ts): второй энкодер на lores, только пока есть зрители
        self.live = LiveHub(self.set_live_demand, grace_s=live_grace_s)
        self._live_wanted = False
        self._live_src: Source | None = None  # источник, на котором сейчас работает live-энкодер
        self._live_mux = LiveMux(self.cam.fps)
        self._live_bitrate = self.cam.live_bitrate_kbps

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
        self.protected_bytes: int | None = None  # защищённые записи там, где чистит этот экземпляр
        self.loop_minutes: int | None = None  # сколько минут записи держит петля (оценка по битрейту)
        self.moving: bool | None = None  # сообщает ядро (POST /motion); от этого — предел выдержки ночью
        # lores-кадры для fnav-watch (камера на охране): копируются, только пока он просит
        self.ring = RingWriter(ring_path or ring_default_path(camera_id))
        self._last_video_mono = 0.0
        self._last_segment: tuple[Path, str, float] | None = None  # закрытый при остановке конвейера: (путь, rel, monotonic)
        self._rate_log: deque[tuple[float, int]] = deque(maxlen=512)
        self._ring_only = False  # кадр нужен только кольцу (JPEG — нет)
        self._ring_want = False
        # микрофон задней камеры: громкие звуки, пока стоит охрана (ядро включает: POST /sound) — клипы WAV;
        # AAC — в записи (rear_camera.record_audio) и в живое видео, пока его смотрят
        self.sound = SoundMonitor(sound_dir or paths.data_dir() / "sound_clips") if self.rear else None
        self._aac_on = False

    @property
    def cam(self) -> CameraSettings:
        """Параметры конвейера этой камеры."""
        return rear_camera_settings(self.settings) if self.rear else self.settings.camera

    @property
    def enabled(self) -> bool:
        return not self.rear or self.settings.rear_camera.enabled

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
        self.ring.close()

    def restart(self) -> None:
        log.info("pipeline restart requested")
        self._restart.set()
        self._wake.set()

    def reload(self) -> bool:
        """Перечитать настройки; True — изменились параметры камеры, конвейер перезапускается.

        Битрейт живого видео запись не трогает: перезапускается только live-энкодер (если он работает).
        Пределы выдержки применяются на ходу."""
        new = load_settings(self.settings_path, fallback=self.settings)
        old_cam = self.cam
        old_rear = self.settings.rear_camera
        self.settings = new
        new_cam = self.cam
        changed = _rec_params(new_cam) != _rec_params(old_cam)
        if self.rear:
            pw = read_rear_password()
            # зеркало — только экран в машине; остальное (адрес, логин, пароль, запись…) — перезапуск
            changed = changed or pw != self._password or (
                old_rear.model_copy(update={"mirror": False}) != new.rear_camera.model_copy(update={"mirror": False}))
            self._password = pw
        self._hk_wake.set()
        if changed:
            log.info("camera settings changed -> restart")
            self.restart()
            return True
        with self._lock:
            if self._p is not None and max_frame_us(new_cam, self.moving) != max_frame_us(old_cam, self.moving):
                self._apply_exposure_limit(self._p)
            if new_cam.live_bitrate_kbps != old_cam.live_bitrate_kbps and self._live_src is not None:
                log.info("live bitrate changed -> restart live encoder")
                self._live_stop()
                self._sync_live()
        return False

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
            cam = self.cam
            # со звуком: у камеры есть микрофон (задняя)
            audio = bool(getattr(src, "audio_info", None)) and hasattr(src, "start_aac")
            self._live_mux.new_session(cam.fps, audio=audio)
            try:
                start(cam.live_bitrate_kbps, self._on_live_video)
            except Exception as e:
                log.exception("live encoder start failed")
                return f"Live stream: {e}"
            self._live_src = src
            self._live_bitrate = cam.live_bitrate_kbps
            self._sync_aac()
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
        self._sync_aac()
        log.info("live encoder stopped")

    def _on_live_video(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        """Поток энкодера: никаких ожиданий — только мультиплексирование и передача в asyncio."""
        try:
            r = self._live_mux.push(au, keyframe, ts_us)
            if r is not None:
                self.live.publish(*r)
        except Exception:
            log.exception("live mux failed")

    def _record_audio(self) -> bool:
        rc = self.settings.rear_camera
        return self.rear and rc.audio and rc.record_audio

    def _sync_aac(self) -> None:
        """AAC нужен записи (rear_camera.record_audio) или живому видео со звуком — включить / выключить у источника."""
        p = self._p
        src = p.source if p else None
        if src is None or not hasattr(src, "start_aac"):
            return
        want = self._live_src is src or (p.recorder is not None and self._record_audio())  # type: ignore[union-attr]
        if want and not self._aac_on:
            src.start_aac(self._on_aac)
            self._aac_on = True
        elif not want and self._aac_on:
            src.stop_aac()
            self._aac_on = False

    def _on_aac(self, adts: bytes, ts_us: int, silent: bool) -> None:
        """Поток помощника звука: кадр AAC в запись и в живое видео (silent — кадр тишины для пропусков живого)."""
        try:
            if silent:
                self._live_mux.set_silence(adts, AAC_FRAME_PTS)
                return
            p = self._p
            if p is not None and p.recorder is not None and self._record_audio():
                p.recorder.push_audio(adts, ts_us)
            if self._live_src is not None:
                r = self._live_mux.push_audio(adts, ts_us, AAC_FRAME_PTS)
                if r is not None:
                    self.live.publish(r, False)
        except Exception:
            log.exception("audio mux failed")

    # ───────────── звук (задняя камера) ─────────────
    def on_audio(self, pcm: bytes, ts_us: int) -> None:
        if self.sound is not None:
            self.sound.feed(pcm, ts_us)

    def set_sound_armed(self, armed: bool) -> None:
        """Ядро: охрана стоит — громкие звуки пишутся клипами; снята — нет (разговоры в фургоне не пишутся)."""
        if self.sound is not None and armed != self.sound.armed:
            self.sound.armed = armed
            log.info("loud sounds: %s", "watching" if armed else "off")

    # ───────────── едем / стоим ─────────────
    def set_moving(self, moving: bool | None) -> None:
        if moving == self.moving:
            return
        self.moving = moving
        log.info("vehicle %s: max exposure %.0f ms", {True: "moving", False: "parked", None: "unknown"}[moving],
                 max_frame_us(self.cam, moving) / 1000)
        with self._lock:
            p = self._p
            if p is not None:
                self._apply_exposure_limit(p)

    def _apply_exposure_limit(self, p: _Pipeline) -> None:
        cam = self.cam
        if self.rear:  # экспозицией IP-камера управляет сама
            return
        us = max_frame_us(cam, self.moving)
        if p.recorder is not None:  # длинные ночные кадры — не «потерянные»
            p.recorder.fps = float(cam.fps) if us <= round(1e6 / cam.fps) else 1e6 / us
        fn = getattr(p.source, "set_max_exposure", None)
        if fn is not None:
            try:
                fn(us)
            except Exception:
                log.exception("set max exposure failed")

    def protect_current(self, recent: bool = False) -> str | None:
        """Защитить текущий сегмент (у границы — и соседей, см. Recorder.protect_current). recent (охрана: камера
        пропала) — его нет, потому что конвейер перезапущен без кадров: последний закрытый, если он моложе
        LAST_SEGMENT_S (в нём то, что было перед пропажей). Закрытый при остановке конвейера меньше PROTECT_EDGE_S
        назад защищается и вместе с текущим: регистратор после перезапуска о нём не знает."""
        p = self._p
        rel = p.recorder.protect_current() if p is not None and p.recorder is not None else None
        last = self._last_segment
        if last is not None and last[0].is_file():
            age = time.monotonic() - last[2]
            if rel is None and recent and age <= LAST_SEGMENT_S:
                lock_segment(last[0])
                rel = last[1]
            elif rel is not None and age < PROTECT_EDGE_S and lock_segment(last[0]):
                log.info("protected the previous segment too: %s", self.rel_prefix + last[1])
        if rel:
            rel = self.rel_prefix + rel
            log.info("protected: %s", rel)
        return rel

    def status(self) -> CameraStatus:
        p = self._p
        rec = p.recorder if p else None
        src = p.source if p else None
        seg = rec.segment_rel if rec else None
        if seg:
            seg = self.rel_prefix + seg
        picture = self._picture_mono(p)
        live = p is not None and time.monotonic() - picture < 3.0
        fps = kbps = None
        if rec is not None and time.monotonic() - rec.last_frame_mono < 3.0:
            fps, kbps = rec.rates()
        elif rec is None and live:
            fps, kbps = self._live_rates()
        exp = dict(getattr(src, "exposure", None) or {})
        main_error = getattr(src, "main_error", None)
        error = self._error or (rec.write_error if rec else None) or (
            f"Rear camera: no main stream ({main_error}): not recording, the picture comes from the second stream"
            if main_error else None)
        if self._no_space and not (self.rear and not self.settings.rear_camera.record):  # без записи место не важно
            error = NO_SPACE
        if not self.enabled:
            state, error = "disabled", None
        elif error:
            state = "error"
        elif p is None:
            state = "starting" if self._state_starting else "stopped"
        elif seg:
            state = "recording"
        elif live and rec is None:
            state = "live"  # задняя без записи (rear_camera.record = false)
        else:
            state = "starting"
        return CameraStatus(
            state=state,
            camera=self.camera_id,
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
            protected_gb=round(self.protected_bytes / GB, 2) if self.protected_bytes is not None else None,
            loop_minutes=self.loop_minutes,
            over_limit=self._over_limit,
            time_synced=rec.segment_time_synced if seg and rec else self.time_sync.value,
            moving=self.moving,
            max_exposure_ms=None if self.rear else max_frame_us(self.cam, self.moving) / 1000,
            exposure_ms=round(exp["ExposureTime"] / 1000, 1) if "ExposureTime" in exp else None,
            gain=round(exp["AnalogueGain"] * exp.get("DigitalGain", 1.0), 2) if "AnalogueGain" in exp else None,
            lux=round(exp["Lux"], 1) if "Lux" in exp else None,
            codec=getattr(src, "codec", None),
            stream_fps=getattr(src, "stream_fps", None),
            password_set=bool(self._password) if self.rear else None,
            video_age_s=round(time.monotonic() - picture, 1) if picture else None,
            **self._sound_status(src),
        )

    def _sound_status(self, src) -> dict:
        a = getattr(src, "audio_info", None)
        snd = self.sound
        if not a or snd is None:
            return {}
        return {"audio": f"{a.get('codec')} {a.get('rate')} Hz", "sound_armed": snd.armed,
                "sound_level_db": snd.level_db, "sound_seq": snd.seq}

    # ───────────── FrameSink (вызывается из потока источника) ─────────────
    def on_video(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        p = self._p
        if p is None:
            return
        self._last_video_mono = now = time.monotonic()
        if p.recorder is not None:
            p.recorder.push(au, keyframe, ts_us)
        else:  # без записи частоту и битрейт считаем здесь
            self._rate_log.append((now, len(au)))
        if p.rtsp is not None:
            p.rtsp.push(au, keyframe, ts_us)

    def on_lores(self, frame: LoresFrame) -> None:
        p = self._p
        if p is None:
            return
        if p.jpeg is not None and not self._ring_only:
            p.jpeg.submit(frame)
        if self._ring_want:
            self._ring_want = False
            self.ring.write(frame.data, frame.kind, frame.w, frame.h, getattr(p.source, "exposure", None))

    def wants_lores(self, ts_us: int | None = None) -> bool:
        p = self._p
        jpeg = bool(p and p.jpeg and p.jpeg.wants_frame(ts_us))
        ring = self._ring_want = p is not None and self.ring.wants_frame()
        self._ring_only = ring and not jpeg
        return jpeg or ring

    def on_error(self, msg: str) -> None:
        self._source_error = msg
        self._wake.set()

    def on_main_lost(self, msg: str) -> None:
        """Задняя: основной поток отвалился, второй даёт картинку (экран, снимки, fnav-watch) — конвейер не
        перезапускаем, источник сам переподключает основной. Сегмент закрыть (не «идёт запись» без кадров); что было
        перед пропажей — в последнем закрытом (защита охраны recent)."""
        p = self._p
        rec = p.recorder if p else None
        if rec is None:
            return
        path, rel = rec.current_path, rec.segment_rel
        if path is not None and rel:
            self._last_segment = (path, rel, time.monotonic())
        rec.close_segment()

    def _picture_mono(self, p: _Pipeline | None) -> float:
        """Когда пришёл последний кадр с камеры — с любого потока (у задней экран живёт на втором); 0 — не было."""
        src_t = getattr(p.source, "picture_mono", 0.0) if p is not None and p.source is not None else 0.0
        return max(self._last_video_mono, src_t or 0.0)

    def _live_rates(self) -> tuple[float | None, float | None]:
        """(к/с, кбит/с) по последним ~3 с входящего видео (когда регистратора нет)."""
        log_ = list(self._rate_log)
        now = time.monotonic()
        r = [(t, n) for t, n in log_ if now - t <= 3.0]
        if len(r) < 2 or r[-1][0] - r[0][0] <= 0:
            return None, None
        dt = r[-1][0] - r[0][0]
        return round((len(r) - 1) / dt, 2), round(sum(n for _, n in r[1:]) * 8 / dt / 1000, 1)

    def lores_demand_fps(self) -> float:
        """Сколько малых кадров/с нужно сейчас (источник RTSP декодирует столько): зрители MJPEG / недавний снимок —
        lores.fps (или наибольшая из запрошенных ?fps), fnav-watch — его частота, иначе 1 (только ключевые кадры:
        свежий снимок к тревоге)."""
        cam = self.cam
        fps = 1.0
        if self.hub.demand:
            rate = self.hub.max_rate()
            fps = float(min(cam.lores.fps, rate) if rate else cam.lores.fps)
        return max(fps, self.ring.demand_fps())

    # ───────────── конвейер ─────────────
    def _segment_s(self) -> float:
        if self.segment_s_override:
            return self.segment_s_override
        return self.cam.segment_minutes * 60.0

    def _start_pipeline(self) -> None:
        s = self.settings
        cam = self.cam
        p = _Pipeline()
        if not self.rear or s.rear_camera.record:
            p.recorder = Recorder(
                self.root, segment_s=self._segment_s(), fsync_interval_s=cam.fsync_interval_s, gop=cam.gop,
                fps=cam.fps, time_sync=self.time_sync, request_keyframe=self._request_keyframe,
                on_segment_start=lambda _path: self._hk_wake.set(),
            )
            p.recorder.paused = self._no_space
            if self.rear:  # частоту задаёт камера (бывает переменной): пропуски по интервалам не считаем
                p.recorder.fps = 0.0
        p.jpeg = JpegWorker(self.hub, cam.lores.fps, cam.lores.jpeg_quality)
        if self.rtsp_url:
            p.rtsp = RtspPublisher(self.rtsp_url, fps=cam.fps, width=cam.out_w, height=cam.out_h)
        if p.recorder is not None:
            p.recorder.start()
        p.jpeg.start()
        if p.rtsp:
            p.rtsp.start()
        self._source_error = None
        self._last_video_mono = 0.0
        self._p = p  # sink начинает принимать кадры
        p.source = self.source_factory(s, self)
        self._apply_exposure_limit(p)  # до start(): предел попадёт в начальные controls
        self._aac_on = False  # новый источник: AAC у него ещё не просили
        p.source.start()
        p.started_mono = time.monotonic()
        with self._lock:
            self._sync_aac()
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
            rec = p.recorder
            if rec is not None and rec.current_path is not None and rec.segment_rel:
                self._last_segment = (rec.current_path, rec.segment_rel, time.monotonic())
            if p.rtsp is not None:  # не задерживаем перезапуск записи ожиданием ffmpeg
                threading.Thread(target=p.rtsp.stop, name="rtsp-stop", daemon=True).start()
            for part in (p.recorder, p.jpeg):
                if part is None:
                    continue
                try:
                    part.stop()
                except Exception:
                    log.exception("stop %s failed", type(part).__name__)

    def _retry_delay(self) -> float:
        if not self.rear:
            return self.retry_s
        return min(30.0, self.retry_s * 2 ** max(0, min(self._fails - 1, 4)))

    def _supervise(self) -> None:
        label = "Rear camera" if self.rear else "Camera"
        if not self._repaired:
            self._repaired = True
            try:
                repair_latest(self.root, exclude=() if self.rear else (REAR_DIR,))
            except Exception:
                log.exception("repair failed")
        while not self._stop.is_set():
            if self._restart.is_set():
                self._restart.clear()
                self._stop_pipeline()
                self._fails = 0
            if not self.enabled:  # задняя выключена в настройках: ничего не держим, ждём /reload
                if self._p is not None:
                    self._stop_pipeline()
                self._error, self._fails = None, 0
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if self._p is None:
                self._state_starting = True
                try:
                    self._start_pipeline()
                    log.info("pipeline started")
                except Exception as e:
                    self._fails += 1
                    if self._fails <= 3 or not self.rear:
                        log.exception("pipeline start failed")
                    self._error = f"{label}: {e}"
                    self._stop_pipeline()
                    self._state_starting = False
                    self._wake.wait(self._retry_delay())
                    self._wake.clear()
                    continue
                finally:
                    self._state_starting = False
            p = self._p
            assert p is not None
            rec = p.recorder
            problem = self._source_error
            now = time.monotonic()
            picture = self._picture_mono(p)
            last = max(rec.last_frame_mono if rec else 0.0, picture)
            if problem is None and now - max(last, p.started_mono) > self.frame_timeout_s:
                main_error = getattr(p.source, "main_error", None)
                problem = f"{label}: {main_error}" if main_error else \
                    f"{label}: no video for {self.frame_timeout_s:.0f} s"
            if problem:
                self._fails += 1
                delay = self._retry_delay()
                if self._fails <= 3 or not self.rear:
                    log.error("%s — restarting in %.0f s", problem, delay)
                self._error = problem
                self._stop_pipeline()
                if self._stop.wait(delay):
                    break
                continue
            if picture:
                self._fails = 0
            if self._error and ((rec and rec.segment_rel) or now - picture < 3.0):
                self._error = None  # снова пишем или показываем (у задней — хотя бы со второго потока)
            self._wake.wait(0.5)
            self._wake.clear()
        self._stop_pipeline()

    # ───────────── обслуживание хранилища ─────────────
    def housekeeping_once(self) -> None:
        s = self.settings
        cam = self.cam
        reserve = segment_reserve_bytes(cam.bitrate_kbps, cam.segment_minutes)
        min_free = int(s.storage.min_free_gb * GB)
        p = self._p
        rec = p.recorder if p else None
        # лимит объёма — с местом под следующий сегмент: после очистки записи + сегмент ≤ лимита
        max_total = max(0, int(s.storage.max_recordings_gb * GB) - reserve)
        # петля ради лимита — не меньше часа записи (но не больше половины лимита): защищённые сверх лимита иначе
        # оставили бы от неё последние минуты; глубже чистит только нехватка свободного места. Байт/с — по битрейту
        # из настроек: у передней — с задней, если та пишет (передняя чистит и её записи)
        kbps = cam.bitrate_kbps
        if not self.rear and s.rear_camera.enabled and s.rear_camera.record:
            kbps += s.rear_camera.bitrate_kbps
        loop_bps = kbps * 1000 / 8
        min_loop = loop_minimum(int(loop_bps * LOOP_MIN_S), max_total)
        # передняя чистит все записи (и задней: самые старые — первыми), кроме чужого текущего сегмента;
        # задняя — только свои (если передняя не работает)
        res = cleanup(self.root, min_free + reserve, current=rec.current_path if rec else None,
                      free_fn=self.free_fn, max_total_bytes=max_total, skip_recent_s=0.0 if self.rear else BUSY_S,
                      min_loop_bytes=min_loop)
        self.free_gb = res.free_gb
        self.protected_bytes = res.protected_bytes
        self.loop_minutes = round(res.loop_bytes / loop_bps / 60) if loop_bps > 0 else None
        over = res.protected_bytes + min_loop > max_total  # защищённые не оставляют петле её минимума
        if over and not self._over_limit:
            log.warning("protected recordings take %.2f GB of the %.1f GB limit: the loop keeps only ~%d min",
                        res.protected_bytes / GB, s.storage.max_recordings_gb, round(min_loop / loop_bps / 60))
        elif self._over_limit and not over:
            log.info("protected recordings fit the limit again (%.2f GB)", res.protected_bytes / GB)
        self._over_limit = over
        if res.free_bytes < min_free:
            if not self._no_space:
                log.error("%s (free %.2f GB)", NO_SPACE, res.free_gb)
            self._no_space = True
        elif self._no_space and res.ok:
            log.info("free space restored (%.2f GB), recording resumes", res.free_gb)
            self._no_space = False
        if rec is not None:
            rec.paused = self._no_space
        self.recordings_count, self.recordings_bytes = stats(self.root, () if self.rear else (REAR_DIR,))

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


LIVE_PARAMS = ("live_bitrate_kbps", "max_exposure_ms_driving", "max_exposure_ms_parked")


def _rec_params(cam) -> object:
    """Параметры камеры, после смены которых конвейер перезапускается (всё, что не меняется на ходу)."""
    return cam.model_copy(update=dict.fromkeys(LIVE_PARAMS, 0))
