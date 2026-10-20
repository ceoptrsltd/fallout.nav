from __future__ import annotations

import itertools
import math

import pytest

from fnav.sensors import heading as hd


def test_norm_and_diff() -> None:
    assert hd.norm360(-10) == 350 and hd.norm360(720) == 0 and hd.norm360(359.9) == pytest.approx(359.9)
    assert hd.norm360(-1e-15) == 0.0
    assert hd.angle_diff(10, 350) == pytest.approx(20)
    assert hd.angle_diff(350, 10) == pytest.approx(-20)
    assert hd.angle_diff(180, 0) == pytest.approx(180)


def test_mag_heading_frd() -> None:
    assert hd.mag_heading_deg(20, 0) == pytest.approx(0)  # поле вдоль «вперёд» → север
    assert hd.mag_heading_deg(0, -20) == pytest.approx(90)  # смотрим на восток: север слева (−y)
    assert hd.mag_heading_deg(-20, 0) == pytest.approx(180)
    assert hd.mag_heading_deg(0, 20) == pytest.approx(270)
    assert hd.mag_heading_deg(0, 0) is None


def test_true_mag_heading() -> None:
    assert hd.true_mag_heading(355, 12, 0) == pytest.approx(7)
    assert hd.true_mag_heading(10, 12, -30) == pytest.approx(352)
    assert hd.true_mag_heading(None, 12, 0) is None


def test_gps_weight() -> None:
    w = lambda v: hd.gps_weight(v, 3.0, 1.0)
    assert w(None) == 0 and w(0) == 0 and w(2.5) == 0
    assert w(3.5) == 1 and w(10) == 1
    assert w(3.0) == pytest.approx(0.5)
    vals = [w(2.5 + i * 0.01) for i in range(101)]
    assert all(b >= a for a, b in itertools.pairwise(vals))  # монотонно
    assert hd.gps_weight(3.0, 3.0, 0.0) == 1 and hd.gps_weight(2.99, 3.0, 0.0) == 0


def test_blend_wraps_through_north() -> None:
    assert abs(hd.angle_diff(hd.blend_angles(350, 10, 0.5), 0)) < 1e-9
    # векторное смешивание ≈ линейному по дуге для близких углов
    assert hd.blend_angles(350, 10, 0.25) == pytest.approx(355, abs=0.1)
    assert hd.blend_angles(350, 10, 0.75) == pytest.approx(5, abs=0.1)
    assert hd.blend_angles(90, 270, 0.6) == 270  # противоположные — без NaN


def test_fuse_sources() -> None:
    f = lambda m, g, v: hd.fuse(m, g, v, 3.0, 1.0)
    assert f(100, 120, 0.0) == (100, "mag")
    assert f(100, 120, 5.0) == (120, "gps")
    deg, src = f(350, 10, 3.0)
    assert src == "blend" and min(abs(hd.angle_diff(deg, 0)), 360) < 1e-6
    assert f(100, None, 10.0) == (100, "mag")  # нет GPS-курса — компас
    assert f(None, 120, 5.0) == (120, "gps")  # нет компаса — GPS
    assert f(None, 120, 2.0) == (120, "gps")  # нет компаса, медленно, но едем
    assert f(None, 120, 0.2) == (None, "none")  # стоим без компаса — GPS-курс шум
    assert f(None, None, 5.0) == (None, "none")


def test_fuse_continuity_across_band() -> None:
    """При разгоне через зону бленда курс меняется плавно, без скачков."""
    prev = None
    for i in range(201):
        v = 2.0 + i * 0.01
        deg, _ = hd.fuse(355.0, 15.0, v, 3.0, 1.0)
        if prev is not None:
            assert abs(hd.angle_diff(deg, prev)) < 1.0
        prev = deg


def test_smoother_time_constant() -> None:
    s = hd.HeadingSmoother(tau_s=1.0)
    assert s.update(0, 0.05) == pytest.approx(0)
    # после tau — около 63 % пути (для малых углов)
    for _ in range(20):
        v = s.update(10, 0.05)
    assert v == pytest.approx(10 * (1 - math.exp(-1)), abs=0.1)


def test_smoother_wraps_shortest_way() -> None:
    s = hd.HeadingSmoother(tau_s=0.5)
    s.update(350, 0.1)
    path = [s.update(20, 0.05) for _ in range(100)]
    # идём 350 → 360/0 → 20, ни разу не через 180
    assert all(v >= 340 or v <= 21 for v in path)
    assert path[-1] == pytest.approx(20, abs=0.1)


def test_smoother_hold_and_zero_tau() -> None:
    s = hd.HeadingSmoother(tau_s=0.0)
    assert s.update(None, 0.1) is None
    assert s.update(123, 0.1) == pytest.approx(123)
    assert s.update(200, 0.1) == pytest.approx(200)  # tau=0 — без сглаживания
    assert s.update(None, 0.1) == pytest.approx(200)  # удержание
