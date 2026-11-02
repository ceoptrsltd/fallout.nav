"""Гироскоп в курсе, компенсация наклона компаса, задний ход (reversing), привязка только на прямой, доверие GPS-курсу
по точности скорости, ошибка слияния снимается."""

from __future__ import annotations

import json
import math
import random
from types import SimpleNamespace

import numpy as np
import pytest
from vansim import H_UT, HARD, LAT, LON, Z_UT, Clock, Van, level_settings, rot

from fnav.core.imu import ImuService
from fnav.models import AlarmSettings, CompassCalibration, GpsFix, LevelCalibration, LevelRef, Settings
from fnav.sensors import heading as hd
from fnav.sensors.autocal import AutoCalibrator
from fnav.sensors.calibration import has_z, z_offset
from fnav.sensors.hub import RealSensorHub
from fnav.sensors.motion import MotionTracker
from fnav.sensors.mpu6050 import FakeAccel


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNAV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNAV_GPS_AUTOCONFIG", "0")
    return tmp_path


def err(a: float | None, b: float) -> float:
    assert a is not None
    return abs(hd.angle_diff(a, b))


# ───────────────────────── компенсация наклона ─────────────────────────


def test_tilt_compensated_heading_is_exact() -> None:
    rng = random.Random(3)
    for _ in range(200):
        psi, pitch, roll = rng.uniform(0, 360), rng.uniform(-10, 10), rng.uniform(-10, 10)
        m = rot(psi, pitch, roll, (H_UT, 0.0, Z_UT))
        up = rot(psi, pitch, roll, (0.0, 0.0, -1.0))
        assert err(hd.tilt_mag_heading_deg(m, up), psi) < 1e-6
    m = rot(123.0, 0.0, 0.0, (H_UT, 0.0, Z_UT))
    assert hd.tilt_mag_heading_deg(m, (0.0, 0.0, -1.0)) == pytest.approx(hd.mag_heading_deg(m[0], m[1]))
    # без компенсации: носом на восток, нос выше на 4° — курс врёт на ~tan(наклонения)·4° ≈ 10°
    m = rot(90.0, 4.0, 0.0, (H_UT, 0.0, Z_UT))
    assert 8.0 < err(hd.mag_heading_deg(m[0], m[1]), 90.0) < 12.0
    assert hd.tilt_mag_heading_deg(m, (0.0, 0.0, 0.0)) is None


def test_compass_up_follows_module_rotation() -> None:
    assert hd.compass_up((0.1, 0.2, 0.97), 0.0) == pytest.approx((0.1, 0.2, -0.97))
    # модуль повёрнут на 90° по часовой: его x смотрит вправо машины, y — назад
    assert hd.compass_up((0.1, 0.2, 0.97), 90.0) == pytest.approx((0.2, -0.1, -0.97))


@pytest.mark.parametrize("pitch,roll", [(4.0, 0.0), (-5.0, 3.0), (2.0, -4.0)])
def test_hub_compass_on_a_slope(pitch, roll) -> None:
    """Стоим на уклоне носом на восток: с калибровкой уровня по эталону курс точный, без неё — как раньше (~10°)."""
    van = Van(level_settings(), psi=90.0)
    van.pitch, van.roll = pitch, roll
    h = van.park(4)[-1].heading
    assert h.tilt_comp and err(h.mag_deg, 90.0) < 0.3 and err(h.deg, 90.0) < 0.5
    raw = Van(level_settings(level=False), psi=90.0)
    raw.pitch, raw.roll = pitch, roll
    h = raw.park(4)[-1].heading
    assert not h.tilt_comp and abs(hd.angle_diff(h.mag_deg, 90.0)) > 4.0


def test_hub_tilt_with_rotated_module() -> None:
    """Модуль компаса повёрнут на 90° (mount_offset_deg = −90): наклон машины переводится в его оси."""
    st = level_settings()
    st.compass.mount_offset_deg = -90.0
    van = Van(st, psi=30.0)
    van.module_rot = 90.0
    van.pitch, van.roll = 4.0, -3.0
    h = van.park(4)[-1].heading
    assert h.tilt_comp and err(h.mag_deg, 30.0) < 0.3


