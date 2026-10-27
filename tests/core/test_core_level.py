"""Уровень: знаки крена/тангажа, калибровка «ноль» (single / two_point), «вперёд» (ось / по разгонам и
торможениям), подкладки под колёса, проверка неподвижности (409), API."""

from __future__ import annotations

import asyncio
import math
import random
import time

import numpy as np
import pytest
from core_helpers import login, running_app

from fnav.core.imu import ImuService
from fnav.core.level import (
    AXES,
    NEED_ZERO,
    NOT_CONNECTED,
    ROCKING,
    ForwardLearner,
    LevelError,
    LevelService,
    basis,
    default_forward,
    nearest_axis,
    still_check,
    tilt_angles,
    wheel_raise,
)
from fnav.models import AlarmSettings, GpsFix, LevelSettings

RAD = math.radians


def rot_x(deg: float) -> np.ndarray:
    c, s = math.cos(RAD(deg)), math.sin(RAD(deg))
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def up_in_car(pitch: float, roll: float) -> np.ndarray:
    """Тяжесть («вверх») в осях машины x — вперёд, y — влево, z — вверх: перед выше на pitch, правый борт — на roll."""
    p, r = RAD(pitch), RAD(roll)
    return np.array([math.sin(p), -math.cos(p) * math.sin(r), math.cos(p) * math.cos(r)])


def angle(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    return math.degrees(math.atan2(np.linalg.norm(np.cross(a, b)), a @ b))


# Датчик стоит «как попало»: стрелка X — влево, Y — назад, плата наклонена на 2° (оси датчика из осей машины: M @ v)
M = np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]]) @ rot_x(2.0)
CAR_UP, CAR_FWD = M @ np.array([0, 0, 1.0]), M @ np.array([1.0, 0, 0])


# ── математика ──


def test_pitch_roll_signs():
    f, r, u = basis(AXES["z+"], AXES["x+"])
    assert np.allclose(r, (0, -1, 0))  # датчик: x — вперёд, y — влево → «вправо» = −y
    assert tilt_angles(up_in_car(2.0, 0.0), f, r, u) == pytest.approx((2.0, 0.0), abs=1e-9)  # перед выше
    assert tilt_angles(up_in_car(0.0, 1.5), f, r, u) == pytest.approx((0.0, 1.5), abs=1e-9)  # правый борт выше
    assert tilt_angles(up_in_car(-1.0, -2.0), f, r, u) == pytest.approx((-1.0, -2.0), abs=0.01)
    # датчик на боку: «вверх» — y+, «вперёд» — z−
    f, r, u = basis(AXES["y+"], AXES["z-"])
    nose_up = np.array([0.0, math.cos(RAD(3)), -math.sin(RAD(3))])
    assert tilt_angles(nose_up, f, r, u) == pytest.approx((3.0, 0.0), abs=1e-9)
    # «вперёд» с наклоном относительно «вверх» — берётся только горизонтальная часть
    f, r, u = basis(AXES["z+"], (1.0, 0.0, 0.3))
    assert np.allclose(f, (1, 0, 0))


def test_uncalibrated_axes():
    assert nearest_axis((0.02, -0.98, 0.1)) == "y-" and nearest_axis((0.1, 0.1, -0.99)) == "z-"
    assert default_forward(AXES["z+"]) == "x+" and default_forward(AXES["x-"]) == "y+"
    with pytest.raises(ValueError):
        basis(AXES["z+"], AXES["z-"])


def test_wheel_raise():
    assert wheel_raise(0.0, 0.0, 3.5, 1.7) == {"fl": 0.0, "fr": 0.0, "rl": 0.0, "rr": 0.0}
    # перед выше на 1°: под задние колёса — 3.5·tan 1° = 6.1 см → 6.0
    assert wheel_raise(1.0, 0.0, 3.5, 1.7) == {"fl": 0.0, "fr": 0.0, "rl": 6.0, "rr": 6.0}
    # правый борт выше на 1°: под левые — 1.7·tan 1° = 3.0 см
    assert wheel_raise(0.0, 1.0, 3.5, 1.7) == {"fl": 3.0, "fr": 0.0, "rl": 3.0, "rr": 0.0}
    w = wheel_raise(1.2, -0.6, 3.5, 1.7)
    assert w == {"fl": 0.0, "fr": 2.0, "rl": 7.5, "rr": 9.0} and min(w.values()) == 0.0
    assert wheel_raise(-2.0, 0.0, 5.0, 2.0)["fl"] == pytest.approx(17.5)  # 5·tan 2° = 17.46 см


