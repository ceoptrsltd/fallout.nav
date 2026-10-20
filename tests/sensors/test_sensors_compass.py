from __future__ import annotations

import threading
import time

import pytest
from sensors_fakes import FakeIST8310Bus

from fnav.sensors import compass as cm
from fnav.sensors.gpio import decide_polarity


def test_ist8310_detect_init_read() -> None:
    bus = FakeIST8310Bus(field=(30.0, -6.0, 45.0))
    drv = cm.detect(bus, "auto")
    assert isinstance(drv, cm.IST8310) and drv.address == 0x0E
    drv.sleep = lambda s: None
    drv.init()
    assert (0x0E, 0x0B, 0x01) in bus.writes  # soft reset
    assert (0x0E, 0x41, 0x24) in bus.writes  # усреднение
    assert (0x0E, 0x0A, 0x01) in bus.writes  # single measurement
    x, y, z = drv.read()
    assert (x, y, z) == pytest.approx((30.0, -6.0, 45.0), abs=0.2)


def test_detect_absent() -> None:
    assert cm.detect(FakeIST8310Bus(present=False), "auto") is None
    assert cm.detect(FakeIST8310Bus(), "qmc5883l") is None


def test_axes() -> None:
    assert cm.parse_axes("x,-y,-z") == [(0, 1.0), (1, -1.0), (2, -1.0)]
    assert cm.parse_axes("-y, x, z") == [(1, -1.0), (0, 1.0), (2, 1.0)]
    for bad in ("x,y", "x,x,z", "x,y,w"):
        with pytest.raises(ValueError):
            cm.parse_axes(bad)
    assert cm.map_axes((1.0, 2.0, 3.0), cm.parse_axes("y,-x,-z")) == (2.0, -1.0, -3.0)
    assert cm.map_axes((1.0, 2.0, 3.0), cm.axes_for("ist8310")) == (-1.0, -2.0, -3.0)


def test_axes_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FNAV_COMPASS_AXES", "-x,-y,z")
    assert cm.axes_for("ist8310") == [(0, -1.0), (1, -1.0), (2, 1.0)]
    monkeypatch.setenv("FNAV_COMPASS_AXES", "garbage")
    assert cm.axes_for("ist8310") == cm.parse_axes(cm.AXES["ist8310"])


def _run_reader(bus_factory, seconds: float = 0.3):
    samples: list = []
    errors: list = []
    r = cm.CompassReader(lambda: ("auto", 1, None, 50), lambda v, c: samples.append((v, c)), errors.append,
                         bus_opener=bus_factory)
    r.start()
    time.sleep(seconds)
    r.stop()
    r.join(2)
    assert not r.is_alive()
    return samples, errors


def test_reader_reads_samples() -> None:
    samples, errors = _run_reader(lambda n: FakeIST8310Bus())
    assert len(samples) >= 5
    (_x, _y, z), chip = samples[-1]
    assert chip == "ist8310" and z == pytest.approx(-45.0, abs=0.2)  # z вверх → FRD вниз
    assert errors and errors[-1] is None


def test_reader_reports_not_connected() -> None:
    samples, errors = _run_reader(lambda n: FakeIST8310Bus(present=False))
    assert not samples
    assert errors and "not connected" in errors[0]


def test_reader_no_i2c_device() -> None:
    def opener(n: int):
        raise FileNotFoundError(2, "No such file or directory", f"/dev/i2c-{n}")

    samples, errors = _run_reader(opener)
    assert not samples and errors[0].startswith("Compass:")


def test_reader_recovers_after_unplug() -> None:
    bus = FakeIST8310Bus()
    samples: list = []
    errors: list = []
    r = cm.CompassReader(lambda: ("ist8310", 1, None, 100), lambda v, c: samples.append(v), errors.append,
                         bus_opener=lambda n: bus)
    r.start()
    try:
        time.sleep(0.15)
        bus.present = False
        time.sleep(0.3)
        assert any(e and "not responding" in e for e in errors)
        n = len(samples)
        bus.present = True
        deadline = time.time() + 5
        while time.time() < deadline and len(samples) <= n:
            time.sleep(0.05)
        assert len(samples) > n
    finally:
        r.stop()
        r.join(2)


def test_button_polarity_decision() -> None:
    p = decide_polarity(1, 1)  # модуль держит линию высоко → нажатие = низкий
    assert p.driven and p.active_low
    p = decide_polarity(0, 0)
    assert p.driven and not p.active_low
    p = decide_polarity(1, 0)  # линия «висит» → как Pixhawk: активный высокий, подтяжка вниз
    assert not p.driven and not p.active_low


def test_threading_lock_unused_guard() -> None:
    # CompassReader — daemon-поток, не мешает выходу интерпретатора
    r = cm.CompassReader(lambda: ("auto", 1, None, 10), lambda v, c: None, lambda e: None,
                         bus_opener=lambda n: FakeIST8310Bus())
    assert r.daemon and isinstance(r, threading.Thread)