def test_no_tilt_compensation_while_moving_without_gps() -> None:
    """Тоннель: GPS нет, на повороте центробежное ускорение похоже на крен — компас без «компенсации»."""
    van = Van(psi=0.0)
    van.park(5)
    van.v = 10.0
    van.run(3)
    van.gps = False
    van.run(4)
    snaps = van.run(3, rate_dps=10.0)
    assert not snaps[-1].heading.tilt_comp
    assert err(snaps[-1].heading.mag_deg, van.psi) < 1.0


def test_z_offset_from_level_points_and_hub_estimate() -> None:
    zh = Z_UT / H_UT
    pts = np.array([rot(psi, 0.0, 0.0, (H_UT, 0.0, Z_UT)) for psi in range(0, 360, 5)]) + np.array(HARD)
    cal2d = CompassCalibration(offset=[HARD[0], HARD[1], 0.0])
    assert not has_z(cal2d) and has_z(CompassCalibration(offset=list(HARD)))
    assert has_z(CompassCalibration(matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.1]]))
    assert z_offset(pts, cal2d, zh) == pytest.approx(HARD[2], abs=1e-6)
    assert z_offset(pts[:10], cal2d, zh) is None  # мало точек

    # хаб: калибровка без оси Z (как у автокалибровки), смещение z — по точкам автокалибровки и WMM
    st = level_settings(cal=CompassCalibration(offset=[HARD[0], HARD[1], 0.0], source="auto", rms_deg=1.0,
                                               coverage_pct=80.0))
    van = Van(st, psi=90.0)
    van.hub._last_pos = (round(LAT, 1), round(LON, 1))
    state = {"version": 1, "points": [[1.0e9, float(c), *map(float, p), 0.0] for c, p in zip(range(0, 360, 5), pts)]}
    assert van.hub.autocal.load_state(state) == len(pts)
    van.hub._update_z()
    assert van.hub._z_off == pytest.approx(HARD[2], abs=0.6)  # WMM по сетке 0,1° ≈ поле в точке
    van.pitch = 4.0
    h = van.park(4)[-1].heading
    assert h.tilt_comp and err(h.mag_deg, 90.0) < 1.0


def test_manual_2d_calibration_stores_z_offset() -> None:
    hub = RealSensorHub(level_settings(cal=CompassCalibration()), gpsd_port=1, enable_compass=False, enable_gpio=False)
    hub._last_pos = (round(LAT, 1), round(LON, 1))
    hub.calibration_start()
    for i in range(360):  # машину покрутили на ровном месте
        b = rot(float(i), 0.0, 0.0, (H_UT, 0.0, Z_UT))
        hub._on_mag((b[0] + HARD[0], b[1] + HARD[1], b[2] + HARD[2]), "ist8310")
    cal = hub.calibration_finish()
    assert cal.offset[:2] == pytest.approx(list(HARD[:2]), abs=0.3)
    assert has_z(cal) and cal.offset[2] == pytest.approx(HARD[2], abs=0.6)


# ───────────────────────── доверие GPS-курсу, история TPV ─────────────────────────


def test_gps_trust_by_speed_accuracy() -> None:
    assert hd.gps_trust(3.0, None, 3.0, 1.0) == hd.gps_weight(3.0, 3.0, 1.0)  # eps нет — как раньше
    assert hd.gps_trust(1.0, 0.1, 3.0, 1.0) == 1.0 and hd.gps_trust(0.0, 0.1, 3.0, 1.0) == 0.0
    assert 0.0 < hd.gps_trust(0.5, 0.1, 3.0, 1.0) < 1.0
    assert hd.gps_trust(1.5, 0.5, 3.0, 1.0) == 1.0 and hd.gps_trust(1.2, 0.5, 3.0, 1.0) < 1.0
    assert hd.gps_trust(5.0, 2.0, 3.0, 1.0) == 0.0 and hd.gps_trust(6.0, 2.0, 3.0, 1.0) == 1.0  # плохой фикс
    assert hd.gps_trust(10.0, float("nan"), 3.0, 1.0) == 1.0
    # fuse с готовым доверием
    assert hd.fuse(100.0, 120.0, 1.2, 3.0, 1.0, w=1.0) == (120.0, "gps")