def test_still_check():
    ok, spread, ang = still_check([(0.0, 0.0, 1.0)] * 30)
    assert ok and spread == 0.0 and ang == pytest.approx(0.0, abs=1e-6)
    assert still_check([(0.0, 0.0, 1.0), (0.0, 0.0, 1.02)])[0] is False  # |a| «прыгает»
    rocking = [tuple(up_in_car(0, 0.5 * math.sin(i / 3))) for i in range(30)]
    ok, spread, ang = still_check(rocking)
    assert not ok and spread < 0.01 and ang > 0.3  # качается


# ── сервис ──


class Sensor:
    model = "MPU-6050"
    address = 0x68
    rate_hz = 200.0

    def read(self):
        return []

    def close(self) -> None: ...


class Rig:
    """LevelService с «датчиком», которому подаём отсчёты сами, и своими часами."""

    def __init__(self, settings: LevelSettings | None = None) -> None:
        self.st = {"s": settings or LevelSettings()}
        self.saved: list = []
        self.m = 0.0
        self.fix = GpsFix()
        self.imu = ImuService(lambda: AlarmSettings(), open_source=lambda b, a: Sensor())
        self.lv = LevelService(self.imu, lambda: self.st["s"], self._save, get_fix=lambda: self.fix,
                               mono=lambda: self.m)
        self.lv.capture_s = 0.5

    async def _save(self, cal) -> None:
        self.saved.append(cal)
        self.st["s"] = self.st["s"].model_copy(update={"calibration": cal})

    def connect(self) -> None:
        self.imu._on_state(Sensor(), None)

    def feed(self, vec, seconds: float = 0.1, noise: float = 0.0) -> None:
        """По 0.1 с (20 отсчётов); vec — вектор или функция времени."""
        rng = random.Random(1)
        for _ in range(max(1, round(seconds / 0.1))):
            batch = []
            for i in range(20):
                v = vec(self.m + i / 200) if callable(vec) else vec
                batch.append(tuple(float(x) + rng.gauss(0, noise) for x in v))
            self.imu.feed(batch)
            self.m += 0.1

    async def measure(self, vec, method: str, step: int | None = None):
        task = asyncio.create_task(self.lv.calibrate_level(method, step))
        await asyncio.sleep(0)
        for _ in range(200):
            if task.done():
                break
            self.feed(vec)
            for _ in range(3):
                await asyncio.sleep(0)
        return await task


async def test_single_calibration_zeroes_the_mount():
    rig = Rig()
    rig.connect()
    a = M @ up_in_car(0.0, 0.0)  # машина ровно, датчик стоит криво
    rig.feed(a, 3.0)
    st = rig.lv.status()
    assert st.sensor_ok and not st.calibrated and st.still and st.pitch_deg is not None
    st = await rig.measure(a, "single")
    cal = rig.saved[-1]
    assert st.calibrated and cal.method == "single" and cal.forward_source == "axis" and cal.axis == "x+"
    assert angle(cal.up, CAR_UP) < 0.01 and st.pitch_deg == pytest.approx(0.0, abs=0.05)
    # «вперёд» — ось y− (стрелка Y датчика смотрит назад)
    st = await rig.lv.set_forward_axis("y-")
    assert rig.saved[-1].axis == "y-" and rig.saved[-1].forward == [0.0, -1.0, 0.0]
    rig.feed(M @ up_in_car(1.0, -0.5), 6.0)  # машина: перед выше, левый борт выше
    st = rig.lv.status()
    assert st.pitch_deg == pytest.approx(1.0, abs=0.05) and st.roll_deg == pytest.approx(-0.5, abs=0.05)
    assert st.wheels_cm.fl == 0.0 and st.wheels_cm.rr > st.wheels_cm.rl > 0
    tel = rig.lv.telemetry()
    assert tel.calibrated and tel.pitch_deg == st.pitch_deg and tel.wheels_cm == st.wheels_cm
    with pytest.raises(LevelError) as e:
        await rig.lv.set_forward_axis("z+")  # смотрит вверх
    assert e.value.status == 409
    st = await rig.lv.reset()
    assert not st.calibrated and rig.saved[-1] is None


