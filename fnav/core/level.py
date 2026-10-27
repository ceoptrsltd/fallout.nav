"""Уровень (docs/contracts/level.md): крен и тангаж по MPU-6050 — тот же поток чтения, что у охраны
(fnav/core/imu.py); калибровка «ноль» (single / two_point) и «вперёд» (ось датчика или по разгонам/торможениям);
сколько подложить под колёса.

Оси машины: F — вперёд, R — вправо, U — вверх. Базис из калибровки: U = up, F = forward без составляющей вдоль U,
R = F × U. a — сглаженный (EMA τ = 1 с) вектор акселерометра (в покое ≈ +1 g «вверх»):
pitch = atan2(a·F, hypot(a·R, a·U)) > 0 — перед выше зада; roll = atan2(a·R, hypot(a·F, a·U)) > 0 — правый борт выше.
Калибровка хранится в settings.level.calibration (общий механизм настроек, как калибровка компаса).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import numpy as np

from fnav.core.imu import ImuService
from fnav.models import (
    GpsFix,
    LevelCalibration,
    LevelSettings,
    LevelStatus,
    LevelTelemetry,
    LevelWizard,
    WheelsCm,
)
from fnav.sensors.mpu6050 import Vec

log = logging.getLogger("fnav.core.level")

LP_TAU_S = 1.0
CAPTURE_S = 3.0  # замер «ноля» — среднее за столько
STILL_WINDOW_S = 3.0
STILL_SPREAD_G = 0.01  # разброс |a| за замер
STILL_ANGLE_DEG = 0.3  # и угол от среднего
PARALLEL_COS = 0.9  # ось «вперёд» почти вдоль «вверх» — не годится
LEARN_TICK_S = 0.25
LEARN_WINDOW_S = 1.0  # dv/dt по скорости GPS за это окно
LEARN_MIN_ACCEL = 0.8  # |dv/dt|, м/с²
LEARN_MIN_SPEED = 5 / 3.6  # м/с
LEARN_MAX_TURN_DEG = 5.0  # курс за окно почти не меняется
LEARN_NEED_S = 5.0
LEARN_MAX_SPREAD_DEG = 10.0
LEARN_PARTS_MAX = 4000

AXES: dict[str, tuple[float, float, float]] = {
    "x+": (1.0, 0.0, 0.0), "x-": (-1.0, 0.0, 0.0), "y+": (0.0, 1.0, 0.0),
    "y-": (0.0, -1.0, 0.0), "z+": (0.0, 0.0, 1.0), "z-": (0.0, 0.0, -1.0),
}

NOT_CONNECTED = "Level sensor is not connected"
ROCKING = "The van is moving or rocking. Keep still and try again"
NEED_ZERO = "Calibrate the level first"


class LevelError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ───────────────────────── математика ─────────────────────────


def unit(v: Sequence[float] | np.ndarray) -> np.ndarray:
    a = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(a))
    if n < 1e-9:
        raise ValueError("zero vector")
    return a / n


def nearest_axis(a: Sequence[float] | np.ndarray) -> str:
    """Ось датчика (со знаком), ближайшая к вектору — «вверх» без калибровки."""
    v = np.asarray(a, dtype=float)
    i = int(np.argmax(np.abs(v)))
    return "xyz"[i] + ("+" if v[i] >= 0 else "-")


def default_forward(up: Sequence[float] | np.ndarray) -> str:
    """«Вперёд» по умолчанию — x+; если датчик стоит осью X вверх/вниз — y+."""
    return "x+" if abs(float(unit(up)[0])) < PARALLEL_COS else "y+"


def basis(up: Sequence[float], forward: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(F, R, U) в осях датчика. forward почти вдоль up — ValueError."""
    u = unit(up)
    f = np.asarray(forward, dtype=float)
    fwd = unit(f - float(f @ u) * u)
    return fwd, np.cross(fwd, u), u


