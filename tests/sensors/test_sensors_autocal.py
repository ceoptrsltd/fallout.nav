"""Автокалибровка компаса по GPS-курсу: синтетическая поездка (повороты, прямые, стоянки)."""

from __future__ import annotations

import itertools
import json
import math

import numpy as np
import pytest

from fnav.models import CompassCalibration, CompassSettings, GpsFix, Settings
from fnav.sensors.autocal import AutoCalibrator, headings_after
from fnav.sensors.calibration import apply_calibration
from fnav.sensors.heading import angle_diff, mag_heading_deg, true_mag_heading

HARD = np.array([90.0, -18.0, -9.0])  # огромный hard iron, как на устройстве
S2 = np.array([[1.10, 0.07], [0.07, 0.92]])  # soft iron в плоскости
H, Z = 18.0, 45.0
DECL = 7.0
MOUNT_ERR = 6.0  # модуль повёрнут относительно машины на 6°


class SimClock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def raw_for(psi_true: float, rng: np.random.Generator | None = None, noise: float = 0.5) -> tuple:
    m = math.radians(psi_true - DECL + MOUNT_ERR)  # магнитный курс модуля
    xy = S2 @ np.array([H * math.cos(m), -H * math.sin(m)])
    v = np.array([xy[0], xy[1], Z]) + HARD
    if rng is not None:
        v = v + rng.normal(0, noise, 3)
    return tuple(float(x) for x in v)


def _const(h: float):
    return lambda tt: h


def _ramp(p0: float, dd: float, t0: float, du: float):
    return lambda tt: p0 + dd * (tt - t0) / du


def build_route(headings: list[float], straight_s: float = 25.0, stop_every: int = 5):
    """[(kind, t_start, t_end, psi(t), speed)]; повороты 15°/с, стоянки по 20 с."""
    segs = []
    t = 0.0
    psi = headings[0]
    for i, h in enumerate(headings):
        d = angle_diff(h, psi)
        if abs(d) > 0.1:
            dur = abs(d) / 15.0
            segs.append(("turn", t, t + dur, _ramp(psi, d, t, dur), 8.0))
            t += dur
        psi = h
        segs.append(("straight", t, t + straight_s, _const(h), 12.0))
        t += straight_s
        if stop_every and i % stop_every == stop_every - 1:
            segs.append(("stop", t, t + 20.0, _const(h), 0.0))
            t += 20.0
    return segs


def state_at(segs, t: float):
    for kind, a, b, f, v in segs:
        if a <= t < b:
            return kind, f(t) % 360.0, v
    kind, a, b, f, v = segs[-1]
    return kind, f(b) % 360.0, v


def drive(cal: AutoCalibrator, segs, clock: SimClock, rng: np.random.Generator, *, latency: float = 0.2,
          on_tick=None, tick_s: float = 30.0) -> list[tuple[float, str | None]]:
    """25 Гц компас, 10 Гц GPS (с задержкой latency и шумом курса 1°). Возвращает [(t, отказ|None)]."""
    t0 = clock.t
    end = segs[-1][2]
    results = []
    n = 0
    next_tick = tick_s
    while n * 0.04 < end:
        ts = n * 0.04
        clock.t = t0 + ts
        _, psi, _ = state_at(segs, ts)
        cal.add_mag(ts, raw_for(psi, rng))
        if n % 10 == 0 and ts > latency:  # TPV: эпоха ts-latency, получена в ts
            _, psi_e, v = state_at(segs, ts - latency)
            course = (psi_e + rng.normal(0, 1.0)) % 360.0
            fix = GpsFix(fix="3d", lat=55.7, lon=37.6, speed_mps=max(0.0, v + rng.normal(0, 0.1)),
                         course_deg=course, hdop=0.9, sats_used=12, sats_visible=20)
            results.append((ts, cal.on_gps(ts, fix, DECL)))
        if on_tick is not None and ts >= next_tick:
            on_tick()
            next_tick += tick_s
        n += 1
    return results


def heading_rms(cal: CompassCalibration, mount: float = 0.0) -> float:
    errs = []
    for psi in np.arange(0, 360, 5.0):
        cx, cy, _ = apply_calibration(raw_for(float(psi)), cal)
        est = true_mag_heading(mag_heading_deg(cx, cy), DECL, mount, cal.deviation)
        errs.append(angle_diff(est, float(psi)))
    return float(np.sqrt(np.mean(np.square(errs))))


def headings_all() -> list[float]:
    rng = np.random.default_rng(10)
    return [float(h) for h in rng.permutation(np.arange(36) * 10.0 + 5.0)]


@pytest.fixture
def drove():
    clock = SimClock()
    cal = AutoCalibrator(CompassSettings(), wall=clock)
    segs = build_route(headings_all())
    res = drive(cal, segs, clock, np.random.default_rng(1))
    return cal, segs, res, clock


