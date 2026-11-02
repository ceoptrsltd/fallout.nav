from __future__ import annotations

import json
import math

import pytest

from fnav.models import CompassCalibration, Settings
from fnav.sensors import heading as hd
from fnav.sensors.hub import RealSensorHub


def run(anchor, smoother, mag, gps, speed, seconds, dt=0.1, th=3.0, band=1.0):
    out = None
    for _ in range(int(seconds / dt)):
        if gps is not None and hd.gps_weight(speed, th, band) >= 1.0:
            anchor.learn(mag, gps, dt)
        deg, _src = hd.fuse(anchor.apply(mag), gps, speed, th, band)
        out = smoother.update(deg, dt)
    return out


def test_no_jump_when_stopping_with_uncalibrated_compass():
    a, sm = hd.CompassAnchor(tau_s=5.0), hd.HeadingSmoother(0.15)
    # едем на курс 100° по GPS, компас врёт на +40° (показывает 140°)
    moving = run(a, sm, mag=140.0, gps=100.0, speed=10.0, seconds=30)
    assert abs(hd.angle_diff(moving, 100.0)) < 0.5
    # остановились: GPS-курса нет, курс по компасу + поправка — скачка на 40° нет
    stopped = run(a, sm, mag=140.0, gps=None, speed=0.0, seconds=5)
    assert abs(hd.angle_diff(stopped, 100.0)) < 1.0
    # на парковке повернули на 30° — курс следует за компасом относительно
    turned = run(a, sm, mag=170.0, gps=None, speed=0.0, seconds=5)
    assert abs(hd.angle_diff(turned, 130.0)) < 1.0


def test_offset_wraps_through_north():
    a = hd.CompassAnchor(tau_s=1.0)
    for _ in range(100):
        a.learn(mag_deg=350.0, gps_deg=10.0, dt=0.1)
    assert abs(a.offset_deg - 20.0) < 0.1
    assert abs(hd.angle_diff(a.apply(355.0), 15.0)) < 0.1


def test_without_learning_compass_passes_through():
    a = hd.CompassAnchor()
    assert a.offset_deg is None and a.apply(123.0) == 123.0


# ───────────── уровень хаба: поправка верна только при той калибровке, под которой выучена ─────────────

HARD = (9.0, -2.0, 3.0)  # hard iron: без калибровки компас на курсе 90° показывает ~62° (поправка ~+28°)
H, Z = 15.0, 45.0
UNCAL_EAST = math.degrees(math.atan2(H - HARD[1], HARD[0]))  # что показывает некалиброванный компас на востоке


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNAV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNAV_GPS_AUTOCONFIG", "0")
    return tmp_path


def raw(psi: float) -> tuple[float, float, float]:
    """Курс машины (склонение 0: магнитный = истинный) → сырой вектор FRD горизонтального модуля + hard iron."""
    p = math.radians(psi)
    return (H * math.cos(p) + HARD[0], -H * math.sin(p) + HARD[1], Z + HARD[2])


def settings(cal: CompassCalibration | None = None) -> Settings:
    st = Settings()
    st.compass.declination = 0.0
    if cal is not None:
        st.compass.calibration = cal
    return st


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_hub(st: Settings) -> RealSensorHub:
    return RealSensorHub(st, gpsd_port=1, enable_compass=False, enable_gpio=False, clock=Clock())


def drive(hub: RealSensorHub, psi: float, speed: float, seconds: float, step: float = 0.05):
    """Курс psi, GPS-курс = psi, скорость speed (0 — стоим); шаги слияния по step на своих часах хаба."""
    hub._still.moving = speed > 1.0  # без секунды подтверждения «едем»
    for _ in range(round(seconds / step)):
        hub._clock.t += step
        hub.gps.handle({"class": "TPV", "mode": 3, "lat": 52.2, "lon": 21.0, "speed": speed, "track": psi})
        hub._on_mag(raw(psi), "ist8310")
        hub._fuse_once()
    return hub.snapshot().heading


def stored(data_dir) -> dict:
    return json.loads((data_dir / "sensors_state.json").read_text(encoding="utf-8"))


def test_manual_calibration_drops_anchor_of_uncalibrated_compass(data_dir) -> None:
    """Сценарий с устройства: ездили с некалиброванным компасом (поправка +27.8°), затем идеальная ручная
    калибровка на стоянке носом на север: mag_deg 0.0, а курс показывал 28.1° до первой езды > 12.6 км/ч."""
    hub = make_hub(settings())
    h = drive(hub, 90.0, 12.0, 10)
    assert h.source == "gps" and h.anchor_offset_deg == pytest.approx(90.0 - UNCAL_EAST, abs=0.2)
    assert h.anchor_offset_deg > 25
    h = drive(hub, 90.0, 0.0, 2)  # остановились: курс = компас + поправка, без скачка
    assert h.source == "mag" and abs(hd.angle_diff(h.deg, 90.0)) < 0.5
    hub._save_state()  # как при остановке
    assert "anchor_offset_deg" in stored(data_dir)

    hub.calibration_start()  # машину (модуль) покрутили и встали носом на север
    for i in range(360):
        hub._on_mag(raw(float(i)), "ist8310")
    cal = hub.calibration_finish()
    assert cal.offset[:2] == pytest.approx(list(HARD[:2]), abs=0.3)
    h = drive(hub, 0.0, 0.0, 2)
    assert abs(hd.angle_diff(h.mag_deg, 0.0)) < 0.5
    assert abs(hd.angle_diff(h.deg, 0.0)) < 0.5, f"heading {h.deg} with the old GPS anchor"
    assert h.anchor_offset_deg is None
    assert "anchor_offset_deg" not in stored(data_dir)  # и копия на диске сброшена

    h = drive(hub, 90.0, 12.0, 2)  # на ходу выучилась заново — под новой калибровкой почти ноль
    assert abs(h.anchor_offset_deg) < 0.5