def tilt_angles(a: Sequence[float] | np.ndarray, f: np.ndarray, r: np.ndarray, u: np.ndarray) -> tuple[float, float]:
    """(pitch, roll), °."""
    v = np.asarray(a, dtype=float)
    af, ar, au = float(v @ f), float(v @ r), float(v @ u)
    return math.degrees(math.atan2(af, math.hypot(ar, au))), math.degrees(math.atan2(ar, math.hypot(af, au)))


def wheel_raise(pitch_deg: float, roll_deg: float, wheelbase_m: float, track_m: float) -> dict[str, float]:
    """Сколько подложить под каждое колесо, см (шаг 0,5): h = x·tan(pitch) + y·tan(roll), x = ±база/2 (перед +),
    y = ±колея/2 (правый +); raise = max(h) − h — самое высокое колесо 0."""
    tp, tr = math.tan(math.radians(pitch_deg)), math.tan(math.radians(roll_deg))
    x, y = wheelbase_m / 2, track_m / 2
    h = {"fl": x * tp - y * tr, "fr": x * tp + y * tr, "rl": -x * tp - y * tr, "rr": -x * tp + y * tr}
    top = max(h.values())
    return {k: math.floor((top - v) * 100 * 2 + 0.5) / 2 for k, v in h.items()}


def still_check(vectors: Sequence[Vec] | np.ndarray) -> tuple[bool, float, float]:
    """(неподвижно, разброс |a| в g, наибольший угол от среднего в °) по сглаженным векторам."""
    v = np.asarray(vectors, dtype=float)
    if len(v) == 0:
        return False, 0.0, 0.0
    mags = np.linalg.norm(v, axis=1)
    spread = float(mags.max() - mags.min())
    m = unit(v.mean(axis=0))
    ang = float(np.degrees(np.arccos(np.clip((v @ m) / mags, -1.0, 1.0))).max())
    return spread <= STILL_SPREAD_G and ang <= STILL_ANGLE_DEG, spread, ang


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    return math.degrees(math.atan2(float(np.linalg.norm(np.cross(a, b))), float(a @ b)))


class ForwardLearner:
    """«Вперёд» по ходу: на разгонах и торможениях горизонтальная (⊥ U) часть ускорения направлена вдоль машины.
    Копим сумму h·dv/dt (dv/dt — по скорости GPS за окно 1 с; h·dv/dt смотрит вперёд и при разгоне, и при
    торможении). Учитываются отрезки с |dv/dt| ≥ 0,8 м/с², скоростью ≥ 5 км/ч и почти прямым курсом."""

    def __init__(self, up: Sequence[float]) -> None:
        self.u = unit(up)
        self.sum = np.zeros(3)
        self.secs = 0.0
        self._parts: deque[tuple[np.ndarray, float]] = deque(maxlen=LEARN_PARTS_MAX)
        self._hist: deque[tuple[float, float, float | None, np.ndarray]] = deque()
        self._last_t: float | None = None

    def add(self, t: float, speed: float, course: float | None, a: Sequence[float]) -> bool:
        """Очередная точка (раз в LEARN_TICK_S): t — с, speed — м/с по GPS, a — среднее ускорение за шаг, g."""
        step = 0.0 if self._last_t is None else max(0.0, min(t - self._last_t, 1.0))
        self._last_t = t
        self._hist.append((t, speed, course, np.asarray(a, dtype=float)))
        while self._hist and t - self._hist[0][0] > LEARN_WINDOW_S * 1.5:
            self._hist.popleft()
        old = None
        for h in self._hist:
            if t - h[0] >= LEARN_WINDOW_S - 1e-6:
                old = h
        if old is None or step <= 0:
            return False
        t0, v0, c0, _ = old
        if min(speed, v0) < LEARN_MIN_SPEED or course is None or c0 is None:
            return False
        if abs((course - c0 + 180.0) % 360.0 - 180.0) > LEARN_MAX_TURN_DEG:
            return False
        dvdt = (speed - v0) / (t - t0)
        if abs(dvdt) < LEARN_MIN_ACCEL:
            return False
        f = np.mean([h[3] for h in self._hist if h[0] > t0], axis=0)
        horiz = f - float(f @ self.u) * self.u  # тяжесть (вдоль U) уходит вместе с вертикалью
        c = horiz * dvdt
        w = float(np.linalg.norm(c))
        if w < 1e-9:
            return False
        self.sum += c * step
        self.secs += step
        self._parts.append((c / w, w * step))
        return True

    @property
    def spread_deg(self) -> float | None:
        """Разброс направлений отдельных отрезков от общего (взвешенное СКО угла), °."""
        if not self._parts or float(np.linalg.norm(self.sum)) < 1e-12:
            return None
        d = unit(self.sum)
        tw = sum(w for _, w in self._parts)
        return math.sqrt(sum(w * _angle_deg(u, d) ** 2 for u, w in self._parts) / tw) if tw > 0 else None

    @property
    def done(self) -> bool:
        sp = self.spread_deg
        return self.secs >= LEARN_NEED_S and sp is not None and sp <= LEARN_MAX_SPREAD_DEG

    def forward(self) -> np.ndarray:
        return unit(self.sum)