def test_tpv_track_course_rate_and_accel() -> None:
    tr = hd.TpvTrack()
    assert tr.course_rate() is None and tr.accel() is None
    for i in range(12):  # через север: 355 → 0 → 6.5 за 1.1 с, разгон 1 м/с²
        tr.add(100.0 + i * 0.1, 5.0 + i * 0.1, (355.0 + i * 1.0) % 360)
    assert tr.course_rate() == pytest.approx(10.0, abs=1e-6)
    assert tr.accel() == pytest.approx(1.0, abs=1e-6)
    tr.add(103.0, 5.0, 10.0)  # разрыв больше 1 с — заново
    assert tr.course_rate() is None


# ───────────────────────── MotionTracker ─────────────────────────


def feed(mt: MotionTracker, clk: Clock, seconds: float, gyro=(0.5, -0.3, 0.8), acc=(0.0, 0.0, 1.0), rng=None,
         noise=0.03) -> None:
    rng = rng or random.Random(5)
    for _ in range(round(seconds / 0.05)):
        clk.t += 0.05
        mt.add([acc] * 10, [tuple(g + rng.gauss(0, noise) for g in gyro) for _ in range(10)], 0.005)


def test_motion_bias_only_when_stopped_and_yaw_sign() -> None:
    clk = Clock()
    mt = MotionTracker(clk)
    mt.set_moving(True)  # GPS: едем (плавная дуга в тоннеле — гироскоп «тихий», но поворачивает)
    feed(mt, clk, 4.0)
    assert mt.read().bias is None and not mt.read().gyro_ok
    mt.set_moving(False)
    feed(mt, clk, 3.0)
    st = mt.read()
    assert st.bias_src == "still" and st.bias == pytest.approx((0.5, -0.3, 0.8), abs=0.03) and st.gyro_ok
    assert st.still_s > 1.0 and st.rate_dps == pytest.approx(0.0, abs=0.1)
    y0 = st.yaw_deg
    mt.set_moving(True)
    feed(mt, clk, 3.0, gyro=(0.5, -0.3, 0.8 - 20.0))  # вправо 20 °/с: у датчика z вверх — ωz < 0
    st = mt.read()
    assert st.rate_dps == pytest.approx(20.0, abs=0.1) and st.yaw_deg - y0 == pytest.approx(60.0, abs=0.5)
    assert st.still_s == 0.0 or st.still_s < 3.0


def test_motion_yaw_uses_level_up_axis() -> None:
    """Датчик стоит на ребре: «вверх» — его ось +y. Рысканье — проекция на U калибровки уровня."""
    clk = Clock()
    mt = MotionTracker(clk)
    mt.set_level(LevelCalibration(up=[0.0, 1.0, 0.0], forward=[1.0, 0.0, 0.0], method="reference",
                                  ref=LevelRef(a=[0.0, 1.0, 0.0], pitch_deg=0.0, roll_deg=0.0)))
    mt.set_bias((0.0, 0.0, 0.0), "saved")
    feed(mt, clk, 2.0, gyro=(0.0, -15.0, 0.0), acc=(0.0, 1.0, 0.0), noise=0.0)
    assert mt.read().rate_dps == pytest.approx(15.0) and mt.read().tilt_ok


