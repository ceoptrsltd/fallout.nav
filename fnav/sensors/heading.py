"""Курс: магнитометр → градусы, слияние компас/GPS, сглаживание. Чистые функции + маленький класс состояния.

Соглашение осей «корпуса» (после таблицы осей в compass.py): x — вперёд (стрелка модуля), y — вправо,
z — вниз (FRD). Для горизонтального модуля курс = atan2(-y, x).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def norm360(deg: float) -> float:
    d = math.fmod(deg, 360.0)
    if d < 0:
        d += 360.0
    return 0.0 if d >= 360.0 else d


def angle_diff(a: float, b: float) -> float:
    """a − b в диапазоне (−180, 180]."""
    d = norm360(a - b)
    return d - 360.0 if d > 180.0 else d


def mag_heading_deg(x: float, y: float) -> float | None:
    """Магнитный курс горизонтального модуля по осям FRD (без компенсации наклона)."""
    if x == 0.0 and y == 0.0:
        return None
    return norm360(math.degrees(math.atan2(-y, x)))


def deviation_deg(theta_deg: float, coeffs: list[float] | None) -> float:
    """Остаточная девиация δ(θ) = A + B·sinθ + C·cosθ + D·sin2θ + E·cos2θ (пусто = 0)."""
    if not coeffs:
        return 0.0
    k = list(coeffs) + [0.0] * (5 - len(coeffs))
    t = math.radians(theta_deg)
    return k[0] + k[1] * math.sin(t) + k[2] * math.cos(t) + k[3] * math.sin(2 * t) + k[4] * math.cos(2 * t)


def true_mag_heading(magnetic_deg: float | None, declination_deg: float, mount_offset_deg: float,
                     deviation: list[float] | None = None) -> float | None:
    """Магнитный курс θ → от истинного севера: θ + склонение (восточное +) + δ(θ) + поправка установки."""
    if magnetic_deg is None:
        return None
    return norm360(magnetic_deg + declination_deg + deviation_deg(magnetic_deg, deviation) + mount_offset_deg)


def gps_weight(speed_mps: float | None, gps_above_mps: float, blend_band_mps: float) -> float:
    """Вес GPS-курса 0..1: ниже (порог − band/2) — 0, выше (порог + band/2) — 1, между — smoothstep."""
    if speed_mps is None:
        return 0.0
    lo = gps_above_mps - blend_band_mps / 2
    hi = gps_above_mps + blend_band_mps / 2
    if hi <= lo:
        return 1.0 if speed_mps >= gps_above_mps else 0.0
    if speed_mps <= lo:
        return 0.0
    if speed_mps >= hi:
        return 1.0
    u = (speed_mps - lo) / (hi - lo)
    return u * u * (3 - 2 * u)


def blend_angles(a_deg: float, b_deg: float, w_b: float) -> float:
    """Круговое смешивание через единичные векторы (без скачка 359→0)."""
    ax, ay = math.cos(math.radians(a_deg)), math.sin(math.radians(a_deg))
    bx, by = math.cos(math.radians(b_deg)), math.sin(math.radians(b_deg))
    x = ax * (1 - w_b) + bx * w_b
    y = ay * (1 - w_b) + by * w_b
    if abs(x) < 1e-12 and abs(y) < 1e-12:  # противоположные направления 50/50 — берём более весомое
        return b_deg if w_b >= 0.5 else a_deg
    return norm360(math.degrees(math.atan2(y, x)))


# минимальная скорость, при которой GPS-курс вообще осмыслен (если компаса нет)
GPS_ONLY_MIN_MPS = 1.0


def fuse(
    mag_deg: float | None,
    gps_deg: float | None,
    speed_mps: float | None,
    gps_above_mps: float,
    blend_band_mps: float,
) -> tuple[float | None, str]:
    """Мгновенный (несглаженный) курс и его источник: gps | mag | blend | none."""
    w = gps_weight(speed_mps, gps_above_mps, blend_band_mps)
    if gps_deg is None:
        w = 0.0
    if mag_deg is None:
        # компаса нет: GPS-курс, если хоть немного едем
        if gps_deg is not None and (speed_mps or 0.0) >= min(GPS_ONLY_MIN_MPS, gps_above_mps):
            return norm360(gps_deg), "gps"
        return None, "none"
    if w <= 0.0:
        return norm360(mag_deg), "mag"
    if w >= 1.0:
        return norm360(gps_deg), "gps"  # type: ignore[arg-type]
    return blend_angles(mag_deg, gps_deg, w), "blend"  # type: ignore[arg-type]


@dataclass
class HeadingSmoother:
    """Экспоненциальное сглаживание направления (вектором) с постоянной времени tau, с.

    update(None, dt) — удержание последнего значения (машина стоит, компаса нет)."""

    tau_s: float = 0.5
    x: float | None = None
    y: float | None = None

    def reset(self) -> None:
        self.x = self.y = None

    @property
    def value(self) -> float | None:
        if self.x is None or self.y is None or (abs(self.x) < 1e-12 and abs(self.y) < 1e-12):
            return None
        return norm360(math.degrees(math.atan2(self.y, self.x)))

    def update(self, deg: float | None, dt: float) -> float | None:
        if deg is None:
            return self.value
        tx, ty = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        if self.x is None or self.y is None or self.tau_s <= 0:
            self.x, self.y = tx, ty
            return self.value
        alpha = 1.0 - math.exp(-max(0.0, dt) / self.tau_s)
        self.x += (tx - self.x) * alpha
        self.y += (ty - self.y) * alpha
        n = math.hypot(self.x, self.y)
        if n > 1e-9:
            self.x, self.y = self.x / n, self.y / n
        else:  # ровно противоположное — прыгаем к новому
            self.x, self.y = tx, ty
        return self.value