# ───────────────────────── сервис ─────────────────────────


class _Capture:
    """Замер «ноля»: среднее сырых отсчётов + сглаженные векторы (для проверки неподвижности)."""

    def __init__(self, need: int, loop: asyncio.AbstractEventLoop, fut: asyncio.Future) -> None:
        self.need = max(1, need)
        self.loop = loop
        self.fut = fut
        self.sum = [0.0, 0.0, 0.0]
        self.n = 0
        self.smooth: list[Vec] = []

    def add(self, raw: Vec, smooth: Vec) -> None:
        if self.n < self.need:
            for i in range(3):
                self.sum[i] += raw[i]
            self.n += 1
            self.smooth.append(smooth)

    @property
    def done(self) -> bool:
        return self.n >= self.need

    def finish(self) -> None:
        mean = np.array(self.sum) / self.n

        def resolve() -> None:
            if not self.fut.done():
                self.fut.set_result((mean, self.smooth))

        with contextlib.suppress(RuntimeError):  # loop закрыт
            self.loop.call_soon_threadsafe(resolve)


class LevelService:
    def __init__(self, imu: ImuService, get_settings: Callable[[], LevelSettings],
                 save_calibration: Callable[[LevelCalibration | None], Awaitable[Any]],
                 get_fix: Callable[[], GpsFix] | None = None, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.imu = imu
        self.get_settings = get_settings
        self.save_calibration = save_calibration
        self.get_fix = get_fix
        self.clock = clock
        self.mono = mono
        self.capture_s = CAPTURE_S
        self._lock = threading.Lock()
        self._a: list[float] | None = None  # сглаженный вектор, оси датчика
        self._hist: deque[tuple[float, Vec]] = deque()  # (t, сглаженный) по пачкам — «стоит ли»
        self._recent: deque[tuple[float, Vec, int]] = deque()  # (t, среднее пачки, n) — для обучения «вперёд»
        self._capture: _Capture | None = None
        self._step1: np.ndarray | None = None  # two_point: первый замер
        self._learner: ForwardLearner | None = None
        self._task: asyncio.Task | None = None
        imu.subscribe(self._on_samples)

    async def start(self, background: bool = True) -> None:
        if background:
            self._task = asyncio.create_task(self._learn_loop(), name="level-learn")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        cap = self._capture
        if cap is not None and not cap.fut.done():
            cap.fut.cancel()

    # ── поток чтения ──
    def _on_samples(self, samples: list[Vec], dt: float) -> None:
        if not samples:
            return
        k = 1.0 - math.exp(-dt / LP_TAU_S)
        now = self.mono()
        with self._lock:
            a = self._a
            cap = self._capture
            if a is None:  # начало — среднее первой пачки, а не один шумный отсчёт
                n0 = len(samples)
                a = [sum(s[0] for s in samples) / n0, sum(s[1] for s in samples) / n0, sum(s[2] for s in samples) / n0]
            sx = sy = sz = 0.0
            for x, y, z in samples:
                a[0] += k * (x - a[0])
                a[1] += k * (y - a[1])
                a[2] += k * (z - a[2])
                sx, sy, sz = sx + x, sy + y, sz + z
                if cap is not None:
                    cap.add((x, y, z), (a[0], a[1], a[2]))
            self._a = a
            n = len(samples)
            self._hist.append((now, (a[0], a[1], a[2])))
            while now - self._hist[0][0] > STILL_WINDOW_S:
                self._hist.popleft()
            self._recent.append((now, (sx / n, sy / n, sz / n), n))
            while now - self._recent[0][0] > 2.0:
                self._recent.popleft()
            if cap is not None and cap.done:
                self._capture = None
                cap.finish()

    # ── чтение ──
    def _vector(self) -> np.ndarray | None:
        with self._lock:
            return None if self._a is None else np.array(self._a)

    def _still(self) -> bool:
        with self._lock:
            hist = list(self._hist)
        if len(hist) < 2 or hist[-1][0] - hist[0][0] < STILL_WINDOW_S * 0.8:
            return False
        return still_check([v for _, v in hist])[0]

    @staticmethod
    def _basis_for(a: np.ndarray, cal: LevelCalibration | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if cal is not None:
            return basis(cal.up, cal.forward)
        up = AXES[nearest_axis(a)]
        return basis(up, AXES[default_forward(up)])

    def _reading(self) -> tuple[LevelSettings, np.ndarray | None, float | None, float | None, WheelsCm | None]:
        s = self.get_settings()
        a = self._vector() if self.imu.ok else None
        if a is None:
            return s, None, None, None, None
        try:
            p, r = tilt_angles(a, *self._basis_for(a, s.calibration))
        except ValueError:  # вырожденная калибровка
            return s, a, None, None, None
        return s, a, round(p, 1), round(r, 1), WheelsCm(**wheel_raise(p, r, s.wheelbase_m, s.track_m))

    def status(self) -> LevelStatus:
        s, a, pitch, roll, wheels = self._reading()
        lr = self._learner
        spread = lr.spread_deg if lr is not None else None
        return LevelStatus(
            sensor_ok=a is not None, calibrated=s.calibration is not None, pitch_deg=pitch, roll_deg=roll,
            still=a is not None and self._still(), raw=[round(float(v), 4) for v in a] if a is not None else None,
            wheels_cm=wheels, calibration=s.calibration,
            wizard=LevelWizard(two_point_step=1 if self._step1 is not None else 0, learning=lr is not None,
                               learn_s=round(lr.secs, 1) if lr is not None else 0.0,
                               learn_spread_deg=round(spread, 1) if spread is not None else None),
        )

    def telemetry(self) -> LevelTelemetry:
        s, a, pitch, roll, wheels = self._reading()
        return LevelTelemetry(sensor_ok=a is not None, calibrated=s.calibration is not None, pitch_deg=pitch,
                              roll_deg=roll, wheels_cm=wheels)

    # ── калибровка ──
    async def _measure(self) -> np.ndarray:
        """Среднее за capture_s; машина должна стоять неподвижно (иначе 409)."""
        if not self.imu.ok:
            raise LevelError(503, NOT_CONNECTED)
        if self._capture is not None:
            raise LevelError(409, "A measurement is already in progress")
        loop = asyncio.get_running_loop()
        cap = _Capture(round(self.capture_s * self.imu.rate_hz), loop, loop.create_future())
        with self._lock:
            self._capture = cap
        try:
            mean, smooth = await asyncio.wait_for(cap.fut, self.capture_s + 3.0)
        except TimeoutError:
            raise LevelError(503, NOT_CONNECTED) from None
        finally:
            with self._lock:
                if self._capture is cap:
                    self._capture = None
        ok, spread, ang = still_check(smooth)
        if not ok:
            log.info("level: not still (spread %.4f g, angle %.2f°)", spread, ang)
            raise LevelError(409, ROCKING)
        return mean

    def _with_up(self, up: np.ndarray, method: str) -> LevelCalibration:
        """Новый «ноль»; «вперёд» — прежний, если он не вдоль нового «вверх», иначе ось по умолчанию."""
        cur = self.get_settings().calibration
        if cur is not None and abs(float(unit(cur.forward) @ up)) < PARALLEL_COS:
            fwd, src, axis = list(cur.forward), cur.forward_source, cur.axis
        else:
            axis = default_forward(up)
            fwd, src = list(AXES[axis]), "axis"
        return LevelCalibration(up=[round(float(v), 6) for v in up], forward=fwd, method=method,  # type: ignore[arg-type]
                                forward_source=src, axis=axis, ts=round(self.clock(), 3))  # type: ignore[arg-type]

    async def calibrate_level(self, method: str, step: int | None = None) -> LevelStatus:
        """single — машина на ровном месте; two_point — замер 1, разворот на 180° на том же месте, замер 2."""
        if method == "two_point" and step == 2 and self._step1 is None:
            raise LevelError(409, "Take the first measurement (step 1) first")
        a = await self._measure()
        if method == "two_point" and step == 1:
            self._step1 = a
            log.info("level: two-point step 1 done")
            return self.status()
        if method == "two_point":
            assert self._step1 is not None
            up = unit(self._step1 + a)
        else:
            up = unit(a)
        self._step1 = None
        cal = self._with_up(up, method)
        await self.save_calibration(cal)
        log.info("level: zero calibrated (%s), up=%s", method, cal.up)
        return self.status()

    async def set_forward_axis(self, axis: str) -> LevelStatus:
        cal = self.get_settings().calibration
        if cal is None:
            raise LevelError(409, NEED_ZERO)
        if abs(float(np.asarray(AXES[axis]) @ unit(cal.up))) >= PARALLEL_COS:
            raise LevelError(409, f"Axis {axis} points up or down: choose the one that points forward")
        self._learner = None
        await self.save_calibration(LevelCalibration(up=cal.up, forward=list(AXES[axis]), method=cal.method,
                                                     forward_source="axis", axis=axis,  # type: ignore[arg-type]
                                                     ts=round(self.clock(), 3)))
        return self.status()

    async def set_learning(self, on: bool) -> LevelStatus:
        if not on:
            self._learner = None
            return self.status()
        cal = self.get_settings().calibration
        if cal is None:
            raise LevelError(409, NEED_ZERO)
        if not self.imu.ok:
            raise LevelError(503, NOT_CONNECTED)
        self._learner = ForwardLearner(cal.up)
        log.info("level: learning the forward direction while driving")
        return self.status()

    async def reset(self) -> LevelStatus:
        self._step1 = None
        self._learner = None
        await self.save_calibration(None)
        log.info("level: calibration reset")
        return self.status()

    # ── обучение «вперёд» ──
    def _recent_mean(self, now: float, window: float) -> np.ndarray | None:
        with self._lock:
            items = [(v, n) for t, v, n in self._recent if now - t <= window + 1e-6]
        if not items:
            return None
        total = sum(n for _, n in items)
        return np.sum([np.asarray(v) * n for v, n in items], axis=0) / total

    async def learn_tick(self) -> None:
        lr = self._learner
        if lr is None or self.get_fix is None:
            return
        now = self.mono()
        fix = self.get_fix()
        a = self._recent_mean(now, LEARN_TICK_S)
        if a is None or fix.fix == "none" or fix.speed_mps is None:
            return
        lr.add(now, fix.speed_mps, fix.course_deg, a)
        if not lr.done:
            return
        self._learner = None
        cal = self.get_settings().calibration
        if cal is None:
            return
        fwd = lr.forward()
        await self.save_calibration(LevelCalibration(
            up=cal.up, forward=[round(float(v), 6) for v in fwd], method=cal.method, forward_source="driving",
            axis=None, ts=round(self.clock(), 3)))
        log.info("level: forward learned while driving (%.0f s, spread %.1f°)", lr.secs, lr.spread_deg or 0.0)

    async def _learn_loop(self) -> None:
        while True:
            await asyncio.sleep(LEARN_TICK_S)
            try:
                await self.learn_tick()
            except Exception:
                log.exception("level learning")
