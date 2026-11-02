"""Движение машины по MPU-6050 для курса (fnav/sensors/hub.py): гироскоп → скорость рысканья с оценкой смещения нуля
на стоянке, наклон в осях машины (компенсация наклона компаса), продольное ускорение с трогания (признак заднего хода).

Оси машины — как у уровня (docs/contracts/level.md): F — вперёд, R — вправо, U — вверх; U и F — из калибровки уровня
(settings.level.calibration). Без неё U — по тяжести (среднее ускорение тихих пачек), F неизвестно: наклон и
продольное ускорение не считаются. Гироскоп — °/с в осях датчика (правая тройка, как у акселерометра).
Скорость рысканья (курса) = −(ω − смещение)·U: > 0 — поворот вправо, по часовой (курс растёт).

Смещение нуля гироскопа — среднее за окно неподвижности (гироскоп и акселерометр тихие), только пока GPS говорит
«стоим» (set_moving(False)): на плавной дуге в тоннеле гироскоп тоже «тихий», но поворачивает. Сохранённое смещение
(set_bias, sensors_state.json) — до первой стоянки после запуска.

add() — из потока чтения датчика (fnav/core/imu.py subscribe_motion, пачки ~20 раз в секунду); read() и set_*() —
из цикла слияния.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fnav.models import LevelCalibration
from fnav.sensors.mpu6050 import Vec

G = 9.80665
LP_TAU_S = 1.0  # наклон: сглаживание акселерометра (как у уровня)
GRAV_TAU_S = 5.0  # «вверх» без калибровки уровня: по тихим пачкам (|a| ≈ 1 g)
STILL_WINDOW_S = 1.0  # неподвижность: за это окно средние по пачкам почти не меняются
STILL_GYRO_DPS = 0.3  # разброс средних по пачкам за окно, °/с (по каждой оси)
STILL_ACC_G = 0.006  # и ускорения, g
STILL_BIAS_DPS = 1.5  # и не дальше этого от известного смещения нуля (медленный разворот — не стоянка)
FIRST_BIAS_MAX_DPS = 5.0  # первое смещение (ещё не знаем) — только если гироскоп показывает меньше этого
FIRST_BIAS_STILL_S = 2.0  # и неподвижно столько
BIAS_TAU_S = 10.0  # уточнение смещения на стоянке
LONG_TAU_S = 3.0  # продольная «скорость» по акселерометру — интеграл с утечкой
LONG_BASE_TAU_S = 20.0  # продольная составляющая тяжести (уклон), пока стоим
STALE_S = 0.5  # пачек нет дольше — гироскопа «нет»
ZERO_GYRO_S = 2.0  # гироскоп отдаёт ровно нули столько — его нет (клон без гироскопа)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v: Sequence[float]) -> tuple[float, float, float] | None:
    n = math.sqrt(_dot(v, v))
    return None if n < 1e-9 else (v[0] / n, v[1] / n, v[2] / n)


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _mean(vs: Sequence[Vec]) -> tuple[float, float, float]:
    n = len(vs)
    return (sum(v[0] for v in vs) / n, sum(v[1] for v in vs) / n, sum(v[2] for v in vs) / n)


def level_basis(cal: LevelCalibration | None) -> tuple[Vec, Vec, Vec] | None:
    """(F, R, U) в осях датчика по калибровке уровня (level.md: U = up, F = forward ⊥ U, R = F × U); нет — None."""
    if cal is None:
        return None
    u = _unit(cal.up)
    if u is None:
        return None
    f0 = cal.forward
    k = _dot(f0, u)
    f = _unit((f0[0] - k * u[0], f0[1] - k * u[1], f0[2] - k * u[2]))
    if f is None:
        return None
    return f, _cross(f, u), u


@dataclass(frozen=True)
class MotionState:
    t: float | None = None  # момент последней пачки (часы трекера)
    fresh: bool = False  # пачки идут (датчик отвечает)
    gyro_ok: bool = False  # гироскоп есть, смещение нуля известно, пачки свежие
    yaw_deg: float = 0.0  # накопленный поворот вправо (с поправкой на смещение), °
    rate_dps: float | None = None  # скорость рысканья по последней пачке, °/с (> 0 — вправо)
    still_s: float = 0.0  # датчик неподвижен столько секунд подряд (0 — движется)
    acc_v: tuple[float, float, float] | None = None  # сглаженное ускорение (aF, aR, aU), g — с калибровкой уровня
    tilt_ok: bool = False  # калибровка уровня с эталоном: acc_v — настоящая вертикаль
    v_long: float | None = None  # продольная «скорость» с трогания по акселерометру, м/с (> 0 — вперёд)
    bias: Vec | None = None  # смещение нуля, °/с, оси датчика
    bias_src: str | None = None  # "still" — на этой стоянке, "saved" — из файла


class MotionTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._basis: tuple[Vec, Vec, Vec] | None = None
        self._tilt_ok = False
        self._moving: bool | None = None  # по GPS: едем / стоим / неизвестно
        self._bias: Vec | None = None
        self._bias_src: str | None = None
        self._yaw = 0.0
        self._rate: float | None = None
        self._t: float | None = None
        self._win: deque[tuple[float, Vec, Vec]] = deque()  # (t, средний гироскоп, среднее ускорение) по пачкам
        self._still_since: float | None = None
        self._grav: list[float] | None = None
        self._acc_lp: list[float] | None = None
        self._base_f: float | None = None
        self._v_long = 0.0
        self._zero_since: float | None = None
        self.batches = 0

    # ── из цикла слияния ──
    def set_level(self, cal: LevelCalibration | None) -> None:
        basis = level_basis(cal)
        with self._lock:
            if basis != self._basis:
                self._base_f, self._v_long = None, 0.0  # другая ось «вперёд» — прежняя база не годится
            self._basis = basis
            # эталон (или «ноль» прежней версии) — U настоящая вертикаль машины; одно «вперёд» без эталона — нет
            self._tilt_ok = basis is not None and cal is not None and (cal.ref is not None or cal.method != "reference")

    def set_moving(self, moving: bool | None) -> None:
        with self._lock:
            self._moving = moving

    def set_bias(self, bias: Sequence[float], source: str = "saved") -> None:
        with self._lock:
            if self._bias_src != "still" or source == "still":
                self._bias = (float(bias[0]), float(bias[1]), float(bias[2]))
                self._bias_src = source

    def read(self) -> MotionState:
        now = self.clock()
        with self._lock:
            fresh = self._t is not None and now - self._t <= STALE_S
            zero = self._zero_since is not None and (self._t or now) - self._zero_since >= ZERO_GYRO_S
            acc_v = None
            if self._basis is not None and self._acc_lp is not None:
                f, r, u = self._basis
                a = self._acc_lp
                acc_v = (_dot(a, f), _dot(a, r), _dot(a, u))
            still = 0.0
            if fresh and self._still_since is not None and self._t is not None:
                still = self._t - self._still_since
            return MotionState(
                t=self._t, fresh=fresh, gyro_ok=fresh and self._bias is not None and not zero, yaw_deg=self._yaw,
                rate_dps=self._rate if fresh and not zero else None, still_s=still,
                acc_v=acc_v if fresh else None, tilt_ok=self._tilt_ok,
                v_long=self._v_long if fresh and self._base_f is not None else None,
                bias=self._bias, bias_src=self._bias_src)

    # ── из потока чтения датчика ──
    def add(self, acc: Sequence[Vec], gyr: Sequence[Vec], dt: float) -> None:
        n = min(len(acc), len(gyr))
        if n == 0:
            return
        span = n * dt
        gm, am = _mean(gyr[:n]), _mean(acc[:n])
        with self._lock:
            t = self.clock()
            self.batches += 1
            self._t = t
            if any(v != (0.0, 0.0, 0.0) for v in gyr[:n]):
                self._zero_since = None
            elif self._zero_since is None:
                self._zero_since = t
            # тяжесть («вверх» без калибровки) — по тихим пачкам
            amag = math.sqrt(_dot(am, am))
            if 0.9 <= amag <= 1.1:
                if self._grav is None:
                    self._grav = list(am)
                else:
                    k = 1.0 - math.exp(-span / GRAV_TAU_S)
                    for i in range(3):
                        self._grav[i] += k * (am[i] - self._grav[i])
            # наклон: сглаженный вектор
            if self._acc_lp is None:
                self._acc_lp = list(am)
            else:
                k = 1.0 - math.exp(-span / LP_TAU_S)
                for i in range(3):
                    self._acc_lp[i] += k * (am[i] - self._acc_lp[i])
            # неподвижность за окно
            self._win.append((t, gm, am))
            while t - self._win[0][0] > STILL_WINDOW_S:
                self._win.popleft()
            still = self._still_now(t, gm)
            if still:
                if self._still_since is None:
                    self._still_since = t - min(t - self._win[0][0], STILL_WINDOW_S)
            else:
                self._still_since = None
            if still and self._moving is False:
                self._learn_bias(t, span)
            # рысканье
            u = self._basis[2] if self._basis is not None else (_unit(self._grav) if self._grav else _unit(am))
            if self._bias is not None and u is not None:
                b = self._bias
                rate = -((gm[0] - b[0]) * u[0] + (gm[1] - b[1]) * u[1] + (gm[2] - b[2]) * u[2])
                self._yaw += rate * span
                self._rate = rate
            else:
                self._rate = None
            # продольная «скорость» с трогания
            if self._basis is not None:
                af = _dot(am, self._basis[0])
                if self._base_f is None:
                    self._base_f = af
                elif self._moving is False:
                    self._base_f += (1.0 - math.exp(-span / LONG_BASE_TAU_S)) * (af - self._base_f)
                if still and self._moving is False:
                    self._v_long = 0.0
                else:
                    self._v_long = self._v_long * math.exp(-span / LONG_TAU_S) + (af - self._base_f) * G * span

    def _still_now(self, t: float, gm: Vec) -> bool:
        w = self._win
        if len(w) < 3 or t - w[0][0] < STILL_WINDOW_S * 0.8:
            return False
        for i in range(3):
            g = [e[1][i] for e in w]
            a = [e[2][i] for e in w]
            if max(g) - min(g) > STILL_GYRO_DPS or max(a) - min(a) > STILL_ACC_G:
                return False
        b = self._bias
        if b is None or self._bias_src != "still":
            return True
        return max(abs(gm[i] - b[i]) for i in range(3)) <= STILL_BIAS_DPS

    def _learn_bias(self, t: float, span: float) -> None:
        wm = _mean([e[1] for e in self._win])
        if self._bias_src != "still":  # первое на этой стоянке после запуска (сохранённое могло уйти с температурой)
            if self._still_since is None or t - self._still_since < FIRST_BIAS_STILL_S \
                    or max(abs(v) for v in wm) > FIRST_BIAS_MAX_DPS:
                return
            self._bias, self._bias_src = wm, "still"
            return
        b = self._bias
        assert b is not None
        k = 1.0 - math.exp(-span / BIAS_TAU_S)
        self._bias = (b[0] + k * (wm[0] - b[0]), b[1] + k * (wm[1] - b[1]), b[2] + k * (wm[2] - b[2]))