async def test_two_point_cancels_the_slope():
    rig = Rig()
    rig.connect()
    uphill, downhill = M @ up_in_car(4.0, 0.0), M @ up_in_car(-4.0, 0.0)  # уклон площадки 4°
    assert angle(uphill, CAR_UP) == pytest.approx(4.0, abs=0.01)  # single здесь ошибся бы на 4°
    rig.feed(uphill, 6.0)
    st = await rig.measure(uphill, "two_point", 1)
    assert st.wizard.two_point_step == 1 and not st.calibrated and rig.saved == []
    rig.feed(downhill, 6.0)  # развернулись на 180° и постояли
    st = await rig.measure(downhill, "two_point", 2)
    cal = rig.saved[-1]
    assert st.wizard.two_point_step == 0 and cal.method == "two_point" and angle(cal.up, CAR_UP) < 0.01
    await rig.lv.set_forward_axis("y-")
    rig.feed(uphill, 6.0)
    st = rig.lv.status()
    assert st.pitch_deg == pytest.approx(4.0, abs=0.05) and st.roll_deg == pytest.approx(0.0, abs=0.05)


async def test_rocking_is_409_and_errors():
    rig = Rig()
    with pytest.raises(LevelError) as e:
        await rig.lv.calibrate_level("single")
    assert (e.value.status, e.value.detail) == (503, NOT_CONNECTED)
    with pytest.raises(LevelError) as e:
        await rig.lv.calibrate_level("two_point", 2)  # без первого замера
    assert e.value.status == 409
    for call in (rig.lv.set_forward_axis("x+"), rig.lv.set_learning(True)):
        with pytest.raises(LevelError) as e:
            await call
        assert (e.value.status, e.value.detail) == (409, NEED_ZERO)
    rig.connect()
    rig.lv.capture_s = 1.0

    def rocking(t: float):  # кто-то ходит по салону: ±3° с периодом 2 с
        return up_in_car(0.0, 3.0 * math.sin(math.pi * t))

    rig.feed(rocking, 3.0)
    assert not rig.lv.status().still
    with pytest.raises(LevelError) as e:
        await rig.measure(rocking, "single")
    assert (e.value.status, e.value.detail) == (409, ROCKING) and rig.saved == []

    def bouncing(t: float):  # подпрыгивает: |a| гуляет ±0.1 g
        return up_in_car(0.0, 0.0) * (1 + 0.1 * math.sin(2 * math.pi * t))

    rig.feed(bouncing, 3.0)
    with pytest.raises(LevelError) as e:
        await rig.measure(bouncing, "single")
    assert e.value.status == 409


def drive(seconds: float, accel: float, v0: float, course0: float = 90.0, turn_dps: float = 0.0,
          step: float = 0.25):
    """Точки (dt, скорость, курс, ускорение м/с²) участка с постоянным ускорением."""
    out = []
    for i in range(round(seconds / step)):
        t = (i + 1) * step
        out.append((step, max(0.0, v0 + accel * t), (course0 + turn_dps * t) % 360, accel))
    return out


def route():
    """Стоянка → разгон 1.5 м/с² → прямо → поворот с разгоном (не считается) → торможение 2 м/с²."""
    return (drive(2, 0, 0) + drive(6, 1.5, 2) + drive(3, 0, 11) + drive(3, 1.0, 11, turn_dps=12)
            + drive(4, -2.0, 14))


def accel_vec(accel: float, rng: random.Random) -> np.ndarray:
    """Акселерометр (оси датчика) при продольном ускорении accel: «вверх» + accel/g вперёд + шум дороги."""
    v = CAR_UP + (accel / 9.81) * CAR_FWD
    return v + np.array([rng.gauss(0, 0.005) for _ in range(3)])


def test_forward_learned_from_acceleration_and_braking():
    rng = random.Random(7)
    lr = ForwardLearner(CAR_UP)
    t = 0.0
    done_at = None
    for dt, v, course, acc in route():
        t += dt
        lr.add(t, v + rng.gauss(0, 0.05), course + rng.gauss(0, 0.3), accel_vec(acc, rng))
        if done_at is None and lr.done:
            done_at = t
    assert done_at is not None and lr.secs >= 5.0 and lr.spread_deg <= 10.0
    assert angle(lr.forward(), CAR_FWD) < 3.0  # «вперёд» — стрелка Y датчика назад: (0, −1, 0) с точностью
    # на месте, по кругу и на крейсерской скорости — ничего не копится
    lr2 = ForwardLearner(CAR_UP)
    t = 0.0
    for dt, v, course, acc in drive(3, 0, 0) + drive(5, 0, 15) + drive(5, 1.5, 15, turn_dps=15):
        t += dt
        lr2.add(t, v, course, accel_vec(acc, rng))
    assert lr2.secs == 0.0 and lr2.spread_deg is None and not lr2.done