def test_motion_saved_bias_until_first_stop_and_staleness() -> None:
    clk = Clock()
    mt = MotionTracker(clk)
    mt.set_bias((1.5, 0.0, 0.0), "saved")
    mt.set_moving(None)
    feed(mt, clk, 0.5, gyro=(0.4, -0.2, 0.9))
    assert mt.read().gyro_ok and mt.read().bias_src == "saved"
    mt.set_moving(False)
    feed(mt, clk, 3.0, gyro=(0.4, -0.2, 0.9))
    assert mt.read().bias_src == "still" and mt.read().bias == pytest.approx((0.4, -0.2, 0.9), abs=0.03)
    mt.set_bias((9.0, 9.0, 9.0), "saved")  # выученное на стоянке сохранённым не затирается
    assert mt.read().bias == pytest.approx((0.4, -0.2, 0.9), abs=0.03)
    clk.t += 1.0  # пачки перестали приходить (датчик отключили)
    st = mt.read()
    assert not st.fresh and not st.gyro_ok and st.rate_dps is None and st.still_s == 0.0


def test_motion_zero_gyro_is_no_gyro() -> None:
    clk = Clock()
    mt = MotionTracker(clk)
    mt.set_bias((0.0, 0.0, 0.0), "saved")
    feed(mt, clk, 3.0, gyro=(0.0, 0.0, 0.0), noise=0.0)
    assert not mt.read().gyro_ok


def test_motion_longitudinal_start() -> None:
    clk = Clock()
    mt = MotionTracker(clk)
    mt.set_level(LevelCalibration(up=[0.0, 0.0, 1.0], forward=[1.0, 0.0, 0.0]))
    mt.set_moving(False)
    tilt = (math.sin(math.radians(2.0)), 0.0, math.cos(math.radians(2.0)))  # стоим носом вверх на 2°
    feed(mt, clk, 3.0, acc=tilt)
    assert mt.read().v_long == pytest.approx(0.0, abs=1e-6)
    back = (tilt[0] - 1.0 / 9.80665, 0.0, tilt[2])  # трогаемся назад: −1 м/с²
    feed(mt, clk, 1.0, acc=back)
    assert mt.read().v_long == pytest.approx(-0.85, abs=0.1)
    mt.set_moving(False)
    feed(mt, clk, 6.0, acc=tilt)  # снова стоим — сброс
    assert abs(mt.read().v_long) < 0.05
    feed(mt, clk, 1.0, acc=(tilt[0] + 1.0 / 9.80665, 0.0, tilt[2]))
    assert mt.read().v_long > 0.5


# ───────────────────────── GyroHeading ─────────────────────────


def gyro_run(g: hd.GyroHeading, t: list, seconds: float, *, rate=0.0, mag=None, gps=None, gps_w=0.0, still=False,
             mag_ok=True, drift=0.0) -> list[float]:
    out = []
    for _ in range(round(seconds / 0.05)):
        t[0] += 0.05
        m = mag(t[0]) if callable(mag) else mag
        out.append(g.update(t[0], 0.05, (rate + drift) * 0.05, rate, m, mag_ok, gps, gps_w, still))
    return out


def test_gyro_heading_ignores_compass_disturbances() -> None:
    g, t = hd.GyroHeading(), [0.0]
    gyro_run(g, t, 1.0, mag=100.0)
    assert g.psi == pytest.approx(100.0) and g.referenced
    # медленно едем, мимо прошёл грузовик / трамвай: компас +25° на 2 с, гироскоп поворота не видит
    out = gyro_run(g, t, 6.0, mag=lambda s: 125.0 if 1.0 <= s - 1.0 < 3.0 else 100.0)
    assert max(err(v, 100.0) for v in out) < 1.0
    # стоим, включили инвертор: компас +15° насовсем
    out = gyro_run(g, t, 30.0, mag=115.0, still=True)
    assert err(out[40], 100.0) < 0.3 and err(out[-1], 100.0) < 4.5
    # разворот на месте: курс ведёт гироскоп, компас (с той же помехой) поворачивается так же — скачка нет
    start, p0 = t[0], g.psi
    out = gyro_run(g, t, 3.0, rate=30.0, mag=lambda s: (115.0 + 30.0 * (s - start)) % 360)
    assert err(g.psi, p0 + 90.0) < 2.0  # за 3 с компас (на ~12° в стороне) подтянул совсем чуть-чуть (τ = 20 с)