def test_rejects_turns_and_stops(drove) -> None:
    cal, segs, res, clock = drove
    t0 = clock.t - (segs[-1][2] // 0.04) * 0.04  # момент начала поездки по «стенным» часам
    pts = cal.points()
    assert len(pts) > 300
    assert sum(1 for _, r in res if r is None) == len(pts)
    centers = np.sort(pts[:, 0] - t0)
    for row in pts:
        te = row[0] - t0 - 0.2  # GPS-эпоха точки
        seg = next(s for s in segs if s[1] <= te < s[2])
        assert seg[0] == "straight", (te, seg[0])
        # не на краю прямой: окно с обеих сторон точки было прямым
        assert te - seg[1] > 0.9 and seg[2] - te > 0.9, (te, seg[1], seg[2])
        # курс точки (регрессия по окну) близок к истинному
        assert abs(angle_diff(row[1], seg[3](te))) < 2.0
    assert cal.rejects["turning"] > 50
    assert cal.rejects["too slow"] + cal.rejects["stopped / no fix"] > 100
    # прореживание: не чаще 1 точки/с, не больше 30 точек на сектор
    assert np.min(np.diff(centers)) >= 0.99
    assert max(cal.sector_counts()) <= 30


def test_fit_recovers_heading(drove) -> None:
    cal, *_ = drove
    raw_cal = CompassCalibration()
    assert heading_rms(raw_cal) > 30  # без калибровки курс непригоден (hard iron 90 мкТл)
    c = cal.fit(mount_offset_deg=0.0)
    assert c is not None and c.source == "auto" and len(c.deviation) == 5
    assert c.coverage_pct >= 90 and c.samples == sum(cal.sector_counts())
    assert c.rms_deg < 2.0
    assert heading_rms(c) < 3.0
    # A поглощает ошибку установки
    assert abs(angle_diff(c.deviation[0], -MOUNT_ERR)) < 3.0
    st = cal.status(True, False, True, raw_cal)
    assert st.state == "fitted" and len(st.sectors) == 36 and st.samples == c.samples


def test_mount_offset_is_consistent(drove) -> None:
    cal, *_ = drove
    c = cal.fit(mount_offset_deg=10.0)  # пользователь задал поправку — формула курса всё равно сходится с GPS
    assert heading_rms(c, mount=10.0) < 3.0


def test_partial_coverage_waits() -> None:
    clock = SimClock()
    cal = AutoCalibrator(CompassSettings(), wall=clock)
    drive(cal, build_route([5.0, 15.0, 95.0, 185.0, 275.0]), clock, np.random.default_rng(2))
    assert cal.coverage_pct() < 60
    assert cal.fit(0.0) is None
    st = cal.status(True, False, True, CompassCalibration())
    assert st.state == "collecting" and "Collecting" in st.message


def test_partial_but_enough_coverage() -> None:
    clock = SimClock()
    cal = AutoCalibrator(CompassSettings(), wall=clock)
    hs = headings_all()[:24]  # ~67 % направлений
    drive(cal, build_route(hs), clock, np.random.default_rng(3))
    assert 60 <= cal.coverage_pct() < 75
    c = cal.fit(0.0)
    assert c is not None and heading_rms(c) < 3.0


def test_apply_and_save_rarely() -> None:
    clock = SimClock()
    cfg = CompassSettings(auto_save_interval_s=300)
    cal = AutoCalibrator(cfg, wall=clock)
    saved = {"cal": CompassCalibration(), "times": []}
    eff = {"cal": CompassCalibration()}

    def tick() -> None:
        cal.fit(0.0)
        apply, save = cal.decide(saved["cal"], eff["cal"], 0.0)
        if apply is not None:
            eff["cal"] = apply
        if save is not None:
            saved["cal"] = save
            saved["times"].append(clock.t)

    hs = headings_all()
    drive(cal, build_route(hs + hs[::-1]), clock, np.random.default_rng(4), on_tick=tick, tick_s=20.0)
    times = saved["times"]
    assert 1 <= len(times) <= 6
    assert all(b - a >= 300 for a, b in itertools.pairwise(times))
    assert saved["cal"].source == "auto" and heading_rms(saved["cal"]) < 3.0
    assert eff["cal"].source == "auto"
    # всё уже хорошо — повторное сохранение не нужно даже после интервала
    clock.t += 1000
    cal.fit(0.0)
    _, save = cal.decide(saved["cal"], saved["cal"], 0.0)
    assert save is None


def test_good_manual_calibration_not_replaced_without_gain(drove) -> None:
    cal, *_ = drove
    best = cal.fit(0.0)
    manual = best.model_copy(update={"source": "manual"})
    apply, save = cal.decide(manual, manual, 0.0)
    assert apply is None and save is None  # не лучше на заметную величину


def test_reset(drove) -> None:
    cal, *_ = drove
    cal.fit(0.0)
    cal.reset()
    assert sum(cal.sector_counts()) == 0 and cal.candidate is None
    assert cal.status(True, False, True, CompassCalibration()).samples == 0


def test_state_roundtrip(drove) -> None:
    cal, _, _, clock = drove
    d = json.loads(json.dumps(cal.to_state()))
    cal2 = AutoCalibrator(CompassSettings(), wall=clock)
    assert cal2.load_state(d) == sum(cal.sector_counts())
    assert cal2.sector_counts() == cal.sector_counts()
    c1, c2 = cal.fit(0.0), cal2.fit(0.0)
    assert c2 is not None and abs(c1.rms_deg - c2.rms_deg) < 0.05
    assert cal2.load_state({"version": 999}) == 0


def test_mirrored_axes_detected() -> None:
    clock = SimClock()
    cal = AutoCalibrator(CompassSettings(), wall=clock)
    segs = build_route(headings_all(), stop_every=0)
    rng = np.random.default_rng(5)
    # зеркалим y на входе — как при неверной таблице осей
    orig = cal.add_mag
    cal.add_mag = lambda t, v: orig(t, (v[0], -v[1] + 2 * HARD[1], v[2]))  # type: ignore[method-assign]
    drive(cal, segs, clock, rng)
    c = cal.fit(0.0)
    assert c is not None and c.rms_deg > 8
    assert "mirrored" in cal.message
    apply, _ = cal.decide(CompassCalibration(), CompassCalibration(), 0.0)
    assert apply is None  # плохую подгонку не применяем


def test_headings_after_matches_scalar() -> None:
    c = CompassCalibration(offset=[90, -18, 0], matrix=[[1.1, 0.1, 0], [0.1, 0.9, 0], [0, 0, 1]])
    pts = np.array([raw_for(a) for a in (0.0, 77.0, 200.0)])
    vec = headings_after(c, pts)
    for p, h in zip(pts, vec, strict=True):
        cx, cy, _ = apply_calibration(p, c)
        assert h == pytest.approx(mag_heading_deg(cx, cy))


# ───────────── уровень хаба ─────────────


@pytest.fixture
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNAV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNAV_GPS_AUTOCONFIG", "0")


class FakeReader:
    def __init__(self, axes: str) -> None:
        self.axes_spec = axes

    def request_reinit(self) -> None:
        pass


async def test_hub_autocal_saves_persists_and_resets(_data_dir) -> None:
    from fnav.sensors.hub import RealSensorHub

    clock = SimClock()
    hub = RealSensorHub(Settings(), gpsd_port=1, enable_compass=False, enable_gpio=False)
    hub.autocal.wall = clock
    hub.compass_reader = FakeReader("-x,-y,-z")  # type: ignore[assignment]
    drive(hub.autocal, build_route(headings_all()), clock, np.random.default_rng(6))
    saved: list = []
    hub.on_calibration_update(saved.append)
    await hub.autocal_tick(force=True)
    assert len(saved) == 1 and saved[0].source == "auto"
    assert hub.effective_calibration() == saved[0]
    await hub.autocal_tick(force=True)
    assert len(saved) == 1  # интервал сохранения не прошёл
    # fnav-core сохраняет присланную калибровку → apply_settings с ней же: применённая остаётся
    new = hub.settings.model_copy(deep=True)
    new.compass.calibration = saved[0]
    hub.apply_settings(new)
    hub._mag = (0.0, raw_for(123.0), "ist8310")
    hub._fuse_once()
    st = hub.snapshot().compass_cal
    assert st is not None and st.applied and st.state == "fitted" and len(st.sectors) == 36

    # переживание перезапуска
    n = sum(hub.autocal.sector_counts())
    hub2 = RealSensorHub(new, gpsd_port=1, enable_compass=False, enable_gpio=False)
    assert sum(hub2.autocal.sector_counts()) == n
    assert hub2.autocal.last_saved_iso == hub.autocal.last_saved_iso
    hub2.compass_reader = FakeReader("-x,-y,-z")  # type: ignore[assignment]
    hub2._check_axes()
    assert sum(hub2.autocal.sector_counts()) == n
    # смена осей → точки сбрасываются
    hub2.compass_reader = FakeReader("x,-y,-z")  # type: ignore[assignment]
    hub2._check_axes()
    assert sum(hub2.autocal.sector_counts()) == 0

    # reset через интерфейс
    hub.auto_calibration_reset()
    assert sum(hub.autocal.sector_counts()) == 0
    assert hub.effective_calibration() == saved[0]  # сохранённая калибровка не тронута


async def test_hub_disabled_autocal(_data_dir) -> None:
    from fnav.sensors.hub import RealSensorHub

    s = Settings()
    s.compass.auto_calibrate = False
    hub = RealSensorHub(s, gpsd_port=1, enable_compass=False, enable_gpio=False)
    hub._fuse_once()
    st = hub.snapshot().compass_cal
    assert st is not None and st.state == "disabled" and not st.enabled
