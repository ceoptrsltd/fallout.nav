"""RealSensorHub — настоящие датчики: gpsd (NEO-M8N), магнитометр IST8310 по I2C, кнопка/LED/пищалка.

Задачи asyncio: два клиента gpsd (JSON и NMEA), цикл слияния курса 20 Гц.
Поток: опрос компаса. Блокирующие операции GPIO и настройка M8N — в asyncio.to_thread.
Отсутствие любого устройства — не падение: текст в SensorSnapshot.errors и повторные попытки.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from fnav.interfaces import ButtonEvent, LedMode
from fnav.models import (
    CalibrationStatus,
    CompassCalibration,
    CompassRaw,
    GpsFix,
    Heading,
    SensorSnapshot,
    Settings,
)
from fnav.sensors import heading as hd
from fnav.sensors.calibration import CalibrationCollector, apply_calibration
from fnav.sensors.compass import CompassReader
from fnav.sensors.declination import declination_deg
from fnav.sensors.gpio import GpioController
from fnav.sensors.gpsd import GpsdClient, GpsState, NmeaRing

log = logging.getLogger("fnav.sensors")

FUSION_HZ = 20.0
MAG_STALE_S = 1.0
GPSD_HOST = os.environ.get("FNAV_GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(os.environ.get("FNAV_GPSD_PORT", "2947"))


def _state_file():
    from fnav.paths import data_dir

    return data_dir() / "sensors_state.json"


class RealSensorHub:
    """Реализует fnav.interfaces.SensorHub."""

    def __init__(self, settings: Settings, *, gpsd_host: str = GPSD_HOST, gpsd_port: int = GPSD_PORT,
                 enable_compass: bool = True, enable_gpio: bool = True):
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._errors: dict[str, str] = {}

        # GPS
        self.gps = GpsState()
        self.nmea = NmeaRing(500)
        self.gpsd = GpsdClient(gpsd_host, gpsd_port, on_message=self.gps.handle, on_connect=self.gps.on_connect)
        self.gpsd_nmea = GpsdClient(gpsd_host, gpsd_port, nmea=True, on_line=self.nmea.append)
        self._configuring = False
        self._autoconf_done = os.environ.get("FNAV_GPS_AUTOCONFIG", "1") == "0" or not sys.platform.startswith(
            "linux"
        )
        self.last_configure: dict | None = None

        # компас
        self._enable_compass = enable_compass
        self.compass_reader: CompassReader | None = None
        self._mag: tuple[float, tuple[float, float, float], str] | None = None  # (monotonic, FRD мкТл, чип)
        self.calib = CalibrationCollector()
        self._smoother = hd.HeadingSmoother(settings.heading.smoothing_s)
        self._last_pos: tuple[float, float] | None = self._load_last_pos()

        # GPIO
        self._enable_gpio = enable_gpio
        self._button_cbs: list[Callable[[ButtonEvent], None]] = []
        self.gpio = GpioController(None, self._dispatch_button)
        self._gpio_task: asyncio.Task | None = None

        self._snap = SensorSnapshot()
        self._last_fuse = time.monotonic()

    # ───────────── жизненный цикл ─────────────
    async def start(self) -> None:
        if self._tasks:
            return
        self._loop = asyncio.get_running_loop()
        self.gpio.loop = self._loop
        self._tasks = [
            asyncio.create_task(self.gpsd.run(), name="fnav-gpsd-json"),
            asyncio.create_task(self.gpsd_nmea.run(), name="fnav-gpsd-nmea"),
            asyncio.create_task(self._fusion_loop(), name="fnav-heading"),
        ]
        if self._enable_compass:
            self._start_compass()
        if self._enable_gpio:
            self._schedule_gpio_setup()

    async def stop(self) -> None:
        for t in self._tasks + ([self._gpio_task] if self._gpio_task else []):
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self.compass_reader is not None:
            self.compass_reader.stop()
            await asyncio.to_thread(self.compass_reader.join, 2.0)
            self.compass_reader = None
        await asyncio.to_thread(self.gpio.close)

    # ───────────── компас ─────────────
    def _compass_cfg(self) -> tuple[str, int, int | None, int]:
        c = self.settings.compass
        return c.chip, c.i2c_bus, c.address, c.rate_hz

    def _start_compass(self) -> None:
        self.compass_reader = CompassReader(self._compass_cfg, self._on_mag, self._on_compass_error)
        self.compass_reader.start()

    def _on_mag(self, v: tuple[float, float, float], chip: str) -> None:  # поток компаса
        self._mag = (time.monotonic(), v, chip)
        self.calib.add(v)

    def _on_compass_error(self, msg: str | None) -> None:  # поток компаса
        if msg:
            self._errors["compass"] = msg
        else:
            self._errors.pop("compass", None)

    # ───────────── GPIO ─────────────
    def _schedule_gpio_setup(self) -> None:
        if self._loop is None:
            return
        if self._gpio_task and not self._gpio_task.done():
            self._gpio_task.cancel()
        self._gpio_task = self._loop.create_task(self._gpio_setup_loop())

    async def _gpio_setup_loop(self) -> None:
        delay = 5.0
        while True:
            errs = await asyncio.to_thread(self.gpio.setup, self.settings.gpio)
            if not errs:
                self._errors.pop("gpio", None)
                return
            self._errors["gpio"] = "; ".join(errs)
            if any("not installed" in e for e in errs):
                return
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)

    def _dispatch_button(self, ev: str) -> None:  # в event loop
        for cb in list(self._button_cbs):
            try:
                cb(ev)  # type: ignore[arg-type]
            except Exception:
                log.exception("button callback failed")

    # ───────────── склонение ─────────────
    def _load_last_pos(self) -> tuple[float, float] | None:
        try:
            d = json.loads(_state_file().read_text(encoding="utf-8"))
            return float(d["lat"]), float(d["lon"])
        except Exception:  # noqa: BLE001
            return None

    def _save_last_pos(self, pos: tuple[float, float]) -> None:
        with contextlib.suppress(OSError):
            p = _state_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"lat": pos[0], "lon": pos[1]}), encoding="utf-8")
            os.replace(tmp, p)

    def _declination(self, fix: GpsFix) -> float | None:
        d = self.settings.compass.declination
        if not isinstance(d, str):
            return float(d)
        if fix.fix != "none" and fix.lat is not None and fix.lon is not None:
            pos = (round(fix.lat, 1), round(fix.lon, 1))
            if pos != self._last_pos:
                self._last_pos = pos
                if self._loop is not None:
                    self._loop.run_in_executor(None, self._save_last_pos, pos)
        if self._last_pos is None:
            return None
        return declination_deg(*self._last_pos)

    # ───────────── слияние ─────────────
    async def _fusion_loop(self) -> None:
        while True:
            try:
                self._fuse_once()
                self._maybe_autoconfigure()
            except Exception as e:
                log.exception("heading fusion failed")
                self._errors["fusion"] = f"Heading: internal error ({e})"
            await asyncio.sleep(1.0 / FUSION_HZ)

    def _fuse_once(self) -> None:
        now = time.monotonic()
        dt = now - self._last_fuse
        self._last_fuse = now
        s = self.settings
        fix = self.gps.current()

        decl = self._declination(fix)
        compass: CompassRaw | None = None
        mag_deg: float | None = None
        mag = self._mag
        if mag is not None:
            t, raw, chip = mag
            fresh = now - t <= MAG_STALE_S
            cx, cy, cz = apply_calibration(raw, s.compass.calibration)
            compass = CompassRaw(x=round(cx, 2), y=round(cy, 2), z=round(cz, 2),
                                 raw_x=round(raw[0], 2), raw_y=round(raw[1], 2), raw_z=round(raw[2], 2),
                                 chip=chip, ok=fresh)
            if fresh:
                mag_deg = hd.true_mag_heading(hd.mag_heading_deg(cx, cy), decl or 0.0, s.compass.mount_offset_deg)

        gps_course = fix.course_deg if fix.fix != "none" else None
        speed = fix.speed_mps if fix.fix != "none" else None
        deg, src = hd.fuse(mag_deg, gps_course, speed, s.heading.gps_above_mps, s.heading.blend_band_mps)
        self._smoother.tau_s = s.heading.smoothing_s
        sm = self._smoother.update(deg, dt)

        errors = []
        if self._configuring:
            errors.append("GPS: receiver is being reconfigured")
        else:
            if self.gpsd.status:
                errors.append(self.gpsd.status)
            elif (p := self.gps.problem()) is not None:
                errors.append(p)
        errors.extend(v for _, v in sorted(dict(self._errors).items()))
        if mag_deg is not None and decl is None and s.compass.declination == "auto":
            errors.append("Compass: magnetic declination unknown until the first GPS fix")

        self._snap = SensorSnapshot(
            gps=fix,
            heading=Heading(
                deg=None if sm is None else round(sm, 1),
                source=src,  # type: ignore[arg-type]
                mag_deg=None if mag_deg is None else round(mag_deg, 1),
                gps_course_deg=None if gps_course is None else round(gps_course, 1),
                declination_deg=None if decl is None else round(decl, 2),
            ),
            compass=compass,
            satellites=list(self.gps.satellites),
            errors=errors,
        )

    def _maybe_autoconfigure(self) -> None:
        """Однократно: если gpsd видит модуль на «чужой» скорости (заводские 38400) — перенастроить."""
        if self._autoconf_done or self._configuring or self._loop is None:
            return
        bps = self.gps.device_bps
        if bps and bps != self.settings.gps.baud:
            self._autoconf_done = True
            log.info("GPS at %s bps, expected %s: running UBX configuration", bps, self.settings.gps.baud)
            self._loop.create_task(self.gps_configure())

    # ───────────── интерфейс SensorHub ─────────────
    def snapshot(self) -> SensorSnapshot:
        return self._snap

    def apply_settings(self, settings: Settings) -> None:
        old = self.settings
        self.settings = settings
        if (old.compass.chip, old.compass.i2c_bus, old.compass.address) != (
            settings.compass.chip, settings.compass.i2c_bus, settings.compass.address
        ) and self.compass_reader is not None:
            self.compass_reader.request_reinit()
        if old.gpio != settings.gpio and self._enable_gpio and self._loop is not None:
            self._loop.call_soon_threadsafe(self._schedule_gpio_setup)

    def nmea_tail(self, n: int = 50) -> list[str]:
        return self.nmea.tail(n)

    async def gps_configure(self) -> dict:
        if self._configuring:
            return {"ok": False, "details": "GPS configuration is already running"}
        from fnav.sensors.m8n import configure_receiver

        g = self.settings.gps
        lines: list[str] = []
        self._configuring = True
        try:
            res: dict[str, Any] = await asyncio.to_thread(
                configure_receiver, g.device, g.baud, g.rate_hz, list(g.constellations), g.dyn_model,
                log=lines.append,
            )
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
        finally:
            self._configuring = False
        res["log"] = lines
        self.last_configure = res
        return {"ok": bool(res.get("ok")), "details": res}

    # калибровка
    def calibration_start(self) -> None:
        self.calib.start()

    def calibration_status(self) -> CalibrationStatus:
        st = self.calib.status()
        if st.active and "compass" in self._errors:
            st = st.model_copy(update={"message": self._errors["compass"]})
        return st

    def calibration_finish(self) -> CompassCalibration:
        """CalibrationError (подкласс ValueError) с текстом, если данных мало — сбор продолжается."""
        cal = self.calib.finish()
        new = self.settings.model_copy(deep=True)
        new.compass.calibration = cal
        self.settings = new
        return cal

    def calibration_cancel(self) -> None:
        self.calib.cancel()

    # кнопка / LED / пищалка
    def on_button(self, callback: Callable[[ButtonEvent], None]) -> None:
        self._button_cbs.append(callback)

    def set_led(self, mode: LedMode) -> None:
        self.gpio.set_led(mode)

    def beep(self, pattern: str = "short") -> None:
        self.gpio.beep(pattern)