def test_gyro_heading_follows_gps_and_learns_bias_residue() -> None:
    g, t = hd.GyroHeading(), [0.0]
    gyro_run(g, t, 0.5, gps=40.0, gps_w=1.0)
    assert g.psi == pytest.approx(40.0) and g.source == "gps"
    out = gyro_run(g, t, 150.0, gps=40.0, gps_w=1.0, drift=0.5)  # гироскоп уходит на 0,5 °/с
    assert g.bias_dps == pytest.approx(0.5, abs=0.05) and err(out[-1], 40.0) < 0.1
    g2 = hd.GyroHeading()
    g2.init(0.0, False)
    gyro_run(g2, [0.0], 2.0, gps=30.0, gps_w=0.5, mag=None)
    assert g2.source == "gps" and 0.0 < g2.psi < 30.0


# ───────────────────────── ReverseDetector ─────────────────────────


def rev_run(det: hd.ReverseDetector, t: list, seconds: float, **kw) -> float | None:
    """→ через сколько секунд стал reversing (None — не стал)."""
    args = {"speed": 1.0, "course": 180.0, "eps": None, "heading": 0.0, "confirmed": True, "gyro": True,
            "rate_dps": 0.0, "v_long": None, "imu_still_s": 0.0, **kw}
    t0 = t[0]
    for _ in range(round(seconds / 0.1)):
        t[0] += 0.1
        if det.update(t[0], **args):
            return round(t[0] - t0, 2)
    return None


@pytest.mark.parametrize("kw,after", [
    ({"v_long": -0.5}, 0.4),  # акселерометр видел трогание назад
    ({}, 1.0),  # курс по гироскопу
    ({"gyro": False, "imu_still_s": None}, 1.5),  # только компас
])
def test_reverse_latch_times(kw, after) -> None:
    det, t = hd.ReverseDetector(), [0.0]
    assert rev_run(det, t, 3.0, **kw) == pytest.approx(0.1 + after, abs=0.01)  # от первого TPV «назад»


@pytest.mark.parametrize("kw", [
    {"confirmed": False},  # курс не сверен — по GPS не судим
    {"v_long": 0.6},  # акселерометр: трогались вперёд
    {"imu_still_s": 3.0},  # датчик неподвижен — GPS «едет» на стоянке
    {"speed": 0.3},  # слишком медленно
    {"speed": 6.0},  # слишком быстро для заднего хода
    {"course": 90.0},  # поперёк — неясно
    {"rate_dps": 25.0, "speed": 0.6},  # крутой поворот на малой скорости: снос антенны
    {"heading": None},
    {"speed": 0.6, "eps": 0.4},  # точность скорости хуже: 0,6 < 2·0,4
])
def test_reverse_not_latched(kw) -> None:
    assert rev_run(hd.ReverseDetector(), [0.0], 4.0, **kw) is None


def test_reverse_release() -> None:
    det, t = hd.ReverseDetector(), [0.0]

    def held(seconds: float, **kw) -> bool:
        """Задний ход держится все seconds?"""
        args = {"speed": 1.0, "course": 180.0, "eps": None, "heading": 0.0, "confirmed": True, "gyro": True,
                "rate_dps": 0.0, "v_long": None, "imu_still_s": 0.0, **kw}
        ok = True
        for _ in range(round(seconds / 0.1)):
            t[0] += 0.1
            ok = det.update(t[0], **args) and ok
        return ok

    assert rev_run(det, t, 2.0) is not None
    assert held(1.0, course=100.0)  # неясный курс — держим
    assert held(2.0, imu_still_s=5.0)  # ровный ход без тряски для датчика — тоже покой: не снимаем
    assert held(0.4, speed=0.0, course=None)  # остановка короче 0,5 с
    assert not held(0.2, speed=0.0, course=None) and not det.reversing
    assert rev_run(det, t, 2.0) is not None
    assert not held(0.6, course=2.0) and not det.reversing  # вперёд
    assert rev_run(det, t, 2.0) is not None
    assert not held(0.1, speed=7.0) and not det.reversing  # быстрее 5 м/с — сразу нет


