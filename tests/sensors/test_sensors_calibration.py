from __future__ import annotations

import math

import numpy as np
import pytest

from fnav.sensors.calibration import (
    CalibrationCollector,
    CalibrationError,
    apply_calibration,
    coverage_pct,
    fit_calibration,
)
from fnav.sensors.heading import angle_diff, mag_heading_deg

H, Z = 15.0, 50.0  # Москва: горизонтальная ≈ 15 мкТл, вертикальная ≈ 50 мкТл
S = np.array([[1.15, 0.08, 0.02], [0.08, 0.90, -0.03], [0.02, -0.03, 1.00]])  # soft iron (симметричная)
OFF = np.array([12.0, -25.0, 8.0])  # hard iron


def level_field(psi_deg: np.ndarray) -> np.ndarray:
    p = np.radians(psi_deg)
    return np.column_stack([H * np.cos(p), -H * np.sin(p), np.full_like(p, Z)])


def distort(b: np.ndarray, rng: np.random.Generator, noise: float = 0.3) -> np.ndarray:
    return b @ S.T + OFF + rng.normal(0, noise, b.shape)


def heading_errors(cal, psi: np.ndarray, raw: np.ndarray) -> np.ndarray:
    errs = []
    for ang, r in zip(psi, raw, strict=True):
        cx, cy, _ = apply_calibration(r, cal)
        errs.append(abs(angle_diff(mag_heading_deg(cx, cy), ang)))
    return np.array(errs)


def test_2d_car_rotation_recovers_heading() -> None:
    rng = np.random.default_rng(1)
    psi = rng.uniform(0, 720, 400) % 360  # два круга на парковке
    raw = distort(level_field(psi), rng)
    # без калибровки курс сильно врёт
    from fnav.models import CompassCalibration

    assert heading_errors(CompassCalibration(), psi, raw).max() > 20
    cal, info = fit_calibration(raw)
    assert info["mode"] == "2d"
    test_psi = np.arange(0, 360, 5.0)
    test_raw = distort(level_field(test_psi), rng, noise=0.0)  # ошибка именно калибровки, без шума замера
    err = heading_errors(cal, test_psi, test_raw)
    assert err.max() < 2.0, err.max()
    assert cal.quality is not None and cal.quality > 0.8
    assert abs(cal.offset[0] - (OFF[0] + 0.02 * Z)) < 1.0  # центр эллипса в плоскости XY


def random_rotations(n: int, rng: np.random.Generator) -> np.ndarray:
    q = rng.normal(size=(n, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    w, x, y, z = q.T
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], 1)


def test_3d_full_rotation_recovers_heading() -> None:
    rng = np.random.default_rng(2)
    world = np.array([H, 0.0, Z])
    R = random_rotations(600, rng)
    body = np.einsum("nji,j->ni", R, world)  # R^T · поле
    raw = distort(body, rng)
    cal, info = fit_calibration(raw)
    assert info["mode"] == "3d"
    test_psi = np.arange(0, 360, 7.5)
    err = heading_errors(cal, test_psi, distort(level_field(test_psi), rng, noise=0.0))
    assert err.max() < 2.0, err.max()
    assert abs(info["field_uT"] - math.hypot(H, Z)) < 3.0
    assert cal.quality > 0.8
    assert np.allclose(cal.offset, OFF, atol=1.0)


def test_partial_coverage_lowers_quality() -> None:
    rng = np.random.default_rng(3)
    psi = rng.uniform(0, 120, 300)
    raw = distort(level_field(psi), rng)
    assert coverage_pct(raw) < 45
    full = distort(level_field(rng.uniform(0, 360, 300)), rng)
    q_part = fit_calibration(raw)[0].quality
    q_full = fit_calibration(full)[0].quality
    assert q_part < 0.6 < q_full


def test_too_few_points() -> None:
    with pytest.raises(CalibrationError):
        fit_calibration(np.zeros((5, 3)))
    with pytest.raises(ValueError):  # CalibrationError — подкласс ValueError
        fit_calibration(np.tile([1.0, 2.0, 3.0], (100, 1)))


def test_collector_flow() -> None:
    rng = np.random.default_rng(4)
    c = CalibrationCollector()
    c.add((1.0, 2.0, 3.0))  # не активен — игнор
    assert c.status().samples == 0 and not c.status().active
    c.start()
    for _ in range(50):
        c.add((10.0, 10.0, 10.0))  # стоим — одинаковые точки не копятся
    assert c.status().samples == 1
    with pytest.raises(CalibrationError):
        c.finish()
    assert c.status().active  # сбор продолжается после неудачи
    for v in distort(level_field(np.linspace(0, 360, 360, endpoint=False)), rng):
        c.add(tuple(v))
    st = c.status()
    assert st.active and st.coverage_pct > 95 and "finish" in st.message
    cal = c.finish()
    assert not c.status().active and cal.fitted_at and cal.quality > 0.8
    c.start()
    c.cancel()
    assert c.status().samples == 0
