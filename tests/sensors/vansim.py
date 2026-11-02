"""Синтетический фургон для тестов курса: истинный курс, скорость (< 0 — задний ход), уклон → компас (FRD, hard iron),
TPV 10 Гц, MPU-6050 пачками (200 Гц: акселерометр и гироскоп со смещением нуля). Время — часы хаба."""

from __future__ import annotations

import math
import random

from fnav.models import CompassCalibration, LevelCalibration, LevelRef, Settings
from fnav.sensors.declination import field_nT
from fnav.sensors.hub import RealSensorHub

LAT, LON = 52.2, 21.0
_X, _Y, _Z = field_nT(LAT, LON)
H_UT, Z_UT = math.hypot(_X, _Y) / 1000, _Z / 1000  # поле в Польше по WMM: горизонтальная (~19) и вниз (~47), мкТл
HARD = (9.0, -2.0, 6.0)  # hard iron, мкТл
G = 9.80665


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeImu:
    def __init__(self) -> None:
        self.cbs: list = []

    def subscribe_motion(self, cb) -> None:
        self.cbs.append(cb)


def rot(psi: float, pitch: float, roll_aero: float, v: tuple[float, float, float]) -> tuple[float, float, float]:
    """Вектор NED → оси FRD машины (рысканье psi, тангаж > 0 — нос вверх, крен > 0 — правый борт вниз), °."""
    p, t, f = math.radians(psi), math.radians(pitch), math.radians(roll_aero)
    x, y, z = v
    x, y = math.cos(p) * x + math.sin(p) * y, -math.sin(p) * x + math.cos(p) * y
    x, z = math.cos(t) * x - math.sin(t) * z, math.sin(t) * x + math.cos(t) * z
    y, z = math.cos(f) * y + math.sin(f) * z, -math.sin(f) * y + math.cos(f) * z
    return x, y, z


def level_settings(cal: CompassCalibration | None = None, level: bool = True) -> Settings:
    st = Settings()
    st.compass.declination = 0.0
    st.compass.calibration = cal if cal is not None else CompassCalibration(
        offset=list(HARD), source="manual", fitted_at="2026-11-01T10:00:00+00:00")
    if level:  # датчик лежит платой вверх, X вперёд (y — влево, z — вверх); эталон снят на ровном месте
        st.level.calibration = LevelCalibration(up=[0.0, 0.0, 1.0], forward=[1.0, 0.0, 0.0],
                                                ref=LevelRef(a=[0.0, 0.0, 1.0], pitch_deg=0.0, roll_deg=0.0))
    return st


class Van:
    def __init__(self, st: Settings | None = None, *, imu: bool = True, psi: float = 0.0, seed: int = 1,
                 gyro_bias: tuple[float, float, float] = (0.6, -0.4, 0.8), hub: RealSensorHub | None = None) -> None:
        self.clock = Clock()
        self.hub = hub or RealSensorHub(st or level_settings(), gpsd_port=1, enable_compass=False, enable_gpio=False,
                                        clock=self.clock)
        if hub is not None:
            self.clock = hub._clock
        self.imu = imu
        if imu:
            self.hub.attach_imu(FakeImu())
        self.psi = psi
        self.v = 0.0  # м/с, < 0 — назад
        self.pitch = 0.0  # нос вверх > 0
        self.roll = 0.0  # правый борт выше > 0 (как у уровня)
        self.gyro_bias = gyro_bias
        self.rng = random.Random(seed)
        self.mag_dist = 0.0  # помеха компасу (поворот поля), °
        self.course_noise = 0.5
        self.gps = True
        self.tpv_age = 0.0  # задержка доставки TPV (для проверки «свежести»)
        self.extra_tpv: dict = {}
        self.course_override: float | None = None
        self.speed_override: float | None = None
        self._next_tpv = self.clock.t
        self._accel = 0.0
        self.lat, self.lon = LAT, LON
        self.module_rot = 0.0  # модуль компаса повёрнут относительно машины по часовой, °

    # ── датчики ──
    def mag_raw(self) -> tuple[float, float, float]:
        b = rot(self.psi + self.mag_dist, self.pitch, -self.roll, (H_UT, 0.0, Z_UT))
        if self.module_rot:  # FRD машины → FRD модуля (поворот вокруг «вниз»)
            r = math.radians(self.module_rot)
            b = (math.cos(r) * b[0] + math.sin(r) * b[1], -math.sin(r) * b[0] + math.cos(r) * b[1], b[2])
        return (b[0] + HARD[0], b[1] + HARD[1], b[2] + HARD[2])

    def accel_sensor(self, rate_dps: float) -> tuple[float, float, float]:
        u = rot(self.psi, self.pitch, -self.roll, (0.0, 0.0, -1.0))  # «вверх» в FRD
        af, ar, au = u[0], u[1], -u[2]
        af += self._accel / G
        ar += self.v * math.radians(rate_dps) / G
        return (af, -ar, au)  # оси датчика: x = F, y = −R, z = U

    def step(self, dt: float = 0.05, rate_dps: float = 0.0, accel: float = 0.0, fuse: bool = True) -> None:
        self.clock.t += dt
        self._accel = accel
        self.psi = (self.psi + rate_dps * dt) % 360.0
        self.v += accel * dt
        hub = self.hub
        hub._on_mag(self.mag_raw(), "ist8310")
        if self.imu:
            n = round(dt * 200)
            a = self.accel_sensor(rate_dps)
            bx, by, bz = self.gyro_bias
            acc = [tuple(x + self.rng.gauss(0, 0.002) for x in a) for _ in range(n)]
            gyr = [(bx + self.rng.gauss(0, 0.05), by + self.rng.gauss(0, 0.05), bz - rate_dps + self.rng.gauss(0, 0.05))
                   for _ in range(n)]
            hub._on_motion(acc, gyr, 1.0 / 200)
        if self.gps and self.clock.t >= self._next_tpv:
            self._next_tpv += 0.1
            spd = abs(self.v) if self.speed_override is None else self.speed_override
            course = self.psi if self.v >= 0 else self.psi + 180.0
            if self.course_override is not None:
                course = self.course_override
            msg = {"class": "TPV", "mode": 3, "lat": self.lat, "lon": self.lon, "speed": spd,
                   "track": (course + self.rng.gauss(0, self.course_noise)) % 360.0, **self.extra_tpv}
            real = self.clock.t
            self.clock.t -= self.tpv_age
            hub.gps.handle(msg)
            self.clock.t = real
        if fuse:
            hub._fuse_once()

    def run(self, seconds: float, **kw) -> list:
        out = []
        for _ in range(round(seconds / 0.05)):
            self.step(**kw)
            out.append(self.hub.snapshot())
        return out

    def park(self, seconds: float) -> list:
        self.v = 0.0
        return self.run(seconds)