# ───────────────────────── хаб: задний ход (B15), привязка, гироскоп ─────────────────────────


def drive_forward(van: Van, seconds: float = 10.0) -> None:
    van.park(5)
    van.run(4, accel=2.5)
    van.run(seconds)
    van.run(4, accel=-2.5)
    van.v = 0.0
    van.run(3)


@pytest.mark.parametrize("imu", [False, True], ids=["compass", "imu"])
def test_reverse_at_10_kmh_keeps_heading(imu) -> None:
    """B15: компас 0°, задний ход 2,8–3,2 м/с — курс и карта не переворачиваются (было 1° → 271° → 89° → 181°)."""
    van = Van(imu=imu, psi=0.0)
    drive_forward(van)
    assert van.hub.snapshot().heading.anchor_offset_deg is not None
    snaps = van.run(2.0, accel=-1.5)
    for k in range(40):
        van.v = -(2.8 + 0.4 * ((k * 7) % 10) / 10)
        snaps += van.run(0.25)
    assert max(err(s.heading.deg, 0.0) for s in snaps) < 3.0
    assert {s.heading.source for s in snaps} == {"mag"}
    first = next(i for i, s in enumerate(snaps) if s.reversing) * 0.05
    start = next(i for i in range(len(snaps)) if 1.5 * (i + 1) * 0.05 >= 0.4) * 0.05  # скорость дошла до 0,4 м/с
    assert first - start <= (1.6 if not imu else 1.0)
    assert all(s.reversing for s in snaps[int(first / 0.05):])
    van.v = 0.0
    assert not van.run(1.5)[-1].reversing  # остановились
    snaps = van.run(4.0, accel=1.0)  # и вперёд
    assert not any(s.reversing for s in snaps) and err(snaps[-1].heading.deg, 0.0) < 3.0


def test_reverse_at_walking_speed_with_imu() -> None:
    """2–5 км/ч назад: признак — в пределах ~1 с уверенного движения назад."""
    van = Van(psi=200.0)
    drive_forward(van)
    snaps = van.run(1.4, accel=-0.5)  # до 0,7 м/с (2,5 км/ч)
    snaps += van.run(2.0)
    t_conf = next(i for i in range(len(snaps)) if 0.5 * min(i + 1, 28) * 0.05 >= 0.5) * 0.05  # 0,5 м/с
    first = next(i for i, s in enumerate(snaps) if s.reversing) * 0.05
    assert first - t_conf <= 1.0
    assert max(err(s.heading.deg, 200.0) for s in snaps) < 2.0


@pytest.mark.parametrize("imu", [False, True], ids=["compass", "imu"])
def test_no_reverse_when_turning_slowly_forward(imu) -> None:
    van = Van(imu=imu, psi=0.0)
    drive_forward(van)
    van.v = 1.0
    psi = []
    snaps = []
    for _ in range(12 * 20):
        van.step(rate_dps=15.0)
        snaps.append(van.hub.snapshot())
        psi.append(van.psi)
    assert not any(s.reversing for s in snaps)
    assert max(err(s.heading.deg, p) for s, p in zip(snaps, psi)) < (3.0 if not imu else 1.0)


@pytest.mark.parametrize("imu", [False, True], ids=["compass", "imu"])
def test_no_reverse_from_gps_jitter_when_parked(imu) -> None:
    van = Van(imu=imu, psi=60.0)
    drive_forward(van)
    van.run(5)
    snaps = []
    for k in range(60):  # Static Hold отпускает: скорость 0,6 м/с, курс куда попало
        van.speed_override = 0.6 if k % 3 else 0.0
        van.course_override = van.rng.uniform(0, 360)
        snaps += van.run(0.1)
    van.speed_override, van.course_override = 0.7, 240.0  # «уехали» назад на 1,2 с
    snaps += van.run(1.2)
    assert not any(s.reversing for s in snaps)


