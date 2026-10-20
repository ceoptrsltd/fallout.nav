"""Калибровка магнитометра (hard iron + soft iron): сбор точек, покрытие по азимуту, подгонка эллипсоида.

Модель: исправленное = M · (raw − offset). Точки — в осях корпуса FRD (после таблицы осей), мкТл.

* 3D: МНК-подгонка общего эллипсоида  x'Ax + 2b'x = 1 (9 параметров) → центр, M = R·sqrt(A/k).
* 2D («машинный» случай: вращаем только по курсу, вертикального разброса почти нет): эллипс в плоскости XY,
  ось Z не трогаем (offset_z = 0, масштаб 1) — для курса она не нужна.

Качество 0..1 = (1 − разброс радиусов после коррекции / 15 %) × покрытие направлений (норма — 80 %).
"""

from __future__ import annotations

import math
import threading
from datetime import UTC, datetime

import numpy as np

from fnav.models import CalibrationStatus, CompassCalibration

SECTORS = 36  # по 10°
MIN_PER_SECTOR = 2
MIN_SAMPLES = 40
FLAT_RATIO = 0.25  # σz / средний горизонтальный радиус ниже этого → 2D


class CalibrationError(ValueError):
    pass


def apply_calibration(v: tuple[float, float, float] | np.ndarray, cal: CompassCalibration) -> tuple[float, ...]:
    m = cal.matrix
    o = cal.offset
    x, y, z = v[0] - o[0], v[1] - o[1], v[2] - o[2]
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z,
        m[1][0] * x + m[1][1] * y + m[1][2] * z,
        m[2][0] * x + m[2][1] * y + m[2][2] * z,
    )


def _sqrtm_spd(m: np.ndarray) -> np.ndarray:
    w, v = np.linalg.eigh((m + m.T) / 2)
    if np.any(w <= 0):
        raise CalibrationError("fit is not an ellipsoid (too few distinct directions)")
    return (v * np.sqrt(w)) @ v.T


def _rough_center(p: np.ndarray) -> np.ndarray:
    return (p.min(axis=0) + p.max(axis=0)) / 2


def center_estimate(p: np.ndarray) -> np.ndarray:
    """Центр облака точек: по подгонке эллипса в XY (устойчиво к дугам и одиночным выбросам), иначе min/max."""
    rough = _rough_center(p)
    if len(p) >= 10:
        try:
            c2, _ = fit_ellipse_2d(p[:, :2])
            span = float(np.max(np.ptp(p[:, :2], axis=0)))
            if np.all(np.isfinite(c2)) and np.linalg.norm(c2 - rough[:2]) < 3 * span + 1e-9:
                return np.array([c2[0], c2[1], rough[2]])
        except (CalibrationError, np.linalg.LinAlgError, ValueError):
            pass
    return rough


def sector_counts(points: np.ndarray, center: np.ndarray | None = None, sectors: int = SECTORS) -> np.ndarray:
    if len(points) == 0:
        return np.zeros(sectors, dtype=int)
    c = center_estimate(points) if center is None else center
    d = points - c
    az = (np.degrees(np.arctan2(-d[:, 1], d[:, 0])) + 360.0) % 360.0
    idx = np.minimum((az / (360.0 / sectors)).astype(int), sectors - 1)
    return np.bincount(idx, minlength=sectors)


def coverage_pct(points: np.ndarray, center: np.ndarray | None = None) -> float:
    cnt = sector_counts(points, center)
    return 100.0 * float(np.count_nonzero(cnt >= MIN_PER_SECTOR)) / len(cnt)