async def test_learning_through_the_service_saves_forward():
    rig = Rig()
    rig.connect()
    rig.feed(CAR_UP, 6.0)
    await rig.measure(CAR_UP, "single")
    st = await rig.lv.set_learning(True)
    assert st.wizard.learning and st.wizard.learn_s == 0.0
    rng = random.Random(3)
    for _dt, v, course, acc in route():
        a = accel_vec(acc, rng)
        rig.imu.feed([tuple(a)] * 25)  # 0.125 с отсчётов
        rig.m += 0.125
        rig.imu.feed([tuple(a)] * 25)
        rig.m += 0.125
        rig.fix = GpsFix(fix="3d", speed_mps=v, course_deg=course)
        await rig.lv.learn_tick()
        if not rig.lv.status().wizard.learning:
            break
    cal = rig.saved[-1]
    assert cal.forward_source == "driving" and cal.axis is None and cal.method == "single"
    assert angle(cal.forward, CAR_FWD) < 3.0 and not rig.lv.status().wizard.learning
    st = await rig.lv.set_learning(True)
    assert st.wizard.learning
    assert not (await rig.lv.set_learning(False)).wizard.learning


# ── API ──


async def test_level_api_mock(data_dir):
    async with running_app() as c:
        assert (await c.get("/api/level")).status_code == 401
        await login(c)
        core = c.app.state.core
        st = (await c.get("/api/level")).json()
        assert st["sensor_ok"] is False and st["pitch_deg"] is None and st["wheels_cm"] is None
        assert st["wizard"] == {"two_point_step": 0, "learning": False, "learn_s": 0.0, "learn_spread_deg": None}
        r = await c.post("/api/level/calibration/level", json={"method": "single"})
        assert r.status_code == 503 and r.json()["detail"] == "Level sensor is not connected"
        assert (await c.post("/api/level/calibration/level", json={"method": "two_point"})).status_code == 422
        assert (await c.post("/api/level/calibration/level", json={"method": "two_point", "step": 2})).status_code == 409
        assert (await c.post("/api/level/calibration/forward", json={})).status_code == 422
        assert (await c.post("/api/level/calibration/forward", json={"axis": "x+", "learn": True})).status_code == 422
        assert (await c.post("/api/level/calibration/forward", json={"axis": "w+"})).status_code == 422
        r = await c.post("/api/level/calibration/forward", json={"axis": "y+"})
        assert r.status_code == 409 and r.json()["detail"] == "Calibrate the level first"
        # --mock: «датчик» стоит с наклоном тангаж +1.2°, крен −0.6°
        core.level.capture_s = 0.2
        core.imu.start()
        t0 = time.monotonic()
        while not (st := (await c.get("/api/level")).json())["sensor_ok"]:
            assert time.monotonic() - t0 < 5
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        st = (await c.get("/api/level")).json()
        assert st["pitch_deg"] == pytest.approx(1.2, abs=0.15) and st["roll_deg"] == pytest.approx(-0.6, abs=0.15)
        assert st["calibrated"] is False and len(st["raw"]) == 3 and st["wheels_cm"]["fl"] == 0.0
        tel = (await c.get("/api/telemetry")).json()["level"]
        assert tel["sensor_ok"] is True and tel["calibrated"] is False and tel["pitch_deg"] is not None
        r = await c.post("/api/level/calibration/level", json={"method": "single"})
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["calibrated"] is True and st["calibration"]["method"] == "single"
        assert st["pitch_deg"] == pytest.approx(0.0, abs=0.2) and st["roll_deg"] == pytest.approx(0.0, abs=0.2)
        saved = (await c.get("/api/settings")).json()["level"]["calibration"]
        assert saved["method"] == "single" and saved["axis"] == "x+" and len(saved["up"]) == 3
        assert (await c.post("/api/level/calibration/forward", json={"axis": "z+"})).status_code == 409
        st = (await c.post("/api/level/calibration/forward", json={"axis": "y-"})).json()
        assert st["calibration"]["axis"] == "y-" and st["calibration"]["forward"] == [0.0, -1.0, 0.0]
        assert (await c.post("/api/level/calibration/forward", json={"learn": True})).json()["wizard"]["learning"]
        assert not (await c.post("/api/level/calibration/forward", json={"learn": False})).json()["wizard"]["learning"]
        st = (await c.post("/api/level/calibration/level", json={"method": "two_point", "step": 1})).json()
        assert st["wizard"]["two_point_step"] == 1
        st = (await c.delete("/api/level/calibration")).json()
        assert st["calibrated"] is False and st["wizard"]["two_point_step"] == 0
        assert (await c.get("/api/settings")).json()["level"]["calibration"] is None
        assert (await c.patch("/api/settings", json={"level": {"wheelbase_m": 9}})).status_code == 422
