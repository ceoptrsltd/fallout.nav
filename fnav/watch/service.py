"""Сервис fnav-watch: поток обработки кадров из колец fnav-camera, события, статус (docs/contracts/vision.md).

Не смотрим — поток спит на событии: кадры не читаются, нейросети не загружены (cv2 не импортирован), CPU ≈ 0.
Смотрим — передняя камера ~4 кадра/с из общей памяти; задняя (rear_camera.enabled и alarm.vision.rear) — своим
кольцом: 1 кадр/с, пока в её кадре тихо (камера и так декодирует только ключевые кадры — для снимка к тревоге),
4 кадра/с, пока там движение, трек или кто-то стоит (и BUSY_S после). Детекторы — одни на обе камеры, в этом же
потоке: за проход не больше одной нейросети, её получает камера, у которой она сильнее просрочена (просроченные
вдвое — равны, тогда та, что дольше ждала). Нет кадров задней — передняя работает как прежде. При перегреве CPU
(≥ 75 °C) частоты ниже. Настройки — settings.json (fnav.paths.settings_file()): при /arm, /reload и сами по mtime
файла, пока смотрим.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from fnav import paths
from fnav.camera import shmring
from fnav.camera.shmring import RingReader
from fnav.models import Settings, VisionSettings

from .frame import Frame
from .pipeline import Event, Plan, Watcher

log = logging.getLogger("fnav.watch")

FPS = 4.0
REAR_IDLE_FPS = 1.0  # задняя, пока в кадре тихо: камера не декодирует сверх ключевых кадров
NO_FRAMES_S = 5.0
REAR_NO_FRAMES_S = 15.0  # тихая задняя — ключевые кадры (GOP камеры до ~4 с) и пауза декодера при смене частоты
NO_FRAMES = "No frames from the camera (is fnav-camera running and up to date?)"
NO_FRAMES_REAR = "No frames from the rear camera (is it on and is fnav-camera-rear running?)"
PROCESSING = "Processing failed"
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
THERMAL_CHECK_S = 10.0
SETTINGS_CHECK_S = 3.0
HOT_C = ((80.0, 3.0), (75.0, 2.0))  # температура → во сколько раз реже
COOL_HYST_C = 3.0
MAX_EVENTS = 20
DUE_CAP = 2.0  # нейросеть просрочена вдвое и больше — камеры равны, тогда по очереди (кто дольше ждал)

DetectorFactory = Callable[[], tuple[object, object]]


def load_settings(path: Path, fallback: Settings | None = None) -> Settings:
    try:
        return Settings.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except Exception:
        log.exception("cannot read %s, using %s", path, "previous settings" if fallback else "defaults")
        return fallback or Settings()


def default_detectors(models_dir: Path | None = None) -> DetectorFactory:
    def make():
        from .detect import load_detectors

        return load_detectors(models_dir)

    return make


class Feed:
    """Одна камера: кольцо кадров, свой конвейер (фон, треки, правила), частота кадров, ошибка."""

    def __init__(self, name: str, ring: RingReader, *, idle_fps: float | None = None,
                 no_frames: str = NO_FRAMES, stale_s: float = 3.0) -> None:
        self.name = name  # "front" | "rear" — data.camera событий
        self.ring = ring
        self.idle_fps = idle_fps  # пока в кадре тихо — столько кадров/с (None — всегда полная частота)
        self.no_frames = no_frames
        self.stale_s = stale_s  # кадров нет дольше — частота в статусе неизвестна
        self.watcher: Watcher | None = None
        self.on = False  # смотрим эту камеру сейчас (поток обработки)
        self.error: str | None = None
        self.seq = 0  # номер последнего прочитанного кадра кольца
        self.last_frame = 0.0  # monotonic: последний кадр (или начало ожидания)
        self.fps_req = 0.0  # сколько кадров/с просим у камеры сейчас
        self.frames_read = 0
        self.frame_times: deque[float] = deque(maxlen=40)
        self.last_net = -1e18  # monotonic: когда камера последний раз получала нейросеть
        self.retry_at = 0.0  # после ошибки кольца или обработки — не раньше

    def want_fps(self, base: float) -> float:
        w = self.watcher
        if self.idle_fps is None or (w is not None and w.busy):
            return base
        return min(base, self.idle_fps)

    def fps(self) -> float | None:
        ft = self.frame_times
        if len(ft) < 2 or time.monotonic() - ft[-1] > self.stale_s:
            return None
        return round((len(ft) - 1) / max(1e-3, ft[-1] - ft[0]), 1)

    def release(self) -> None:
        """Не смотрим эту камеру: отпустить кольцо (камера сразу перестаёт копировать), забыть фон и треки."""
        with contextlib.suppress(Exception):
            self.ring.stop()
        if self.watcher is not None:
            self.watcher.reset()
        self.frame_times.clear()
        self.on = False
        self.error = None
        self.retry_at = 0.0


class WatchService:
    def __init__(self, *, settings_path: Path | None = None, ring: RingReader | None = None,
                 ring_rear: RingReader | None = None, detectors: DetectorFactory | None = None,
                 thermal_path: Path | None = THERMAL, fps: float = FPS, clock: Callable[[], float] = time.time) -> None:
        self.settings_path = Path(settings_path) if settings_path else paths.settings_file()
        self.front = Feed("front", ring or RingReader())
        self.rear = Feed("rear", ring_rear or RingReader(shmring.default_path("rear")), idle_fps=REAR_IDLE_FPS,
                         no_frames=NO_FRAMES_REAR, stale_s=6.0)
        self.feeds = (self.front, self.rear)
        self.detectors = detectors or default_detectors()
        self.thermal_path = thermal_path
        self.base_fps = fps
        self.clock = clock
        self._all = load_settings(self.settings_path)
        self.settings: VisionSettings = self._all.alarm.vision
        self.rear_on = self._rear_wanted()
        self._settings_mtime = self._mtime()
        self.watching = False
        self._error: str | None = None  # общая ошибка (модели не загрузились); ошибки камер — у Feed
        self.seq = 0
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self._nets: tuple[object, object] | None = None  # (люди, лица) — одни на все камеры
        self.temp_c: float | None = None
        self.slow = 1.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._models_error_until = 0.0

    # ── передняя камера — поля прежнего однокамерного сервиса ──
    @property
    def ring(self) -> RingReader:
        return self.front.ring

    @property
    def watcher(self) -> Watcher | None:
        return self.front.watcher

    @property
    def frames_read(self) -> int:
        return self.front.frames_read

    @property
    def error(self) -> str | None:
        return self._error or self.front.error

    @property
    def fps(self) -> float | None:
        return self.front.fps() if self.watching else None

    # ── управление ──
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(10)
            self._thread = None
        for feed in self.feeds:
            try:
                feed.ring.stop()
                feed.ring.close()
            except Exception:
                log.exception("%s ring close", feed.name)

    def set_watching(self, watching: bool) -> None:
        if watching == self.watching:
            return
        if watching:
            self.reload()
        self.watching = watching
        log.info("%s%s", "watching" if watching else "stopped watching",
                 " (front and rear cameras)" if watching and self.rear_on else "")
        self._wake.set()

    def reload(self) -> VisionSettings:
        self._settings_mtime = self._mtime()
        self._all = load_settings(self.settings_path, fallback=self._all)
        self.settings = self._all.alarm.vision
        self.rear_on = self._rear_wanted()
        for feed in self.feeds:
            if feed.watcher is not None:
                feed.watcher.apply(self.settings)
        self._wake.set()  # задняя включилась / выключилась — поток подхватит сразу
        return self.settings

    def _rear_wanted(self) -> bool:
        return bool(self._all.rear_camera.enabled and self._all.alarm.vision.rear)

    def _mtime(self) -> float | None:
        try:
            return self.settings_path.stat().st_mtime
        except OSError:
            return None

    # ── статус ──
    def _camera_status(self, feed: Feed) -> dict:
        watched = self.watching and (feed is self.front or self.rear_on)
        w = feed.watcher
        return {"watching": watched, "people": w.people if (watched and w is not None) else 0,
                "fps": feed.fps() if watched else None, "error": feed.error}

    def status(self) -> dict:
        with self._lock:  # события дописывает поток обработки
            seq, events = self.seq, list(self.events)
        cams = {"front": self._camera_status(self.front)}
        if self.rear_on:
            cams["rear"] = self._camera_status(self.rear)
        return {
            "watching": self.watching,
            "people": sum(c["people"] for c in cams.values()),
            "seq": seq,
            "events": events,
            "error": self.error,
            "fps": cams["front"]["fps"],
            "cameras": cams,
        }

    def _feed_debug(self, feed: Feed) -> dict:
        w = feed.watcher
        return {
            "fps": feed.fps() if self.watching else None,
            "fps_wanted": feed.fps_req if feed.on else 0.0,
            "busy": w.busy if w else False,
            "frames_read": feed.frames_read,
            "ring": str(feed.ring.path),
            "near_line": w.near_line if w else None,
            "stats": asdict(w.stats) if w else None,
            "tracks": w.tracks_info() if w else [],
            "loiter": {"present_s": round(w.snap.frame.t - w.loiter.since, 1)
                       if w.loiter.since is not None and w.snap.frame is not None else None,
                       "armed": w.loiter.armed, "source": w.loiter.source or None} if w else None,
            "motion": asdict(w.snap.motion) if w else None,
            "error": feed.error,
        }

    def debug_info(self) -> dict:
        return {
            "watching": self.watching,
            "settings": self.settings.model_dump(),
            "temp_c": self.temp_c,
            "slow": self.slow,
            **self._feed_debug(self.front),
            "error": self.error,
            "rear": self._feed_debug(self.rear) if self.rear_on else None,
        }

    def debug_jpeg(self, camera: str = "front") -> bytes | None:
        w = (self.rear if camera == "rear" else self.front).watcher
        return w.render() if w is not None else None

    # ── поток обработки ──
    def _publish(self, ev: Event) -> None:
        with self._lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": ev.kind, "ts": round(ev.ts, 3), "data": ev.data})
        log.warning("event %s #%d %s", ev.kind, self.seq, ev.data)

    def _ensure_detectors(self) -> bool:
        if self._nets is not None:
            return True
        if time.monotonic() < self._models_error_until:
            return False
        try:
            self._nets = self.detectors()
        except Exception as e:
            log.exception("cannot load detectors")
            self._error = f"Cannot load detectors: {e}"
            self._models_error_until = time.monotonic() + 30.0
            return False
        self._error = None
        log.info("detectors loaded")
        return True

    def _sync_feeds(self, now: float) -> None:
        """Какие камеры смотрим: передняя — всегда, задняя — по настройкам (сменились — сразу)."""
        assert self._nets is not None
        for feed in self.feeds:
            want = feed is self.front or self.rear_on
            if want and not feed.on:
                if feed.watcher is None:
                    person, face = self._nets
                    feed.watcher = Watcher(person, face, self.settings, clock=self.clock,  # type: ignore[arg-type]
                                           camera=feed.name)
                feed.watcher.slow = self.slow
                feed.on, feed.error, feed.last_frame = True, None, now
                if feed is self.rear:
                    log.info("watching the rear camera too (%s)", feed.ring.path)
            elif feed.on and not want:
                feed.release()
                log.info("stopped watching the %s camera", feed.name)

    def _thermal(self) -> None:
        if self.thermal_path is None:
            return
        try:
            self.temp_c = int(self.thermal_path.read_text().strip()) / 1000
        except (OSError, ValueError):
            return
        slow = 1.0
        for limit, k in HOT_C:
            if self.temp_c >= limit or (self.slow >= k and self.temp_c >= limit - COOL_HYST_C):
                slow = k
                break
        if slow != self.slow:
            log.warning("CPU %.0f °C: frame rate %.1f/s", self.temp_c, self.base_fps / slow)
            self.slow = slow
        for feed in self.feeds:
            if feed.watcher is not None:
                feed.watcher.slow = self.slow

    def _idle(self) -> None:
        """Не смотрим: отпустить кольца, забыть фон и треки, спать до /arm."""
        for feed in self.feeds:
            feed.release()
        self._wake.wait()
        self._wake.clear()

    def _wait(self, s: float) -> None:
        """Пауза, которую прерывают /arm, /reload и остановка (состояние перечитывается в начале цикла)."""
        if self._wake.wait(s):
            self._wake.clear()

    def _read(self, feed: Feed, base_fps: float, now: float) -> Frame | None:
        """Попросить кадры с нужной частотой и взять новый, если есть."""
        if now < feed.retry_at:
            return None
        fps = feed.fps_req = feed.want_fps(base_fps)
        try:
            feed.ring.want(fps)
            f = feed.ring.read(feed.seq)
        except Exception as e:
            log.exception("%s frame ring", feed.name)
            feed.error = f"Frame ring{' (rear)' if feed is self.rear else ''}: {e}"
            feed.retry_at = now + 5.0
            return None
        if f is None:
            limit = REAR_NO_FRAMES_S if feed is self.rear else NO_FRAMES_S
            if now - feed.last_frame > limit and feed.error is None:
                log.warning("%s", feed.no_frames)
                feed.error = feed.no_frames
            return None
        feed.seq = f.seq
        feed.last_frame = now
        feed.frames_read += 1
        if feed.error is not None and not feed.error.startswith(PROCESSING):
            feed.error = None  # кадры пошли
        return Frame(f.ts, f.kind, f.data, f.w, f.h, f.exposure, wall=self.clock())

    @staticmethod
    def _failed(feed: Feed, e: Exception) -> None:
        feed.error = f"{PROCESSING}: {e}"
        feed.retry_at = time.monotonic() + 1.0

    @staticmethod
    def _pick(plans: list[tuple[Feed, Plan]]) -> Feed | None:
        """Кому нейросеть в этом проходе: где сильнее просрочена; просроченные вдвое — по очереди."""
        best: tuple[tuple[float, float], Feed] | None = None
        for feed, plan in plans:
            if not plan.net:
                continue
            key = (min(plan.due, DUE_CAP), -feed.last_net)
            if best is None or key > best[0]:
                best = (key, feed)
        return best[1] if best else None

    def _process(self, got: list[tuple[Feed, Frame]]) -> None:
        plans: list[tuple[Feed, Plan]] = []
        for feed, frame in got:
            assert feed.watcher is not None
            try:
                plans.append((feed, feed.watcher.prepare(frame)))
            except Exception as e:
                log.exception("%s camera: frame processing failed", feed.name)
                self._failed(feed, e)
        chosen = self._pick(plans)
        for feed, plan in plans:
            assert feed.watcher is not None
            try:
                events = feed.watcher.finish(plan, run=feed is chosen)
            except Exception as e:
                log.exception("%s camera: frame processing failed", feed.name)
                self._failed(feed, e)
                continue
            if feed is chosen:
                feed.last_net = time.monotonic()
            if feed.error and feed.error.startswith(PROCESSING):
                feed.error = None
            feed.frame_times.append(time.monotonic())
            for ev in events:
                self._publish(ev)

    def _rest(self) -> float | None:
        """Сколько ждать следующего кадра (писатель кладёт не чаще ~1/fps); None — ни одна камера не шлёт кадры."""
        now = time.monotonic()
        rest = None
        for feed in self.feeds:
            if not feed.on or feed.fps_req <= 0 or now - feed.last_frame > max(2.0, 2.0 / feed.fps_req):
                continue
            r = feed.last_frame + 0.8 / feed.fps_req - now
            rest = r if rest is None else min(rest, r)
        return rest

    def _run(self) -> None:
        next_thermal = next_settings = 0.0
        while not self._stop.is_set():
            if not self.watching:
                self._idle()
                continue
            if not self._ensure_detectors():
                self._wait(5.0)
                continue
            now = time.monotonic()
            if now >= next_thermal:
                next_thermal = now + THERMAL_CHECK_S
                self._thermal()
            if now >= next_settings:
                next_settings = now + SETTINGS_CHECK_S
                if self._mtime() != self._settings_mtime:
                    log.info("settings changed, reloading")
                    self.reload()
            self._sync_feeds(now)
            base = self.base_fps / self.slow
            got = []
            for feed in self.feeds:
                if feed.on:
                    frame = self._read(feed, base, now)
                    if frame is not None:
                        got.append((feed, frame))
            if got:
                self._process(got)
            rest = self._rest()
            if not got:
                self._wait(max(0.05, rest or 0.0))
            elif rest is not None and rest > 0:
                self._wait(rest)
        log.info("watch thread stopped")
