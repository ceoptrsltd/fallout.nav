"""Сервис fnav-watch: поток обработки кадров из кольца fnav-camera, события, статус (docs/contracts/vision.md).

Не смотрим — поток спит на событии: кадры не читаются, нейросети не загружены (cv2 не импортирован), CPU ≈ 0.
Смотрим — ~4 кадра/с из общей памяти; при перегреве CPU (≥ 75 °C) частоты ниже. Настройки — settings.json
(fnav.paths.settings_file()): при /arm, /reload и сами по mtime файла, пока смотрим.
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
from fnav.camera.shmring import RingReader
from fnav.models import Settings, VisionSettings

from .frame import Frame
from .pipeline import Event, Watcher

log = logging.getLogger("fnav.watch")

FPS = 4.0
NO_FRAMES_S = 5.0
NO_FRAMES = "No frames from the camera (is fnav-camera running and up to date?)"
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
THERMAL_CHECK_S = 10.0
SETTINGS_CHECK_S = 3.0
HOT_C = ((80.0, 3.0), (75.0, 2.0))  # температура → во сколько раз реже
COOL_HYST_C = 3.0
MAX_EVENTS = 20

DetectorFactory = Callable[[], tuple[object, object]]


def load_vision(path: Path, fallback: VisionSettings | None = None) -> VisionSettings:
    try:
        return Settings.model_validate_json(path.read_text(encoding="utf-8")).alarm.vision
    except FileNotFoundError:
        return VisionSettings()
    except Exception:
        log.exception("cannot read %s, using %s", path, "previous settings" if fallback else "defaults")
        return fallback or VisionSettings()


def default_detectors(models_dir: Path | None = None) -> DetectorFactory:
    def make():
        from .detect import load_detectors

        return load_detectors(models_dir)

    return make


class WatchService:
    def __init__(self, *, settings_path: Path | None = None, ring: RingReader | None = None,
                 detectors: DetectorFactory | None = None, thermal_path: Path | None = THERMAL,
                 fps: float = FPS, clock: Callable[[], float] = time.time) -> None:
        self.settings_path = Path(settings_path) if settings_path else paths.settings_file()
        self.ring = ring or RingReader()
        self.detectors = detectors or default_detectors()
        self.thermal_path = thermal_path
        self.base_fps = fps
        self.clock = clock
        self.settings = load_vision(self.settings_path)
        self._settings_mtime = self._mtime()
        self.watching = False
        self.error: str | None = None
        self.seq = 0
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.watcher: Watcher | None = None
        self.temp_c: float | None = None
        self.slow = 1.0
        self.frames_read = 0
        self._frame_times: deque[float] = deque(maxlen=40)
        self._ring_seq = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._models_error_until = 0.0

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
        try:
            self.ring.stop()
            self.ring.close()
        except Exception:
            log.exception("ring close")

    def set_watching(self, watching: bool) -> None:
        if watching == self.watching:
            return
        if watching:
            self.reload()
        self.watching = watching
        log.info("%s", "watching" if watching else "stopped watching")
        self._wake.set()

    def reload(self) -> VisionSettings:
        self._settings_mtime = self._mtime()
        self.settings = load_vision(self.settings_path, fallback=self.settings)
        w = self.watcher
        if w is not None:
            w.apply(self.settings)
        return self.settings

    def _mtime(self) -> float | None:
        try:
            return self.settings_path.stat().st_mtime
        except OSError:
            return None

    # ── статус ──
    @property
    def fps(self) -> float | None:
        ft = self._frame_times
        if not self.watching or len(ft) < 2 or time.monotonic() - ft[-1] > 3.0:
            return None
        return round((len(ft) - 1) / max(1e-3, ft[-1] - ft[0]), 1)

    def status(self) -> dict:
        w = self.watcher
        with self._lock:  # события дописывает поток обработки
            seq, events = self.seq, list(self.events)
        return {
            "watching": self.watching,
            "people": w.people if (self.watching and w is not None) else 0,
            "seq": seq,
            "events": events,
            "error": self.error,
            "fps": self.fps,
        }

    def debug_info(self) -> dict:
        w = self.watcher
        return {
            "watching": self.watching,
            "settings": self.settings.model_dump(),
            "temp_c": self.temp_c,
            "slow": self.slow,
            "fps": self.fps,
            "frames_read": self.frames_read,
            "ring": str(self.ring.path),
            "stats": asdict(w.stats) if w else None,
            "tracks": w.tracks_info() if w else [],
            "loiter": {"present_s": round(w.snap.frame.t - w.loiter.since, 1)
                       if w.loiter.since is not None and w.snap.frame is not None else None,
                       "armed": w.loiter.armed, "source": w.loiter.source or None} if w else None,
            "motion": asdict(w.snap.motion) if w else None,
            "error": self.error,
        }

    def debug_jpeg(self) -> bytes | None:
        w = self.watcher
        return w.render() if w is not None else None

    # ── поток обработки ──
    def _publish(self, ev: Event) -> None:
        with self._lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": ev.kind, "ts": round(ev.ts, 3), "data": ev.data})
        log.warning("event %s #%d %s", ev.kind, self.seq, ev.data)

    def _ensure_watcher(self) -> bool:
        if self.watcher is not None:
            return True
        if time.monotonic() < self._models_error_until:
            return False
        try:
            person, face = self.detectors()
        except Exception as e:
            log.exception("cannot load detectors")
            self.error = f"Cannot load detectors: {e}"
            self._models_error_until = time.monotonic() + 30.0
            return False
        self.watcher = Watcher(person, face, self.settings, clock=self.clock)  # type: ignore[arg-type]
        self.error = None
        log.info("detectors loaded")
        return True

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
            if self.watcher is not None:
                self.watcher.slow = slow

    def _idle(self) -> None:
        """Не смотрим: отпустить кольцо, забыть фон и треки, спать до /arm."""
        with contextlib.suppress(Exception):
            self.ring.stop()
        if self.watcher is not None:
            self.watcher.reset()
        self._frame_times.clear()
        if self.error == NO_FRAMES:
            self.error = None
        self._wake.wait()
        self._wake.clear()

    def _wait(self, s: float) -> None:
        """Пауза, которую прерывают /arm и остановка (состояние перечитывается в начале цикла)."""
        if self._wake.wait(s):
            self._wake.clear()

    def _run(self) -> None:
        next_thermal = next_settings = 0.0
        last_frame = 0.0
        while not self._stop.is_set():
            if not self.watching:
                self._idle()
                last_frame = time.monotonic()
                continue
            if not self._ensure_watcher():
                self._wait(5.0)
                continue
            w = self.watcher
            assert w is not None
            now = time.monotonic()
            if now >= next_thermal:
                next_thermal = now + THERMAL_CHECK_S
                self._thermal()
                w.slow = self.slow
            if now >= next_settings:
                next_settings = now + SETTINGS_CHECK_S
                if self._mtime() != self._settings_mtime:
                    log.info("settings changed, reloading")
                    self.reload()
            fps = self.base_fps / self.slow
            try:
                self.ring.want(fps)
                f = self.ring.read(self._ring_seq)
            except Exception as e:
                log.exception("frame ring")
                self.error = f"Frame ring: {e}"
                self._wait(5.0)
                continue
            if f is None:
                if now - last_frame > NO_FRAMES_S and self.error is None:
                    log.warning("%s", NO_FRAMES)
                    self.error = NO_FRAMES
                self._wait(0.05)
                continue
            self._ring_seq = f.seq
            last_frame = now
            self.frames_read += 1
            if self.error == NO_FRAMES:
                self.error = None
            frame = Frame(f.ts, f.kind, f.data, f.w, f.h, f.exposure, wall=self.clock())
            try:
                events = w.process(frame)
            except Exception as e:
                log.exception("frame processing failed")
                self.error = f"Processing failed: {e}"
                self._wait(1.0)
                continue
            if self.error and self.error.startswith("Processing failed"):
                self.error = None
            self._frame_times.append(time.monotonic())
            for ev in events:
                self._publish(ev)
            # следующий кадр не раньше чем через ~1/fps (писатель кладёт не чаще)
            rest = 0.8 / fps - (time.monotonic() - now)
            if rest > 0:
                self._wait(rest)
        log.info("watch thread stopped")
