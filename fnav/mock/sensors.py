"""Имитация датчиков для разработки на ПК (владелец: ведущий).

Машина едет по синтетическому маршруту туда-обратно (Ленинский проспект, Москва — координаты
приблизительные). Скоростной профиль циклически проходит все режимы экрана:
стоянка → разгон → 5 / 12 / 20 / 28 м/с → торможение → стоянка (камера) → …

    hub = MockSensorHub(Settings())
    await hub.start()
    hub.snapshot()  # SensorSnapshot

Переменная окружения FNAV_MOCK_SPEEDUP=4 ускоряет время (для быстрых тестов).
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Callable

from fnav.interfaces import ButtonEvent, LedMode
from fnav.models import (
    CalibrationStatus,
    CompassCalibration,
    CompassRaw,
    GpsFix,
    Heading,
    AutoCalStatus,
    Satellite,
    SensorSnapshot,
    Settings,
)

# (lat, lon) — туда; обратно тем же путём
_ROUTE_OUT = [
    (55.70690, 37.58530),
    (55.70210, 37.57830),
    (55.69650, 37.57040),
    (55.69300, 37.56420),
    (55.68930, 37.55900),
    (55.68570, 37.55300),
    (55.68100, 37.54620),
    (55.67700, 37.54010),
]
ROUTE = _ROUTE_OUT + _ROUTE_OUT[-2::-1]

# (длительность, с; целевая скорость, м/с) — по кругу
SPEED_PROFILE = [
    (12, 0.0),
    (15, 5.0),
    (20, 12.0),
    (25, 20.0),
    (20, 28.0),
    (15, 12.0),
    (8, 1.0),
    (10, 0.0),
    (20, 8.0),
]

_EARTH_R = 6_371_000.0


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(h))


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _lerp(a: tuple[float, float], b: tuple[float, float], f: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


class _Route:
    def __init__(self, pts: list[tuple[float, float]]):
        self.pts = pts
        self.seg_len = [_haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        self.total = sum(self.seg_len)

    def at(self, dist: float) -> tuple[tuple[float, float], float]:
        d = dist % self.total
        for i, sl in enumerate(self.seg_len):
            if d <= sl:
                return _lerp(self.pts[i], self.pts[i + 1], d / sl if sl else 0), _bearing(
                    self.pts[i], self.pts[i + 1]
                )
            d -= sl
        return self.pts[-1], _bearing(self.pts[-2], self.pts[-1])


def _target_speed(t: float) -> float:
    cycle = sum(d for d, _ in SPEED_PROFILE)
    t %= cycle
    for dur, v in SPEED_PROFILE:
        if t < dur:
            return v
        t -= dur
    return 0.0


class MockSensorHub:
    """Реализует fnav.interfaces.SensorHub."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._route = _Route(ROUTE)
        self._speedup = float(os.environ.get("FNAV_MOCK_SPEEDUP", "1"))
        self._task: asyncio.Task | None = None
        self._dist = 0.0
        self._speed = 0.0
        self._sim_t = 0.0
        self._heading = _bearing(ROUTE[0], ROUTE[1])
        self._snap = SensorSnapshot()
        self._nmea: list[str] = []
        self._button_cbs: list[Callable[[ButtonEvent], None]] = []
        self._led: LedMode = "off"
        self._calib_active = False
        self._calib_samples = 0
        self._auto_samples = 0
        self._rng = random.Random(42)
        self._sats = [
            Satellite(gnss=g, prn=p, el=self._rng.uniform(10, 85), az=self._rng.uniform(0, 360))
            for g, p in [("gps", n) for n in (2, 5, 7, 9, 13, 15, 18, 20, 29, 30)]
            + [("glonass", n) for n in (65, 66, 72, 76, 81)]
            + [("galileo", n) for n in (3, 8, 13, 26)]
        ]
        self._step(0.0)

    # ── жизненный цикл ──
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            self._step((now - last) * self._speedup)
            last = now

    # ── симуляция ──
    def _step(self, dt: float) -> None:
        self._sim_t += dt
        target = _target_speed(self._sim_t)
        accel = 2.5 if target > self._speed else 4.0
        if self._speed < target:
            self._speed = min(target, self._speed + accel * dt)
        else:
            self._speed = max(target, self._speed - accel * dt)
        self._dist += self._speed * dt
        (lat, lon), course = self._route.at(self._dist)

        # плавный поворот «машины» к курсу участка
        diff = (course - self._heading + 540) % 360 - 180
        self._heading = (self._heading + diff * min(1.0, dt * 1.5)) % 360

        r = self._rng
        speed_meas = max(0.0, self._speed + r.gauss(0, 0.08))
        gps_course = (self._heading + r.gauss(0, 0.7)) % 360 if speed_meas > 0.5 else None
        mag = (self._heading + 4.0 + r.gauss(0, 2.5)) % 360  # компас «врёт» на 4° и шумит

        hs = self.settings.heading
        if speed_meas >= hs.gps_above_mps + hs.blend_band_mps / 2 and gps_course is not None:
            heading, source = gps_course, "gps"
        elif speed_meas <= hs.gps_above_mps - hs.blend_band_mps / 2 or gps_course is None:
            heading, source = mag, "mag"
        else:
            heading, source = gps_course, "blend"

        for s in self._sats:
            s.snr = max(0.0, min(50.0, 25 + (s.el or 0) / 4 + r.gauss(0, 2)))
            s.used = s.snr > 28
        used = sum(1 for s in self._sats if s.used)

        if speed_meas > self.settings.compass.auto_min_speed_mps:
            self._auto_samples += 1
        cov = min(100.0, self._auto_samples / 20)
        auto = AutoCalStatus(
            enabled=self.settings.compass.auto_calibrate,
            state="fitted" if cov >= self.settings.compass.auto_min_coverage_pct else "collecting",
            samples=self._auto_samples, coverage_pct=round(cov, 1),
            rms_deg=2.1 if cov >= self.settings.compass.auto_min_coverage_pct else None,
            applied=cov >= self.settings.compass.auto_min_coverage_pct,
            message="mock auto-calibration",
        )
        now = datetime.now(timezone.utc)
        self._snap = SensorSnapshot(
            compass_cal=auto,
            gps=GpsFix(
                fix="3d",
                lat=lat + r.gauss(0, 2e-6),
                lon=lon + r.gauss(0, 2e-6),
                alt_m=165.0 + r.gauss(0, 0.5),
                speed_mps=round(speed_meas, 2),
                course_deg=None if gps_course is None else round(gps_course, 1),
                hdop=round(0.8 + r.random() * 0.3, 2),
                sats_used=used,
                sats_visible=len(self._sats),
                time_utc=now.isoformat(timespec="milliseconds"),
                age_s=0.05,
            ),
            heading=Heading(
                deg=round(heading, 1),
                source=source,
                mag_deg=round(mag, 1),
                gps_course_deg=None if gps_course is None else round(gps_course, 1),
                declination_deg=11.5,
            ),
            compass=CompassRaw(
                x=round(30 * math.cos(math.radians(mag)), 2),
                y=round(-30 * math.sin(math.radians(mag)), 2),
                z=-45.0,
                chip="mock",
            ),
            satellites=[s.model_copy() for s in self._sats],
        )
        if self._calib_active:
            self._calib_samples += 1

        hhmmss = now.strftime("%H%M%S.%f")[:9]
        self._nmea.append(
            f"$GNRMC,{hhmmss},A,{abs(lat) * 100:.5f},N,{abs(lon) * 100:.5f},E,"
            f"{speed_meas * 1.943844:.2f},{(gps_course or 0):.1f},{now:%d%m%y},,,A*00"
        )
        del self._nmea[:-500]

    # ── интерфейс SensorHub ──
    def snapshot(self) -> SensorSnapshot:
        return self._snap

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings

    def nmea_tail(self, n: int = 50) -> list[str]:
        return self._nmea[-n:]

    async def gps_configure(self) -> dict:
        await asyncio.sleep(0.2)
        return {"ok": True, "details": "mock: nothing to configure"}

    def calibration_start(self) -> None:
        self._calib_active, self._calib_samples = True, 0

    def calibration_status(self) -> CalibrationStatus:
        cov = min(100.0, self._calib_samples / 3)
        return CalibrationStatus(
            active=self._calib_active,
            samples=self._calib_samples,
            coverage_pct=round(cov, 1),
            message="mock: rotate the module" if self._calib_active else "",
        )

    def calibration_finish(self) -> CompassCalibration:
        self._calib_active = False
        return CompassCalibration(
            offset=[1.2, -3.4, 0.5],
            fitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            quality=0.93,
        )

    def calibration_cancel(self) -> None:
        self._calib_active = False

    def on_calibration_update(self, callback: Callable[[CompassCalibration], None]) -> None:
        self._cal_cbs = getattr(self, "_cal_cbs", []) + [callback]

    def auto_calibration_reset(self) -> None:
        self._auto_samples = 0

    def on_button(self, callback: Callable[[ButtonEvent], None]) -> None:
        self._button_cbs.append(callback)

    def mock_press(self, event: ButtonEvent = "press") -> None:
        """Только в моке: имитировать нажатие кнопки на модуле."""
        for cb in self._button_cbs:
            cb(event)

    def set_led(self, mode: LedMode) -> None:
        self._led = mode

    @property
    def led(self) -> LedMode:
        return self._led

    def beep(self, pattern: str = "short") -> None:
        pass