async def test_autocal_new_fit_drops_anchor(data_dir) -> None:
    hub = make_hub(settings())
    assert drive(hub, 90.0, 12.0, 3).anchor_offset_deg > 25
    fitted = CompassCalibration(offset=list(HARD), source="auto", rms_deg=1.0, coverage_pct=80.0, samples=500)
    hub.autocal.fit = lambda mount_offset_deg: None  # type: ignore[method-assign]
    hub.autocal.decide = lambda saved, effective, mount_offset_deg: (fitted, None)  # type: ignore[method-assign]
    await hub.autocal_tick(force=True)
    assert hub.effective_calibration() == fitted and hub._anchor.offset_deg is None
    h = drive(hub, 90.0, 0.0, 2)  # остановились: курс по новой калибровке, без старой поправки (было 118°)
    assert h.anchor_offset_deg is None and abs(hd.angle_diff(h.deg, 90.0)) < 0.5


def test_restart_ignores_anchor_learned_against_unsaved_autocal(data_dir) -> None:
    saved = CompassCalibration(offset=list(HARD), source="manual", fitted_at="2026-11-01T10:00:00+00:00")
    hub = make_hub(settings(saved))
    biased = CompassCalibration(offset=[HARD[0] - 5.0, HARD[1] + 3.0, HARD[2]], source="auto")
    hub._cal_auto = biased  # неудачная автоподгонка: применена, но не сохранена
    off = drive(hub, 90.0, 12.0, 3).anchor_offset_deg
    assert off > 10
    hub._save_state()
    assert stored(data_dir)["anchor_model"]

    # будь эта подгонка сохранена — поправка к ней подходит и переживает перезапуск
    same = make_hub(settings(biased))
    assert same._anchor.offset_deg == pytest.approx(off, abs=0.05)
    # а после перезагрузки действует сохранённая калибровка — поправка от несохранённой не применяется
    hub2 = make_hub(settings(saved))
    assert hub2._anchor.offset_deg is None
    assert "anchor_offset_deg" not in stored(data_dir)
    h = drive(hub2, 90.0, 0.0, 2)
    assert abs(hd.angle_diff(h.deg, 90.0)) < 0.5


def test_legacy_state_without_model_is_not_applied(data_dir) -> None:
    (data_dir / "sensors_state.json").write_text(json.dumps({"lat": 52.2, "lon": 21.0, "anchor_offset_deg": 27.8}),
                                                 encoding="utf-8")
    hub = make_hub(settings())
    assert hub._anchor.offset_deg is None and hub._last_pos == (52.2, 21.0)


IMPERFECT = CompassCalibration(offset=[5.0, 1.0, 0.0], source="manual", fitted_at="2026-11-01T10:00:00+00:00")


@pytest.mark.parametrize("change", [
    lambda c: setattr(c, "mount_offset_deg", 5.0),
    lambda c: setattr(c, "axes", "-x,-y,-z"),
    lambda c: setattr(c, "declination", 6.5),
    lambda c: setattr(c, "calibration", CompassCalibration()),  # сброс калибровки
    lambda c: setattr(c, "calibration", IMPERFECT.model_copy(update={"deviation": [2.0, 0.0, 0.0, 0.0, 0.0]})),
], ids=["mount_offset", "axes", "declination", "calibration_reset", "deviation"])
def test_compass_model_change_drops_anchor(data_dir, change) -> None:
    hub = make_hub(settings(IMPERFECT))
    assert abs(drive(hub, 90.0, 12.0, 2).anchor_offset_deg) > 5
    hub._save_state()
    new = hub.settings.model_copy(deep=True)
    change(new.compass)
    hub.apply_settings(new)
    assert hub._anchor.offset_deg is None
    assert "anchor_offset_deg" not in stored(data_dir)


def test_unrelated_changes_keep_anchor(data_dir) -> None:
    hub = make_hub(settings())
    auto = CompassCalibration(offset=[5.0, 1.0, 0.0], source="auto", rms_deg=2.0, coverage_pct=70.0)
    hub._cal_auto = auto
    off = drive(hub, 90.0, 12.0, 2).anchor_offset_deg
    assert abs(off) > 5
    new = hub.settings.model_copy(deep=True)
    new.heading.smoothing_s = 0.3
    new.compass.auto_save_interval_s = 600.0
    hub.apply_settings(new)
    assert hub._anchor.offset_deg == pytest.approx(off, abs=0.1)
    # fnav-core сохранил присланную автокалибровку — модель компаса та же, поправка остаётся
    new = hub.settings.model_copy(deep=True)
    new.compass.calibration = auto
    hub.apply_settings(new)
    assert hub.effective_calibration() == auto and hub._anchor.offset_deg == pytest.approx(off, abs=0.1)


class FakeReader:
    def __init__(self, axes: str) -> None:
        self.axes_spec = axes

    def request_reinit(self) -> None:
        pass


def test_detected_axes_change_drops_anchor(data_dir) -> None:
    hub = make_hub(settings())
    hub.compass_reader = FakeReader("x,-y,-z")  # type: ignore[assignment]
    hub._check_axes()
    assert drive(hub, 90.0, 12.0, 2).anchor_offset_deg is not None
    hub._check_axes()
    assert hub._anchor.offset_deg is not None  # те же оси — не трогаем
    hub.compass_reader.axes_spec = "-x,-y,-z"  # chip=auto нашёл другой чип
    hub._check_axes()
    assert hub._anchor.offset_deg is None
