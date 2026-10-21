"""RealSensorHub — настоящие датчики: gpsd (NEO-M8N), магнитометр IST8310 по I2C, кнопка/LED/пищалка.

Задачи asyncio: два клиента gpsd (JSON и NMEA), цикл слияния курса 20 Гц, автокалибровка компаса по GPS.
Поток: опрос компаса. Блокирующие операции GPIO, подгонка и настройка M8N — в asyncio.to_thread.
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
    AutoCalStatus,
    CalibrationStatus,
    CompassCalibration,
    CompassRaw,
    GpsFix,
    Heading,
    SensorSnapshot,
    Settings,
)
from fnav.sensors import heading as hd
from fnav.sensors.autocal import AutoCalibrator
from fnav.sensors.calibration import CalibrationCollector, apply_calibration
from fnav.sensors.compass import CompassReader
from fnav.sensors.declination import declination_deg
from fnav.sensors.gpio import GpioController
from fnav.sensors.gpsd import GpsdClient, GpsState, NmeaRing
from fnav.sensors.stillness import StillnessFilter

log = logging.getLogger("fnav.sensors")

FUSION_HZ = 20.0
MAG_STALE_S = 1.0
AUTOCAL_TICK_S = 5.0  # как часто проверять, не пора ли переподогнать автокалибровку
AUTOCAL_MIN_NEW = 5  # столько новых точек — повод для переподгонки
STATE_SAVE_S = 180.0  # как часто сохранять накопленные точки в sensors_state.json
AUTOCONF_RETRY_S = 600.0
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
        self.gpsd = GpsdClient(gpsd_host, gpsd_port, on_message=self._on_gps_msg, on_connect=self.gps.on_connect)
        self.gpsd_nmea = GpsdClient(gpsd_host, gpsd_port, nmea=True, on_line=self.nmea.append)
        self._configuring = False
        self._autoconf_done = os.environ.get("FNAV_GPS_AUTOCONFIG", "1") == "0" or not sys.platform.startswith(
            "linux"
        )
        self._autoconf_next = 0.0
        self._autoconf_attempts = 0
        self.last_configure: dict | None = None

        # компас
        self._enable_compass = enable_compass
        self.compass_reader: CompassReader | None = None
        self._mag: tuple[float, tuple[float, float, float], str] | None = None  # (monotonic, FRD мкТл, чип)
        self.calib = CalibrationCollector()
        self._smoother = hd.HeadingSmoother(settings.heading.smoothing_s)
        self._still = StillnessFilter()
        self._anchor = hd.CompassAnchor()

        # автокалибровка по GPS-курсу
        self.autocal = AutoCalibrator(settings.compass)
        self._cal_auto: CompassCalibration | None = None  # применённая автокалибровка (может быть не сохранена)
        self._cal_cbs: list[Callable[[CompassCalibration], None]] = []
        self._autocal_axes: str | None = None  # оси, в которых накоплены точки
        self._last_fit = 0.0
        self._state_saved_at = time.monotonic()
        self._last_pos: tuple[float, float] | None = None
        self._load_state()

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
            asyncio.create_task(self._autocal_loop(), name="fnav-autocal"),
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
        if self.autocal.dirty:
            await asyncio.to_thread(self._save_state)

    # ───────────── компас ─────────────
    def _compass_cfg(self) -> tuple[str, int, int | None, int, str]:
        c = self.settings.compass
        return c.chip, c.i2c_bus, c.address, c.rate_hz, c.axes

    def _start_compass(self) -> None:
        self.compass_reader = CompassReader(self._compass_cfg, self._on_mag, self._on_compass_error)
        self.compass_reader.start()

    def _on_mag(self, v: tuple[float, float, float], chip: str) -> None:  # поток компаса
        t = time.monotonic()
        self._mag = (t, v, chip)
        self.calib.add(v)
        self.autocal.add_mag(t, v)

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

    # ───────────── состояние на диске (позиция + точки автокалибровки) ─────────────
    def _load_state(self) -> None:
        try:
            d = json.loads(_state_file().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — нет файла / битый файл
            return
        if not isinstance(d, dict):
            return
        with contextlib.suppress(Exception):
            self._last_pos = (float(d["lat"]), float(d["lon"]))
        ac = d.get("autocal")
        if isinstance(ac, dict):
            n = self.autocal.load_state(ac)
            axes = ac.get("axes")
            self._autocal_axes = axes if (n and isinstance(axes, str)) else None
            if n:
                log.info("compass auto-calibration: %d stored points loaded", n)

    def _state_dict(self) -> dict:
        d: dict[str, Any] = {}
        if self._last_pos is not None:
            d["lat"], d["lon"] = self._last_pos
        ac = self.autocal.to_state()
        ac["axes"] = self._autocal_axes
        d["autocal"] = ac
        return d

    def _save_state(self) -> None:  # блокирующее — вызывать из потока
        with contextlib.suppress(OSError):
            data = json.dumps(self._state_dict(), separators=(",", ":"))
            p = _state_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, p)
        self._state_saved_at = time.monotonic()

    # ───────────── склонение ─────────────
    def _declination(self, fix: GpsFix) -> float | None:
        d = self.settings.compass.declination
        if not isinstance(d, str):
            return float(d)
        if fix.fix != "none" and fix.lat is not None and fix.lon is not None:
            pos = (round(fix.lat, 1), round(fix.lon, 1))
            if pos != self._last_pos:
                self._last_pos = pos
                if self._loop is not None:
                    self._loop.run_in_executor(None, self._save_state)
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
        fix = self._still.apply(now, self.gps.current(), s.gps)

        decl = self._declination(fix)
        compass: CompassRaw | None = None
        mag_deg: float | None = None
        mag = self._mag
        if mag is not None:
            t, raw, chip = mag
            fresh = now - t <= MAG_STALE_S
            cal = self.effective_calibration()
            cx, cy, cz = apply_calibration(raw, cal)
            compass = CompassRaw(x=round(cx, 2), y=round(cy, 2), z=round(cz, 2),
                                 raw_x=round(raw[0], 2), raw_y=round(raw[1], 2), raw_z=round(raw[2], 2),
                                 chip=chip, ok=fresh)
            if fresh:
                mag_deg = hd.true_mag_heading(hd.mag_heading_deg(cx, cy), decl or 0.0, s.compass.mount_offset_deg,
                                              cal.deviation)

        gps_course = fix.course_deg if fix.fix != "none" else None
        speed = fix.speed_mps if fix.fix != "none" else None
        fused_mag = mag_deg
        if s.heading.anchor_to_gps and mag_deg is not None:
            self._anchor.tau_s = s.heading.anchor_tau_s
            # учимся только когда курс полностью по GPS (скорость выше зоны перехода) — там он надёжен
            if gps_course is not None and hd.gps_weight(speed, s.heading.gps_above_mps,
                                                        s.heading.blend_band_mps) >= 1.0:
                self._anchor.learn(mag_deg, gps_course, dt)
            fused_mag = self._anchor.apply(mag_deg)
        deg, src = hd.fuse(fused_mag, gps_course, speed, s.heading.gps_above_mps, s.heading.blend_band_mps)
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
                anchor_offset_deg=None if self._anchor.offset_deg is None or not s.heading.anchor_to_gps
                else round(self._anchor.offset_deg, 1),
            ),
            compass=compass,
            compass_cal=self._autocal_status(),
            satellites=list(self.gps.satellites),
            errors=errors,
        )

    # ───────────── автонастройка M8N ─────────────
    def _maybe_autoconfigure(self) -> None:
        """Если gpsd РЕАЛЬНО получает данные от модуля (TPV после подключения), но на «чужой» скорости
        (заводские 38400) — перенастроить. Без данных не трогаем: при поиске скорости gpsd показывает
        случайные bps. Неудача → повтор через AUTOCONF_RETRY_S, не больше 3 попыток за запуск."""
        if self._autoconf_done or self._configuring or self._loop is None:
            return
        g = self.gps
        now = time.monotonic()
        receiving = g.last_tpv is not None and g.last_tpv >= g.since and now - g.last_tpv < 2.0
        bps = g.device_bps
        if not receiving or not bps or bps == self.settings.gps.baud or now < self._autoconf_next:
            return
        self._autoconf_attempts += 1
        self._autoconf_next = now + AUTOCONF_RETRY_S
        if self._autoconf_attempts >= 3:
            self._autoconf_done = True
        log.info("GPS at %s bps, expected %s: running UBX configuration", bps, self.settings.gps.baud)
        self._loop.create_task(self._autoconfigure())

    async def _autoconfigure(self) -> None:
        r = await self.gps_configure()
        if r.get("ok"):
            self._autoconf_done = True
        else:
            log.warning("GPS auto-configuration failed: %s", r.get("details", {}).get("error"))

    # ───────────── автокалибровка по GPS-курсу ─────────────
    def effective_calibration(self) -> CompassCalibration:
        if self._cal_auto is not None and self.settings.compass.auto_calibrate:
            return self._cal_auto
        return self.settings.compass.calibration

    def _on_gps_msg(self, msg: dict) -> None:  # event loop (клиент gpsd)
        self.gps.handle(msg)
        if msg.get("class") != "TPV" or not self.settings.compass.auto_calibrate:
            return
        mag = self._mag
        if mag is None or time.monotonic() - mag[0] > MAG_STALE_S:
            return
        fix = self.gps.current()
        self.autocal.on_gps(time.monotonic(), fix, self._declination(fix))

    def _check_axes(self) -> None:
        """Точки копятся в осях корпуса — при смене осей они непригодны."""
        r = self.compass_reader
        spec = r.axes_spec if r is not None else ""
        if not spec:
            return
        if self._autocal_axes is None:
            self._autocal_axes = spec
        elif spec != self._autocal_axes:
            log.info("compass axes changed %s -> %s: auto-calibration points reset", self._autocal_axes, spec)
            self.autocal.reset()
            self._cal_auto = None
            self._autocal_axes = spec

    async def _autocal_loop(self) -> None:
        while True:
            await asyncio.sleep(AUTOCAL_TICK_S)
            try:
                await self.autocal_tick()
            except Exception as e:
                log.exception("compass auto-calibration failed")
                self.autocal.message = f"Auto-calibration error: {e}"

    async def autocal_tick(self, force: bool = False) -> None:
        """Переподогнать (если накопились новые точки), применить/сохранить, периодически записать состояние."""
        self._check_axes()
        c = self.settings.compass
        self.autocal.cfg = c
        now = time.monotonic()
        new = self.autocal.new_points
        if c.auto_calibrate and (force or new >= AUTOCAL_MIN_NEW or (new > 0 and now - self._last_fit > 60)):
            self._last_fit = now
            await asyncio.to_thread(self.autocal.fit, c.mount_offset_deg)
            apply, save = await asyncio.to_thread(
                self.autocal.decide, c.calibration, self.effective_calibration(), c.mount_offset_deg
            )
            if apply is not None:
                self._cal_auto = apply
                log.info("compass auto-calibration applied: rms %.2f deg, coverage %.0f%%, %d points",
                         apply.rms_deg or 0.0, apply.coverage_pct, apply.samples)
            if save is not None:
                for cb in list(self._cal_cbs):
                    try:
                        cb(save)
                    except Exception:
                        log.exception("calibration update callback failed")
        if self.autocal.dirty and (force or time.monotonic() - self._state_saved_at >= STATE_SAVE_S):
            await asyncio.to_thread(self._save_state)

    def _autocal_status(self) -> AutoCalStatus:
        c = self.settings.compass
        compass_ok = self._mag is not None and "compass" not in self._errors
        eff = self.effective_calibration()
        applied = c.auto_calibrate and eff.source == "auto"
        return self.autocal.status(c.auto_calibrate, applied, compass_ok, eff)

    # ───────────── интерфейс SensorHub ─────────────
    def snapshot(self) -> SensorSnapshot:
        return self._snap

    def apply_settings(self, settings: Settings) -> None:
        old = self.settings
        self.settings = settings
        oc, nc = old.compass, settings.compass
        self.autocal.cfg = nc
        if (oc.chip, oc.i2c_bus, oc.address, oc.axes) != (nc.chip, nc.i2c_bus, nc.address, nc.axes) \
                and self.compass_reader is not None:
            self.compass_reader.request_reinit()  # новые оси → _check_axes сбросит точки
        if nc.calibration != oc.calibration:
            if nc.calibration != self._cal_auto:
                self._cal_auto = None  # задана другая калибровка — она главная до следующей подгонки
            if nc.calibration.source == "manual" and nc.calibration.fitted_at != oc.calibration.fitted_at:
                self.autocal.reset()  # новая ручная калибровка (модуль могли переставить) — набираем заново
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
                static_hold_cm_s=g.static_hold_cm_s, static_hold_max_dist_m=g.static_hold_max_dist_m,
                log=lines.append,
            )
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
        finally:
            self._configuring = False
        res["log"] = lines
        self.last_configure = res
        return {"ok": bool(res.get("ok")), "details": res}

    # ручная калибровка
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
        info = self.calib.last_info
        cal = cal.model_copy(update={"source": "manual", "samples": int(info.get("samples", 0)),
                                     "coverage_pct": float(info.get("coverage_pct", 0.0))})
        new = self.settings.model_copy(deep=True)
        new.compass.calibration = cal
        self.apply_settings(new)
        return cal

    def calibration_cancel(self) -> None:
        self.calib.cancel()

    # автокалибровка
    def on_calibration_update(self, callback: Callable[[CompassCalibration], None]) -> None:
        self._cal_cbs.append(callback)

    def auto_calibration_reset(self) -> None:
        self.autocal.reset()
        self._cal_auto = None

    # кнопка / LED / пищалка
    def on_button(self, callback: Callable[[ButtonEvent], None]) -> None:
        self._button_cbs.append(callback)

    def set_led(self, mode: LedMode) -> None:
        self.gpio.set_led(mode)

    def beep(self, pattern: str = "short") -> None:
        self.gpio.beep(pattern)
