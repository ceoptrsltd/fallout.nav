"""Ядро процесса fnav-core: связывает настройки, датчики, камеру, тайлы, WS и фоновые задачи."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from fnav import paths
from fnav.camera.discover import replace_host, url_host
from fnav.core import secretsfile as sf
from fnav.core.alarm import DETECTED, AlarmService
from fnav.core.auth import AuthManager
from fnav.core.autorear import AutoRear
from fnav.core.bluetooth import create_bluetooth
from fnav.core.camera import (
    CAMERA_URL,
    REAR_CAMERA_URL,
    REAR_WHEP_URL,
    WHEP_URL,
    CameraService,
    CameraUnavailable,
)
from fnav.core.config import SettingsStore
from fnav.core.games import GamesService
from fnav.core.guests import GuestLinks, redact_message
from fnav.core.impact import ImpactService
from fnav.core.imu import ImuService
from fnav.core.keypad import KeypadService
from fnav.core.level import LevelService
from fnav.core.marks import MarkLog
from fnav.core.navlogic import NavLogic
from fnav.core.radio import MOCK_STATION, Player, create_radio
from fnav.core.recgps import RecGps
from fnav.core.recordings import Recordings
from fnav.core.speedlimit import SpeedLimitService
from fnav.core.system import SystemService, run
from fnav.core.tiles import TileService
from fnav.core.timezone import TimeZoneFollower
from fnav.core.tracking import RelayConfig, TrackService
from fnav.core.util import cancel_tasks, spawn
from fnav.core.videoout import VideoOutput, VideoOutputError
from fnav.core.vision import WATCH_URL, VisionClient
from fnav.core.weather import WeatherService
from fnav.core.ws import WsManager, new_nmea_lines
from fnav.interfaces import ButtonEvent, LedMode, SensorHub, create_sensor_hub
from fnav.models import (
    CalibrationStatus,
    CameraStatus,
    CompassCalibration,
    DisplayState,
    LevelCalibration,
    SensorSnapshot,
    Settings,
    Telemetry,
)
from fnav.sensors.hazard import FakeHazard, open_gpio_hazard

log = logging.getLogger("fnav.core")

TELEMETRY_HZ = 10.0
REMOTE_TELEMETRY_EVERY = 5  # через туннель (мобильный интернет) — 2 Гц вместо 10
DEV_PASSWORD = "fnav-dev"


@dataclass
class CoreOptions:
    mock: bool = False
    trust_localhost: bool = True
    background: bool = True  # фоновые циклы (телеметрия, опросы); в unit-тестах можно выключить
    camera_url: str = CAMERA_URL
    rear_camera_url: str = REAR_CAMERA_URL
    rear_whep_url: str = REAR_WHEP_URL
    watch_url: str = WATCH_URL
    whep_url: str = WHEP_URL
    local_transport: httpx.AsyncBaseTransport | None = None  # для камеры/MediaMTX (тесты)
    tile_transport: httpx.AsyncBaseTransport | None = None  # для Esri (тесты)
    hub_factory: Callable[[Settings], SensorHub] | None = None
    secrets_path: Path | None = None
    remote_port: int | None = None  # вход ssh-туннеля с сервера (без доверия к localhost)
    relay: RelayConfig | None = None  # None — из окружения (/etc/fnav/remote.env)
    relay_transport: httpx.AsyncBaseTransport | None = None  # для fnav-relay (тесты)
    radio_transport: httpx.AsyncBaseTransport | None = None  # для каталога radio-browser (тесты)
    radio_player: Player | None = None  # подмена плеера (тесты); None — mpv или FakePlayer в --mock
    weather_transport: httpx.AsyncBaseTransport | None = None  # для Open-Meteo (тесты)


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
                                trust_localhost=opts.trust_localhost, remote_port=opts.remote_port)
        self.ws = WsManager()
        s = self.store.get()
        self.nav = NavLogic(s.nav, s.display, start_time=time.monotonic())
        self.recordings = Recordings(current_fn=self.current_segments)  # «идёт запись» — и по задней
        self.tiles = TileService(s, transport=opts.tile_transport)
        self.camera = CameraService(mock=opts.mock, recordings_root=paths.recordings_dir(),
                                    base_url=opts.camera_url, whep_url=opts.whep_url,
                                    transport=opts.local_transport,
                                    get_speed=lambda: self.hub.snapshot().gps.speed_mps)
        # задняя камера (fnav-camera-rear): выключена в настройках — не опрашивается, в телеметрии None
        self.camera_rear = CameraService(mock=opts.mock, recordings_root=paths.recordings_dir() / "rear",
                                         base_url=opts.rear_camera_url, whep_url=opts.rear_whep_url,
                                         transport=opts.local_transport, camera_id="rear",
                                         get_enabled=lambda: self.store.get().rear_camera.enabled,
                                         get_sound_armed=self._sound_wanted, on_sound=self._on_sound)
        self.view_camera = "front"  # что показывает экран в режиме камеры (кнопка «камеры»: передняя ↔ задняя)
        self.auto_rear = AutoRear()  # задний вид при заднем ходе (fnav/core/autorear.py)
        self.system = SystemService(mock=opts.mock)
        self.video = VideoOutput(mock=opts.mock)
        relay = opts.relay or RelayConfig.from_env()
        self.tracking = TrackService(paths.data_dir() / "track_outbox.db", relay,
                                     lambda: (self.auth.password_hash, self.auth.session_secret),
                                     transport=opts.relay_transport)
        # гостевые ссылки «только просмотр»: проверка — AuthMiddleware и ws.py, список ссылок узнаёт и fnav-relay;
        # отозвали — открытые гостевые WebSocket закрываются
        self.guests = GuestLinks(paths.data_dir() / "guest_links.json", on_change=lambda: (
            self.tracking.resync_auth(), self.ws.drop_guests(self.guests.active)))
        self.auth.guests = self.guests
        self.tracking.get_guests = self.guests.relay_list
        self.ws.guest_filter = redact_message
        self.bt = create_bluetooth(opts.mock, paths.data_dir(), lambda: self.store.get().bluetooth)
        self.radio = create_radio(opts.mock, paths.data_dir(), lambda: self.store.get().radio,
                                  self._save_radio_volume, transport=opts.radio_transport,
                                  player=opts.radio_player)
        self.bt.on_output(self.radio.set_output)
        self.speedlimits = SpeedLimitService(paths.data_dir() / "speedlimits.sqlite", lambda: self.hub.snapshot().gps)
        self.timezone = TimeZoneFollower(lambda: self.store.get().system.auto_timezone, mock=opts.mock)
        self.weather = WeatherService(paths.data_dir() / "weather.json", lambda: self.hub.snapshot().gps,
                                      lambda st: self.ws.broadcast(st.model_dump(mode="json")),
                                      mock=opts.mock, transport=opts.weather_transport,
                                      on_zone=self.timezone.observe)
        self.games = GamesService(lambda: self.hub.snapshot().gps.speed_mps, self._pause_radio_for_game,
                                  self._resume_radio_after_game, lambda lvl, text: self.ws.toast(text, lvl),
                                  mock=opts.mock, prepare_audio=self._audio_for_game,
                                  audio_ok=lambda: bool((o := self.bt.output()) and o.connected))
        # MPU-6050: один поток чтения на охрану и уровень
        self.imu = ImuService(lambda: self.store.get().alarm, mock=opts.mock)
        self.alarm = AlarmService(paths.data_dir(), lambda: self.store.get().alarm, self.imu, relay,
                                  get_fix=lambda: self.hub.snapshot().gps, protect=self.camera.protect_current,
                                  snapshot=self.camera.snapshot, radio=self.radio, transport=opts.relay_transport,
                                  open_hazard=FakeHazard if opts.mock else open_gpio_hazard)
        self.alarm.protect_rear = self._protect_rear_quiet
        self.alarm.snapshot_rear = self._snapshot_rear_quiet
        self.alarm.get_video = self._camera_video
        # отметки движения с камер (без тревоги) — серые метки на шкале записей
        self.marks = MarkLog(paths.data_dir() / "vision_marks.jsonl")
        self.vision = VisionClient(get_settings=lambda: self.store.get().alarm.vision,
                                   get_armed=lambda: self.alarm.state in ("armed", "triggered"),
                                   on_event=self.alarm.vision_event, base_url=opts.watch_url,
                                   transport=opts.local_transport, on_mark=self._on_mark)
        self.alarm.vision_status = self.vision.status
        self.level = LevelService(self.imu, lambda: self.store.get().level, self._save_level_calibration,
                                  get_fix=lambda: self.hub.snapshot().gps)
        # удар на ходу: защитить текущие записи обеих камер, сигнал, метка на шкале записей; трек GPS к записям
        self.impact = ImpactService(paths.data_dir() / "impacts.jsonl", lambda: self.store.get().impact, self.imu,
                                    get_fix=lambda: self.hub.snapshot().gps, protect=self.protect_current,
                                    beep=self._beep)
        self.recgps = RecGps(paths.recordings_dir, lambda: self.hub.snapshot().gps, (self.camera, self.camera_rear))
        self.keypad = KeypadService({
            "station_prev": lambda: self.radio.step(-1),
            "station_next": lambda: self.radio.step(1),
            "view_toggle": self.toggle_view,
            "video_toggle": self.toggle_video_output,
            "camera_switch": self.switch_camera,
            "protect": self.protect_with_feedback,
            "shuffle": self._keypad_shuffle,
            "favorite": self._keypad_favorite,
            "radio_toggle": self._keypad_radio_toggle,
            "volume_down": lambda: self._keypad_volume(-1),
            "volume_up": lambda: self._keypad_volume(1),
            "alarm": self._keypad_alarm,
        }, get_bindings=lambda: self.store.get().keypad)
        self.hub: SensorHub = self._make_hub(s)
        if (attach_imu := getattr(self.hub, "attach_imu", None)) is not None:
            attach_imu(self.imu)  # гироскоп MPU-6050 — в курс, акселерометр — наклон компаса и задний ход
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
        self.camera_rear.on_status(self._on_rear_status)
        self._rear_host_saving: str | None = None

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
        await self.camera_rear.start(background=bg)
        self._on_camera_status(self.camera.status)
        try:
            await self.bt.start(background=bg)
            await self.radio.start(demo=MOCK_STATION if self.mock else None)
        except Exception:
            log.exception("radio/bluetooth start")
        # охрана, отметки, fnav-watch, уровень, датчик — каждый сам по себе: сбой одного не оставит без остальных
        for name, start in (("alarm", self.alarm.start), ("marks", self.marks.start), ("vision", self.vision.start),
                            ("level", self.level.start), ("impact", self.impact.start)):
            try:
                await start(background=bg)
            except Exception:
                log.exception("%s start", name)
        if bg:
            try:
                self.imu.start()
            except Exception:
                log.exception("imu start")
        await self._refresh_slow()
        if bg:
            self._tasks += [
                asyncio.create_task(self._telemetry_loop(), name="telemetry"),
                asyncio.create_task(self._slow_loop(), name="slow-status"),
            ]
            self.tracking.start(lambda: self.hub.snapshot().gps, lambda: self.store.get().tracking)
            self.weather.start()
            self.speedlimits.start()
            self.games.start()
            self.keypad.start()
            self.recgps.start()
        self.started = True
        log.info("fnav-core started (mock=%s)", self.mock)

    async def stop(self) -> None:
        await cancel_tasks(*self._tasks)
        self._tasks.clear()
        for closer in (self.imu.stop, self.vision.stop, self.marks.stop, self.alarm.stop, self.level.stop, self.speedlimits.stop, self.weather.stop,
                       self.keypad.stop, self.games.close, self.radio.stop, self.bt.stop, self.tracking.stop,
                       self.impact.stop, self.recgps.stop,
                       self.hub.stop, self.camera.stop, self.camera_rear.stop, self.tiles.stop):
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
        spawn(self.camera_rear.reload(), name="camera-rear-reload")
        if not s.rear_camera.enabled and self.view_camera == "rear":
            self.view_camera = "front"
        try:
            self.bt.apply_settings(s.bluetooth)
            self.radio.apply_settings(s.radio)
        except Exception:
            log.exception("radio/bluetooth apply_settings")
        try:
            self.imu.apply_settings(s.alarm)
            self.alarm.apply_settings(s.alarm)
        except Exception:
            log.exception("alarm apply_settings")

    async def _save_radio_volume(self, volume: int) -> None:
        await self.store.patch({"radio": {"volume": volume}})

    async def _save_level_calibration(self, cal: LevelCalibration | None) -> None:
        await self.store.patch({"level": {"calibration": cal.model_dump(mode="json") if cal else None}})

    # ── игры: радио на паузе, пока идёт игра ──
    async def _audio_for_game(self) -> None:
        """Перед игрой: колонка по умолчанию должна быть подключена — звук игра открывает только при старте."""
        out = self.bt.output()
        if out is None or self.mock:
            return
        if not out.connected:
            try:
                await asyncio.wait_for(self.bt.connect(out.address), timeout=10)
            except Exception as e:  # noqa: BLE001 — нет колонки: играем без звука
                log.info("games: speaker %s not connected (%s)", out.name, e)
                return
        for _ in range(10):  # PCM у bluealsa появляется чуть позже подключения
            _rc, pcms, _ = await run("bluealsa-cli", "list-pcms", timeout=3)
            if "a2dpsrc" in pcms:
                return
            await asyncio.sleep(0.5)

    async def _pause_radio_for_game(self) -> bool:
        return await self.radio.pause_for_game()  # «на паузе ради игры» — в radio_state.json

    async def _resume_radio_after_game(self) -> None:
        await self.radio.resume_after_game()  # ничего, если радио молчало не ради игры

    def settings_message(self) -> str:
        return json.dumps({"type": "settings", "settings": self.store.get().model_dump(mode="json")},
                          ensure_ascii=False)

    # ── телеметрия ──
    def build_telemetry(self, now_mono: float | None = None) -> Telemetry:
        snap = self.hub.snapshot()
        now = now_mono if now_mono is not None else time.monotonic()
        self.display_state = self.nav.update(now, snap.gps)
        self._auto_rear_tick(now, snap)
        errors = list(snap.errors)
        cam = self.camera.status
        if cam.state == "error" and cam.error:
            errors.append(f"Camera: {cam.error}")
        rear = self.camera_rear.status if self.camera_rear.enabled else None
        if rear is not None and rear.state == "error" and rear.error:
            errors.append(rear.error if rear.error.startswith("Rear camera") else f"Rear camera: {rear.error}")
        disp = self.view_state(self.display_state)
        if self.games.pad_present():  # экран опрашивает геймпад, только когда он есть (ui/games-menu.js)
            disp = disp.model_copy(update={"gamepad": True})
        return Telemetry(
            t=time.time(),
            gps=snap.gps,
            heading=snap.heading,
            compass=snap.compass,
            compass_cal=snap.compass_cal,
            display=disp,
            camera=cam,
            camera_rear=rear,
            disk=self.system.disk,
            system=self.system.status,
            radio=self.radio.status(),
            speed_limit=self.speedlimits.status(),
            alarm=self.alarm.telemetry(),
            level=self.level.telemetry(),
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
                    self.ws.broadcast_telemetry(tel.model_dump_json(), tick)
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

    def current_segments(self) -> list[str]:
        """Пишущиеся сейчас сегменты обеих камер — по статусам их сервисов (задняя по Wi-Fi молчит и 3–15 с, а файл
        открыт: по mtime его не отличить от законченного)."""
        out = []
        for cam in (self.camera, self.camera_rear):
            st = cam.status
            if st.current_segment and (st.recording or st.state == "recording"):
                out.append(st.current_segment)
        return out

    def _on_rear_status(self, st: CameraStatus) -> None:
        """Сервис задней нашёл камеру по MAC на новом адресе (DHCP точки доступа) и уже подключается туда — адрес в
        настройки (сервис камеры settings.json не пишет): хост в rear_camera.url и lores_url (если там тот же)."""
        host, rc = st.discovered_host, self.store.get().rear_camera
        old = url_host(rc.url)
        if not host or host == old or host == self._rear_host_saving or not rc.enabled:
            return
        patch = {"url": replace_host(rc.url, host)}
        if rc.lores_url and url_host(rc.lores_url) == old:
            patch["lores_url"] = replace_host(rc.lores_url, host)
        log.warning("rear camera: moved from %s to %s (found by its MAC) - saving the new address", old, host)
        self._rear_host_saving = host

        async def save() -> None:
            try:
                await self.store.patch({"rear_camera": patch})
            except Exception:
                log.exception("rear camera: cannot save the new address")
            finally:
                self._rear_host_saving = None

        spawn(save(), name="rear-host")

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
        """Кнопка на GPS-модуле: короткое нажатие — передняя ↔ задняя камера на экране,
        долгое — защитить текущую запись."""
        self.ws.event("button", value=event)
        if event == "press":
            return await self.switch_camera()
        if event != "long_press":
            return None
        return await self.protect_with_feedback()

    async def protect_with_feedback(self) -> dict | None:
        """Защитить текущую запись + звук/светодиод (кнопка на GPS-модуле, клавиша 5 мини-клавиатуры)."""
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

    # ── мини-клавиатура ──
    async def toggle_view(self) -> None:
        """Карта ↔ камера: из авто — в противоположный текущему режим; из ручного — обратно в авто.
        Задний вид на экране — просто выйти из него."""
        if self.view_camera == "rear":
            self.view_camera = "front"
            self.auto_rear.manual()
            self.ws.event("camera_view", camera="front")
            return
        ds = self.store.get().display
        if ds.force_mode != "auto":
            mode = "auto"
        else:
            mode = "map" if self.display_state.mode == "camera" else "camera"
        await self.store.patch({"display": {"force_mode": mode}})

    async def _keypad_shuffle(self) -> None:
        """Клавиша 6: следующая станция — по порядку → случайная → по избранному (если оно есть) → по порядку.
        Экран показывает режим на плашке радио."""
        if self.games.running:  # в игре радио на паузе; геймпад отключили вместо клавиатуры — выйти из игры
            await self.games.stop("keypad")
            return
        mode = {"order": "random", "random": "favorites", "favorites": "order"}[self.store.get().radio.mode]
        if mode == "favorites" and not self.radio.favorites:
            mode = "order"
        await self.store.patch({"radio": {"mode": mode}})

    async def _keypad_favorite(self) -> None:
        """Клавиша 6 (удержание): играющую станцию — в избранное; экран: FAVORITE SAVED / ALREADY SAVED.
        Радио выключено — ничего (плашки нет, подтвердить нечем)."""
        if self.games.running or not self.radio.want_play:
            return
        await self.radio.add_favorite(from_keypad=True)

    async def _keypad_radio_toggle(self) -> None:
        """Нажатие крутилки: радио играет (или ждёт колонку / ищет сигнал) — стоп, иначе — последняя станция."""
        if self.games.running:  # в игре Enter нужен самой игре, радио и так на паузе
            return
        if self.radio.want_play:
            await self.radio.stop_playback()
        else:
            await self.radio.resume()

    async def _keypad_alarm(self) -> None:
        """Клавиша «Alarm on/off»: off → arming/armed, иначе → off (и сирена молчит); в журнал и push хозяину. Экран
        показывает вспышку ALARM ON / ALARM OFF по смене Telemetry.alarm.state."""
        on = await self.alarm.toggle(source="keypad")
        self._beep("short" if on else "double")

    async def _keypad_volume(self, direction: int) -> None:
        if not self.games.running:
            await self.radio.set_volume(self.radio.volume + 5 * direction)
            return
        # в игре — громкость самой колонки (A2DP, 0…127): игры свою громкость наружу не дают
        _rc, pcms, _ = await run("bluealsa-cli", "list-pcms", timeout=3)
        pcm = next((ln.strip() for ln in pcms.splitlines() if "a2dpsrc" in ln), None)
        if not pcm:
            return
        _rc, out, _ = await run("bluealsa-cli", "volume", pcm, timeout=3)
        nums = [int(x) for x in re.findall(r"(\d{1,3})", out)]
        cur = nums[0] if nums else 100
        await run("bluealsa-cli", "volume", pcm, str(max(0, min(127, cur + 10 * direction))), timeout=3)

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

    # ── передняя ↔ задняя камера на экране ──
    def view_state(self, disp: DisplayState) -> DisplayState:
        """Задний вид поверх авто-логики: экран в режиме камеры с задней камерой, пока его не выключат кнопкой."""
        mirror = self.store.get().rear_camera.mirror
        if self.view_camera == "rear" and self.camera_rear.enabled:
            return disp.model_copy(update={"mode": "camera", "reason": "rear", "camera": "rear",
                                           "rear_mirror": mirror})
        return disp.model_copy(update={"camera": "front", "rear_mirror": mirror})

    async def switch_camera(self) -> dict | None:
        """Кнопка «камеры» (клавиша 4, кнопка GPS-модуля, админка): задний вид на экране ↔ обратно. Задняя камера
        выключена в настройках — длинный сигнал и подсказка, экран не меняется. Ручной выбор главнее заднего вида по
        заднему ходу — до следующего заднего хода."""
        if self.view_camera == "rear":
            self.view_camera = "front"
        elif not self.store.get().rear_camera.enabled:
            self._beep("long")
            self.ws.toast("Rear camera is off: turn it on in Camera → Rear", "warn")
            return {"ok": False, "camera": "front", "error": "Rear camera is off"}
        else:
            self.view_camera = "rear"
        self.auto_rear.manual()
        self._beep("short")
        self.ws.event("camera_view", camera=self.view_camera)
        return {"ok": True, "camera": self.view_camera}

    def _auto_rear_tick(self, now: float, snap: SensorSnapshot) -> None:
        """Задний ход дольше 1 с — задний вид, вперёд > 5 км/ч или стоим 10 с — обратно (fnav/core/autorear.py)."""
        s = self.store.get()
        want = self.auto_rear.update(
            now, reversing=bool(getattr(snap, "reversing", False)),  # поле хаба — SensorSnapshot.reversing
            speed_mps=snap.gps.speed_mps if snap.gps.fix != "none" else None,
            enabled=s.display.auto_rear_on_reverse and s.rear_camera.enabled, view=self.view_camera)
        if want is not None and want != self.view_camera:
            self.view_camera = want
            log.info("rear view %s (reversing)", "on" if want == "rear" else "off")
            self.ws.event("camera_view", camera=want, auto=True)

    def rec_marks(self, t0: float, t1: float, camera: str | None = None) -> list[dict]:
        """Метки на шкале записей: отметки движения с камер и удары на ходу (recordings.attach_marks)."""
        return sorted(self.marks.between(t0, t1, camera) + self.impact.between(t0, t1, camera), key=lambda m: m["ts"])

    def _on_mark(self, data: dict, ts: float) -> None:
        """Отметка движения от fnav-watch (без тревоги) — в журнал отметок, если они включены."""
        v = self.store.get().alarm.vision
        if v.marks and (data.get("camera") != "rear" or v.rear):
            self.marks.add(data, ts)

    def _sound_wanted(self) -> bool:
        """Громкие звуки у задней камеры пишутся клипами, только пока стоит охрана (и это включено)."""
        s = self.store.get()
        return bool(s.alarm.vision.sound and s.rear_camera.audio and self.alarm.state in DETECTED)

    def _on_sound(self, ev: dict) -> None:
        """Громкий звук у задней камеры на охране — отметка с клипом (без тревоги, как отметки движения)."""
        clip, start = ev.get("clip"), ev.get("start")
        if isinstance(start, (int, float)) and clip and any(
                m.get("clip") == clip for m in self.marks.between(start - 5, start + 5, "rear")):
            return  # уже есть (ядро или сервис камеры перезапускались)
        self.marks.add({"what": "sound", "camera": "rear",
                        **{k: ev.get(k) for k in ("start", "dur_s", "peak_db", "clip", "clip_start", "clip_dur_s")}},
                       time.time())

    def _camera_video(self) -> dict[str, bool]:
        """Идёт ли видео камер (охрана: пропало — тревога); выключенной задней нет."""
        out = {"front": self.camera.has_video()}
        if self.camera_rear.enabled:
            out["rear"] = self.camera_rear.has_video()
        return out

    async def drop_camera_video(self, camera: str, seconds: float) -> bool:
        """Только --mock: у mock-камеры «пропадает» видео на seconds. False — камера не mock (или выключена)."""
        cam = self.camera_rear if camera == "rear" else self.camera
        if not cam.enabled or not cam.using_mock or cam.mockcam is None:
            return False
        cam.mockcam.drop_video(seconds)
        await cam.poll()
        return True

    async def _protect_rear_quiet(self, recent: bool = False) -> dict:
        """Для сигнализации: защитить запись задней, если она включена (выключена — пусто, без ошибки)."""
        if not self.camera_rear.enabled:
            return {}
        return await self.camera_rear.protect_current(recent=recent)

    async def _snapshot_rear_quiet(self, before_s: float = 0.0) -> bytes:
        if not self.camera_rear.enabled:
            return b""
        return await self.camera_rear.snapshot(before_s)

    async def set_rear_credentials(self, username: str | None, password: str | None) -> dict:
        """Логин — в настройки, пароль — в secrets.env (FNAV_REAR_CAMERA_PASSWORD; пустой — удалить), камере — /reload."""
        if username is not None:
            await self.store.patch({"rear_camera": {"username": username}})
        if password is not None:
            await asyncio.to_thread(sf.update_secrets, {sf.KEY_REAR_CAMERA_PASSWORD: password},
                                    self.auth.secrets_path)
        await self.camera_rear.reload()
        return {"ok": True, "username": self.store.get().rear_camera.username, "password_set": bool(
            password if password is not None else (await asyncio.to_thread(self._rear_password_set)))}

    def _rear_password_set(self) -> bool:
        try:
            return bool(sf.read_secrets(self.auth.secrets_path).get(sf.KEY_REAR_CAMERA_PASSWORD))
        except OSError:
            return False

    def _beep(self, pattern: str) -> None:
        try:
            self.hub.beep(pattern)
        except Exception:
            log.exception("hub.beep()")

    async def protect_current(self) -> dict:
        """Защитить текущие записи передней и задней (если она включена) — каждую сама по себе: нет записи одной
        камеры — защищается другая. Нет ни одной — ошибка передней наружу."""
        res: dict = {"ok": True, "segment": None}
        front_err: CameraUnavailable | None = None
        try:
            res = await self.camera.protect_current()
        except CameraUnavailable as e:
            front_err = e
            log.info("front camera: cannot protect: %s", e)
        seg = res.get("segment")
        rear = None
        if self.camera_rear.enabled:
            try:
                rear = (await self.camera_rear.protect_current()).get("segment")
            except CameraUnavailable as e:
                log.info("rear camera: cannot protect: %s", e)
        if front_err is not None and not rear:
            raise front_err
        res = {**res, "segment_rear": rear}
        self.ws.event("protected", segment=seg, segment_rear=rear)
        segs = ", ".join(x for x in (seg, rear) if x)
        self.ws.toast(f"Recording protected: {segs}" if segs else "No current recording", "info" if segs else "warn")
        return res