def fit_ellipse_2d(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Эллипс a x² + b xy + c y² + d x + e y = 1 → (центр(2), W(2×2)) так, что |W(p−c)| ≈ R."""
    x, y = p[:, 0], p[:, 1]
    # центрирование для численной устойчивости
    c0 = np.array([x.mean(), y.mean()])
    x, y = x - c0[0], y - c0[1]
    D = np.column_stack([x * x, x * y, y * y, x, y])
    v, *_ = np.linalg.lstsq(D, np.ones(len(x)), rcond=None)
    a, b, c, d, e = v
    A = np.array([[a, b / 2], [b / 2, c]])
    bv = np.array([d / 2, e / 2])
    try:
        ctr = -np.linalg.solve(A, bv)
    except np.linalg.LinAlgError as ex:
        raise CalibrationError("degenerate ellipse") from ex
    k = 1.0 + ctr @ A @ ctr
    if k <= 0:
        raise CalibrationError("degenerate ellipse")
    M = A / k
    W = _sqrtm_spd(M)
    R = float(np.linalg.det(M)) ** -0.25  # среднегеометрический радиус
    return ctr + c0, W * R


def fit_ellipsoid_3d(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    c0 = p.mean(axis=0)
    q = p - c0
    x, y, z = q[:, 0], q[:, 1], q[:, 2]
    D = np.column_stack([x * x, y * y, z * z, 2 * x * y, 2 * x * z, 2 * y * z, 2 * x, 2 * y, 2 * z])
    v, *_ = np.linalg.lstsq(D, np.ones(len(x)), rcond=None)
    a, b, c, d, e, f, g, h, i = v
    A = np.array([[a, d, e], [d, b, f], [e, f, c]])
    bv = np.array([g, h, i])
    try:
        ctr = -np.linalg.solve(A, bv)
    except np.linalg.LinAlgError as ex:
        raise CalibrationError("degenerate ellipsoid") from ex
    k = 1.0 + ctr @ A @ ctr
    if k <= 0:
        raise CalibrationError("degenerate ellipsoid")
    M = A / k
    W = _sqrtm_spd(M)
    R = float(np.linalg.det(M)) ** (-1.0 / 6.0)
    return ctr + c0, W * R


def _fit_mode(p: np.ndarray, mode: str) -> tuple[np.ndarray, np.ndarray, str]:
    offset = np.zeros(3)
    matrix = np.eye(3)
    if mode == "3d":
        try:
            offset, matrix = fit_ellipsoid_3d(p)
            return offset, matrix, "3d"
        except CalibrationError:
            mode = "2d"
    c2, w2 = fit_ellipse_2d(p[:, :2])
    offset[:2] = c2
    matrix[:2, :2] = w2
    return offset, matrix, "2d"


def _radii(p: np.ndarray, offset: np.ndarray, matrix: np.ndarray, mode: str) -> np.ndarray:
    corr = (p - offset) @ matrix.T
    return np.hypot(corr[:, 0], corr[:, 1]) if mode == "2d" else np.linalg.norm(corr, axis=1)


def fit_calibration(points: np.ndarray | list, force: str | None = None) -> tuple[CompassCalibration, dict]:
    """Подогнать калибровку. force = '2d' | '3d' | None (авто). Возвращает (калибровка, инфо)."""
    p = np.asarray(points, dtype=float)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < MIN_SAMPLES:
        raise CalibrationError(f"too few samples: {len(p)} (need >= {MIN_SAMPLES})")
    rc = _rough_center(p)
    h_r = float(np.mean(np.hypot(p[:, 0] - rc[0], p[:, 1] - rc[1])))
    if h_r < 1e-6:
        raise CalibrationError("the module was not rotated")
    flat = float(np.std(p[:, 2])) / h_r
    mode = force or ("2d" if flat < FLAT_RATIO else "3d")
    info: dict = {"mode": mode, "flat_ratio": round(flat, 3), "samples": len(p)}

    offset, matrix, mode = _fit_mode(p, mode)
    r = _radii(p, offset, matrix, mode)
    # один проход отбраковки выбросов (магнитные помехи, точки «до начала вращения»)
    med = float(np.median(r))
    mad = float(np.median(np.abs(r - med))) * 1.4826
    keep = np.abs(r - med) <= max(5 * mad, 0.03 * med)
    if 0 < np.count_nonzero(~keep) and np.count_nonzero(keep) >= MIN_SAMPLES:
        p = p[keep]
        offset, matrix, mode = _fit_mode(p, mode)
        r = _radii(p, offset, matrix, mode)
    info["mode"] = mode
    info["outliers"] = int(np.count_nonzero(~keep))
    corr = (p - offset) @ matrix.T
    rel = float(np.std(r) / np.mean(r))
    cov = coverage_pct(p, offset)
    fit_q = max(0.0, min(1.0, 1.0 - rel / 0.15))
    quality = fit_q * max(0.0, min(1.0, cov / 80.0))
    info.update({"residual_pct": round(rel * 100, 2), "coverage_pct": round(cov, 1),
                 "field_uT": round(float(np.mean(np.linalg.norm(corr, axis=1))), 2)})
    cal = CompassCalibration(
        offset=[round(float(x), 4) for x in offset],
        matrix=[[round(float(x), 6) for x in row] for row in matrix],
        fitted_at=datetime.now(UTC).isoformat(timespec="seconds"),
        quality=round(quality, 3),
    )
    return cal, info


class CalibrationCollector:
    """Потокобезопасный сбор точек (add() вызывает поток компаса)."""

    def __init__(self, max_samples: int = 6000, min_step_uT: float = 0.4):
        self.max_samples = max_samples
        self.min_step = min_step_uT
        self._lock = threading.Lock()
        self._pts: list[tuple[float, float, float]] = []
        self.active = False
        self.last_info: dict = {}

    def start(self) -> None:
        with self._lock:
            self._pts = []
            self.active = True

    def cancel(self) -> None:
        with self._lock:
            self.active = False
            self._pts = []

    def add(self, v: tuple[float, float, float]) -> None:
        if not self.active:
            return
        with self._lock:
            if self._pts:
                lx, ly, lz = self._pts[-1]
                if math.dist((lx, ly, lz), v) < self.min_step:  # стоим на месте — не копим одинаковые
                    return
            if len(self._pts) >= self.max_samples:
                del self._pts[::2]  # прореживание
            self._pts.append((float(v[0]), float(v[1]), float(v[2])))

    def points(self) -> np.ndarray:
        with self._lock:
            return np.array(self._pts, dtype=float).reshape(-1, 3)

    def status(self) -> CalibrationStatus:
        p = self.points()
        if not self.active:
            return CalibrationStatus(active=False, samples=len(p), coverage_pct=0.0, message="")
        cnt = sector_counts(p)
        cov = 100.0 * float(np.count_nonzero(cnt >= MIN_PER_SECTOR)) / len(cnt)
        if len(p) < MIN_SAMPLES:
            msg = "Slowly turn the car (or the module) through a full circle"
        elif cov < 80:
            gaps = [i * (360 // SECTORS) for i, c in enumerate(cnt) if c < MIN_PER_SECTOR]
            msg = f"Missing directions ({len(gaps)} of {SECTORS} sectors), keep turning"
        else:
            msg = "Enough data - you can finish now"
        return CalibrationStatus(active=True, samples=len(p), coverage_pct=round(cov, 1), message=msg)

    def finish(self) -> CompassCalibration:
        """CalibrationError (ValueError) — если данных мало; сбор при этом продолжается."""
        p = self.points()
        cal, info = fit_calibration(p)
        with self._lock:
            self.active = False
        self.last_info = info
        return cal
