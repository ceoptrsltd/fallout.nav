"""Ядро процесса fnav-core: связывает настройки, датчики, камеру, тайлы, WS и фоновые задачи."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from fnav import paths
from fnav.core.auth import AuthManager
from fnav.core.camera import CAMERA_URL, WHEP_URL, CameraService, CameraUnavailable
from fnav.core.config import SettingsStore
from fnav.core.navlogic import NavLogic
from fnav.core.recordings import Recordings
from fnav.core.system import SystemService
from fnav.core.tiles import TileService
from fnav.core.util import cancel_tasks, spawn
from fnav.core.videoout import VideoOutput, VideoOutputError
from fnav.core.ws import WsManager, new_nmea_lines
from fnav.interfaces import ButtonEvent, LedMode, SensorHub, create_sensor_hub
from fnav.models import (
    CalibrationStatus,
    CameraStatus,
    CompassCalibration,
    SensorSnapshot,
    Settings,
    Telemetry,
)

log = logging.getLogger("fnav.core")

TELEMETRY_HZ = 10.0
DEV_PASSWORD = "fnav-dev"


@dataclass
class CoreOptions:
    mock: bool = False
    trust_localhost: bool = True
    background: bool = True  # фоновые циклы (телеметрия, опросы); в unit-тестах можно выключить
    camera_url: str = CAMERA_URL
    whep_url: str = WHEP_URL
    local_transport: httpx.AsyncBaseTransport | None = None  # для камеры/MediaMTX (тесты)
    tile_transport: httpx.AsyncBaseTransport | None = None  # для Esri (тесты)
    hub_factory: Callable[[Settings], SensorHub] | None = None
    secrets_path: Path | None = None


class NullSensorHub:
    """Заглушка, если настоящий хаб датчиков не загрузился: процесс продолжает работать."""

    def __init__(self, reason: str):
        self.reason = reason

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(errors=[f"Sensors unavailable: {self.reason}"])

    def apply_settings(self, settings: Settings) -> None: ...

    def nmea_tail(self, n: int = 50) -> list[str]:
        return []

    async def gps_configure(self) -> dict:
        return {"ok": False, "details": self.reason}

    def calibration_start(self) -> None: ...

    def calibration_status(self) -> CalibrationStatus:
        return CalibrationStatus(message=self.reason)

    def calibration_finish(self) -> CompassCalibration:
        raise RuntimeError(self.reason)

    def calibration_cancel(self) -> None: ...

    def on_calibration_update(self, callback) -> None: ...

    def auto_calibration_reset(self) -> None: ...

    def on_button(self, callback) -> None: ...

    def set_led(self, mode: LedMode) -> None: ...

    def beep(self, pattern: str = "short") -> None: ...


class Core:
    def __init__(self, opts: CoreOptions):
        self.opts = opts
        self.mock = opts.mock
        self.store = SettingsStore()
        self.auth = AuthManager(secrets_path=opts.secrets_path or paths.secrets_file(),
                                trust_localhost=opts.trust_localhost)
        self.ws = WsManager()
        s = self.store.get()
        self.nav = NavLogic(s.nav, s.display, start_time=time.monotonic())
        self.recordings = Recordings()
        self.tiles = TileService(s, transport=opts.tile_transport)
        self.camera = CameraService(mock=opts.mock, recordings_root=paths.recordings_dir(),
                                    base_url=opts.camera_url, whep_url=opts.whep_url,
                                    transport=opts.local_transport)
        self.system = SystemService(mock=opts.mock)
        self.video = VideoOutput(mock=opts.mock)
        self.hub: SensorHub = self._make_hub(s)
        self.display_state = self.nav.update(time.monotonic(), self.hub.snapshot().gps)
        self._tasks: list[asyncio.Task] = []
        self._led: LedMode | None = None
        self._led_override: LedMode | None = None
        self._led_timer: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._secrets_mtime: float | None = None
        self.started = False
        self.telemetry_sent = 0

        self.store.on_change(self._on_settings)
        self.camera.on_status(self._on_camera_status)

    def _make_hub(self, s: Settings) -> SensorHub:
        try:
            if self.opts.hub_factory:
                return self.opts.hub_factory(s)
            return create_sensor_hub(s, mock=self.mock)
        except Exception as e:  # noqa: BLE001 — хаб F может ещё не существовать
            log.error("cannot create sensor hub: %s", e)
            return NullSensorHub(f"{type(e).__name__}: {e}")

    # ── жизненный цикл ──
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.auth.load(create_default_password=DEV_PASSWORD if self.mock else None)
        self._secrets_mtime = self._stat_secrets()
        self.store.ensure_file()
        try:
            await self.hub.start()
        except Exception as e:
            log.exception("hub.start()")
            self.hub = NullSensorHub(str(e))
        try:
            self.hub.on_button(self._on_button)
        except Exception:
            log.exception("hub.on_button()")
        try:
            self.hub.on_calibration_update(self._on_calibration_update)
        except Exception:
            log.exception("hub.on_calibration_update()")
        bg = self.opts.background
        await self.tiles.start(background=bg)
        await self.camera.start(background=bg)
        self._on_camera_status(self.camera.status)
        await self._refresh_slow()
        if bg:
            self._tasks += [
                asyncio.create_task(self._telemetry_loop(), name="telemetry"),
                asyncio.create_task(self._slow_loop(), name="slow-status"),
            ]
        self.started = True
        log.info("fnav-core started (mock=%s)", self.mock)

    async def stop(self) -> None:
        await cancel_tasks(*self._tasks)
        self._tasks.clear()
        for closer in (self.hub.stop, self.camera.stop, self.tiles.stop):
            try:
                await closer()
            except Exception:
                log.exception("shutdown")
        self.started = False

    # ── настройки ──
    async def _on_settings(self, s: Settings) -> None:
        self.nav.apply_settings(s.nav, s.display)
        self.tiles.apply_settings(s)
        self.ws.broadcast({"type": "settings", "settings": s.model_dump(mode="json")})
        try:
            self.hub.apply_settings(s)
        except Exception:
            log.exception("hub.apply_settings()")
        spawn(self.camera.reload(), name="camera-reload")

    def settings_message(self) -> str:
        return json.dumps({"type": "settings", "settings": self.store.get().model_dump(mode="json")},
                          ensure_ascii=False)

    # ── телеметрия ──
    def build_telemetry(self, now_mono: float | None = None) -> Telemetry:
        snap = self.hub.snapshot()
        self.display_state = self.nav.update(now_mono if now_mono is not None else time.monotonic(), snap.gps)
        errors = list(snap.errors)
        cam = self.camera.status
        if cam.state == "error" and cam.error:
            errors.append(f"Camera: {cam.error}")
        return Telemetry(
            t=time.time(),
            gps=snap.gps,
            heading=snap.heading,
            compass=snap.compass,
            compass_cal=snap.compass_cal,
            display=self.display_state,
            camera=cam,
            disk=self.system.disk,
            system=self.system.status,
            errors=errors,
        )

    async def _telemetry_loop(self) -> None:
        period = 1.0 / TELEMETRY_HZ
        nxt = time.monotonic()
        tick = 0
        while True:
            try:
                tel = self.build_telemetry()
                if len(self.ws):
                    self.ws.broadcast_text(tel.model_dump_json())
                    self.telemetry_sent += 1
                if tick % 2 == 0:
                    self._push_nmea()
            except Exception:
                log.exception("telemetry")
            tick += 1
            nxt += period
            delay = nxt - time.monotonic()
            if delay < -1.0:  # сильно отстали (засыпание/нагрузка) — без «догоняющей» пачки
                nxt = time.monotonic()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    def _push_nmea(self) -> None:
        subs = self.ws.subscribers("nmea")
        if not subs:
            return
        tail = self.hub.nmea_tail(200)
        if not tail:
            return
        for c in subs:
            lines = new_nmea_lines(tail, c.nmea_last)
            if lines:
                c.nmea_last = lines[-1]
                c.send(json.dumps({"type": "nmea", "lines": lines}, ensure_ascii=False))

    async def _refresh_slow(self) -> None:
        await self.system.refresh(internet=self.tiles.internet)
        rec_bytes = await asyncio.to_thread(self.recordings.total_bytes)
        await self.system.refresh_disk(recordings_bytes=rec_bytes, tiles_mb=self.tiles.size_mb)

    async def _slow_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(5.0)
            n += 1
            try:
                await self.system.refresh(internet=self.tiles.internet)
                if n % 3 == 0:
                    rec = await asyncio.to_thread(self.recordings.total_bytes)
                    await self.system.refresh_disk(recordings_bytes=rec, tiles_mb=self.tiles.size_mb)
                else:
                    self.system.disk = self.system.disk.model_copy(update={"tiles_mb": self.tiles.size_mb})
                self._reload_secrets_if_changed()
            except Exception:
                log.exception("background status refresh")

    def _stat_secrets(self) -> float | None:
        try:
            return self.auth.secrets_path.stat().st_mtime
        except OSError:
            return None

    def note_secrets_written(self) -> None:
        self._secrets_mtime = self._stat_secrets()

    def _reload_secrets_if_changed(self) -> None:
        m = self._stat_secrets()
        if m != self._secrets_mtime:
            self._secrets_mtime = m
            log.info("secrets.env changed - reloading")
            self.auth.load()

    # ── камера / LED / кнопка ──
    def current_segment(self) -> str | None:
        st = self.camera.status
        return st.current_segment if st.recording or st.state == "recording" else None

    # LED на кнопке GPS-модуля: запись идёт — мигает, ошибка — часто мигает, нет записи — погашен.
    # После нажатия кнопки — короткое подтверждение (горит ровно / часто мигает при ошибке).
    def _status_led_mode(self) -> LedMode:
        st = self.camera.status
        return "blink_slow" if st.state == "recording" else ("blink_fast" if st.state == "error" else "off")

    def _apply_led(self) -> None:
        mode = self._led_override or self._status_led_mode()
        if mode != self._led:
            self._led = mode
            try:
                self.hub.set_led(mode)
            except Exception:
                log.exception("hub.set_led()")

    def _on_camera_status(self, st: CameraStatus) -> None:
        self._apply_led()

    def _flash_led(self, mode: LedMode, seconds: float = 3.0) -> None:
        """Временно показать подтверждение на LED, затем вернуть индикацию записи."""
        self._led_override = mode
        self._apply_led()
        if self._led_timer is not None:
            self._led_timer.cancel()
        loop = self._loop
        if loop is None:
            return

        def restore() -> None:
            self._led_override = None
            self._led_timer = None
            self._apply_led()

        self._led_timer = loop.call_later(seconds, restore)

    def _on_calibration_update(self, cal: CompassCalibration) -> None:
        """Автокалибровка компаса улучшилась — сохранить в settings.compass.calibration."""
        loop = self._loop
        if loop is None:
            return

        async def save() -> None:
            await self.store.patch({"compass": {"calibration": cal.model_dump(mode="json")}})
            log.info("compass auto-calibration saved (samples=%s, coverage=%.0f%%, rms=%s)",
                     cal.samples, cal.coverage_pct, cal.rms_deg)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            spawn(save(), name="calib-save")
        else:
            loop.call_soon_threadsafe(lambda: spawn(save(), name="calib-save"))

    def _on_button(self, event: ButtonEvent) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            spawn(self.handle_button(event), name="button")
        else:  # хаб вызвал из другого потока
            loop.call_soon_threadsafe(lambda: spawn(self.handle_button(event), name="button"))

    async def handle_button(self, event: ButtonEvent) -> dict | None:
        """Кнопка на GPS-модуле: короткое нажатие — вкл/выкл видеовыход на монитор,
        долгое — защитить текущую запись."""
        self.ws.event("button", value=event)
        if event == "press":
            return await self.toggle_video_output()
        if event != "long_press":
            return None
        try:
            res = await self.protect_current()
            self._beep("short")
            self._flash_led("on" if res.get("segment") else "blink_fast")
            return res
        except CameraUnavailable as e:
            self._beep("long")
            self._flash_led("blink_fast")
            self.ws.toast(f"Failed to protect recording: {e}", "error")
            return None

    async def toggle_video_output(self, on: bool | None = None) -> dict | None:
        try:
            state = await self.video.set(on)
        except VideoOutputError as e:
            self._flash_led("blink_fast")
            self.ws.toast(f"Video output: {e}", "error")
            return None
        self._beep("short")
        self._flash_led("on" if state else "off", seconds=1.5)
        self.ws.event("video_output", on=state)
        self.ws.toast(f"Video output {'ON' if state else 'OFF'}", "info")
        return {"ok": True, "on": state}

    def _beep(self, pattern: str) -> None:
        try:
            self.hub.beep(pattern)
        except Exception:
            log.exception("hub.beep()")

    async def protect_current(self) -> dict:
        res = await self.camera.protect_current()
        seg = res.get("segment")
        self.ws.event("protected", segment=seg)
        self.ws.toast(f"Recording protected: {seg}" if seg else "No current recording", "info" if seg else "warn")
        return res