def test_gyro_hub_disturbance_tunnel_and_turns() -> None:
    van = Van(psi=0.0)
    drive_forward(van)
    van.v = 1.2  # медленно, курс по компасу (+ гироскоп)
    van.run(3)
    van.mag_dist = 25.0  # грузовик рядом
    snaps = van.run(2)
    van.mag_dist = 0.0
    snaps += van.run(3)
    assert max(err(s.heading.deg, 0.0) for s in snaps) < 1.0
    assert snaps[-1].heading.yaw_rate_dps == pytest.approx(0.0, abs=0.2)
    # тоннель: GPS нет, поворот на 90° — курс по гироскопу
    van.v = 15.0
    van.run(3)
    van.gps = False
    van.run(4)
    snaps = van.run(9, rate_dps=10.0)
    assert snaps[-1].heading.yaw_rate_dps == pytest.approx(10.0, abs=0.2)
    snaps += van.run(5)
    assert err(snaps[-1].heading.deg, 90.0) < 1.0


def test_gyro_less_lag_than_gps_in_turns() -> None:
    """Поворот 15 °/с на скорости: GPS-курс относится к моменту 0,15 с назад — курс по гироскопу не отстаёт."""
    errs = {}
    for imu in (False, True):
        van = Van(imu=imu, psi=0.0)
        drive_forward(van)
        van.run(4, accel=2.5)
        van.run(3)
        hist: list[float] = []
        worst = 0.0
        for _ in range(6 * 20):
            hist.append(van.psi)
            van.course_override = hist[-3] if len(hist) >= 3 else hist[0]  # курс GPS — 0,15 с назад
            van.step(rate_dps=15.0)
            worst = max(worst, err(van.hub.snapshot().heading.deg, van.psi))
        errs[imu] = worst
    assert errs[True] < 1.5 < errs[False]


def test_anchor_learns_only_on_straight_with_fresh_tpv() -> None:
    st = level_settings(cal=CompassCalibration(offset=[HARD[0] - 4.0, HARD[1] + 2.0, HARD[2]], source="manual",
                                               fitted_at="2026-11-01T10:00:00+00:00"))  # компас врёт на несколько °
    van = Van(st, imu=False, psi=0.0)
    van.park(3)
    van.run(4, accel=2.5)
    van.run(1, rate_dps=10.0)  # кругами (без гироскопа поворот виден по GPS-курсу за ~0,5 с)
    van.hub._anchor.reset()
    van.run(10, rate_dps=10.0)
    assert van.hub._anchor.offset_deg is None
    van.tpv_age = 0.4  # TPV доходят с опозданием
    van.run(5)
    assert van.hub._anchor.offset_deg is None
    van.tpv_age = 0.0
    van.run(5)
    assert van.hub._anchor.offset_deg is not None


def test_imu_lost_falls_back_without_jump_and_yaw_rate_exposed() -> None:
    van = Van(psi=45.0)
    van.park(4)
    assert van.hub.snapshot().heading.yaw_rate_dps == pytest.approx(0.0, abs=0.1)
    before = van.hub.snapshot().heading.deg
    van.imu = False  # датчик отключили: пачек нет
    snaps = van.run(3)
    assert snaps[-1].heading.yaw_rate_dps is None
    assert max(err(s.heading.deg, before) for s in snaps) < 0.5
    no_imu = Van(imu=False, psi=45.0)
    assert no_imu.park(2)[-1].heading.yaw_rate_dps is None


async def test_gyro_bias_saved_and_restored(_data_dir) -> None:
    van = Van(gyro_bias=(0.7, -0.5, 1.1))
    van.park(4)
    await van.hub.autocal_tick()
    saved = json.loads((_data_dir / "sensors_state.json").read_text(encoding="utf-8"))
    assert saved["gyro_bias"] == pytest.approx([0.7, -0.5, 1.1], abs=0.05)
    hub2 = RealSensorHub(level_settings(), gpsd_port=1, enable_compass=False, enable_gpio=False, clock=Clock())
    st = hub2.motion.read()
    assert st.bias_src == "saved" and st.bias == pytest.approx((0.7, -0.5, 1.1), abs=0.05)


def test_fusion_error_is_cleared_after_a_good_cycle(monkeypatch) -> None:
    """B21: ошибка слияния видна в снимке и снимается, когда цикл снова удался."""
    van = Van()
    hub = van.hub
    van.park(1)
    real = hub._declination
    monkeypatch.setattr(hub, "_declination", lambda fix: 1 / 0)
    try:
        hub._fuse_once()
    except Exception as e:  # noqa: BLE001 — как в _fusion_loop
        hub._fusion_failed(e)
    assert any(e.startswith("Heading: internal error") for e in hub.snapshot().errors)
    monkeypatch.setattr(hub, "_declination", real)
    hub._fuse_once()
    assert not any("internal error" in e for e in hub.snapshot().errors) and "fusion" not in hub._errors


def test_reset_to_default_calibration_keeps_autocal_points_until_reset_auto() -> None:
    """B8 (сервер): сброс калибровки к умолчаниям не стирает точки молча; их чистит reset-auto (админка зовёт оба)."""
    st = level_settings()
    hub = RealSensorHub(st, gpsd_port=1, enable_compass=False, enable_gpio=False, clock=Clock())
    pts = [[1.0e9, float(c), 20.0, 0.0, 40.0, 0.0] for c in range(0, 360, 5)]
    hub.autocal.load_state({"version": 1, "points": pts})
    new = hub.settings.model_copy(deep=True)
    new.compass.calibration = CompassCalibration()
    hub.apply_settings(new)
    assert len(hub.autocal.points()) == len(pts) and hub.effective_calibration() == CompassCalibration()
    old_client = hub.settings.model_copy(deep=True)  # старая админка: offset/matrix/fitted_at, остальное прежнее
    old_client.compass.calibration = st.compass.calibration.model_copy(update={"fitted_at": None})
    hub.apply_settings(old_client)
    assert len(hub.autocal.points()) == len(pts)
    hub.auto_calibration_reset()
    assert len(hub.autocal.points()) == 0 and hub._z_off is None


# ───────────────────────── автокалибровка, датчик ─────────────────────────


def test_autocal_rejects_poor_speed_accuracy_and_slopes() -> None:
    ac = AutoCalibrator(Settings().compass)
    fix = SimpleNamespace(fix="3d", course_deg=90.0, speed_mps=10.0, hdop=0.8, eps_mps=1.5)
    assert ac.on_gps(1.0, fix, 0.0) == "poor speed accuracy"
    fix.eps_mps = 0.3
    assert ac.on_gps(1.1, fix, 0.0, tilt_deg=4.0) == "slope"
    assert ac.on_gps(1.2, fix, 0.0, tilt_deg=1.0) == "warming up"
    assert ac.on_gps(1.3, GpsFix(fix="3d", course_deg=90.0, speed_mps=10.0, hdop=0.8), 0.0) == "warming up"


def test_imu_service_motion_consumers_and_fake_gyro() -> None:
    clk = {"t": 0.0}
    fake = FakeAccel(noise_g=0.0, noise_dps=0.0, clock=lambda: clk["t"], gyro_bias=(0.1, 0.2, 0.3))
    imu = ImuService(lambda: AlarmSettings(), open_source=lambda b, a: fake)
    got: list = []
    imu.subscribe(lambda s, dt: got.append(("acc", len(s))))
    imu.subscribe_motion(lambda s, g, dt: got.append(("gyro", len(g), round(dt, 4))))
    imu._on_state(fake, None)
    assert imu.ok and imu.gyro
    clk["t"] = 1.0
    fake.yaw_dps = 10.0
    acc, gyr = fake.read_imu()
    assert len(acc) == len(gyr) == 20 and gyr[0] == pytest.approx((0.1, 0.2, 0.3 - 10.0))
    imu.feed(acc)
    imu.feed_gyro(acc, gyr)
    assert got == [("acc", 20), ("gyro", 20, 0.005)]
    imu._on_state(None, "MPU-6050: gone")
    assert not imu.gyro
