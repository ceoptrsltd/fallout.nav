from __future__ import annotations

import asyncio
import json
import math
import time

import pytest
from sensors_fakes import FakeGpsd

from fnav.interfaces import SensorHub
from fnav.models import Settings
from fnav.sensors.hub import RealSensorHub


def tpv(speed: float, track: float, lat: float = 55.7069, lon: float = 37.5853) -> str:
    return json.dumps({"class": "TPV", "mode": 3, "time": "2026-10-20T16:40:01.100Z", "lat": lat, "lon": lon,
                       "altMSL": 165.0, "speed": speed, "track": track})


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FNAV_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNAV_GPS_AUTOCONFIG", "0")


async def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def mag_vec(mag_heading: float, h: float = 15.0) -> tuple[float, float, float]:
    p = math.radians(mag_heading)
    return (h * math.cos(p), -h * math.sin(p), 50.0)


def test_protocol() -> None:
    assert isinstance(RealSensorHub(Settings(), enable_compass=False, enable_gpio=False), SensorHub)


async def test_gps_heading_when_fast() -> None:
    srv = FakeGpsd(lines=[tpv(12.0, 359.0)], nmea_lines=["$GNRMC,a*00"])
    await srv.start()
    hub = RealSensorHub(Settings(), gpsd_port=srv.port, enable_compass=False, enable_gpio=False)
    await hub.start()
    try:
        assert await wait_until(lambda: hub.snapshot().gps.fix == "3d" and hub.snapshot().heading.deg is not None)
        s = hub.snapshot()
        assert s.heading.source == "gps" and s.heading.deg == pytest.approx(359.0, abs=0.5)
        assert 11 < s.heading.declination_deg < 13
        assert s.errors == []
        assert await wait_until(lambda: hub.nmea_tail(5) == ["$GNRMC,a*00"])
    finally:
        await hub.stop()
        await srv.stop()


async def test_mag_heading_when_stopped_with_declination_and_offset() -> None:
    srv = FakeGpsd(lines=[tpv(0.1, 200.0)])
    await srv.start()
    st = Settings()
    st.compass.mount_offset_deg = 5.0
    hub = RealSensorHub(st, gpsd_port=srv.port, enable_compass=False, enable_gpio=False)
    await hub.start()
    try:
        assert await wait_until(lambda: hub.snapshot().gps.fix == "3d")
        for _ in range(30):
            hub._on_mag(mag_vec(350.0), "ist8310")
            await asyncio.sleep(0.02)
        s = hub.snapshot()
        decl = s.heading.declination_deg
        assert s.heading.source == "mag"
        assert s.heading.mag_deg == pytest.approx((350.0 + decl + 5.0) % 360, abs=0.2)
        assert s.compass is not None and s.compass.chip == "ist8310" and s.compass.ok
    finally:
        await hub.stop()
        await srv.stop()


async def test_no_gpsd_no_compass_reports_errors() -> None:
    probe = FakeGpsd()
    await probe.start()
    port = probe.port
    await probe.stop()
    hub = RealSensorHub(Settings(), gpsd_port=port, enable_compass=False, enable_gpio=False)
    await hub.start()
    try:
        assert await wait_until(lambda: any("gpsd: cannot connect" in e for e in hub.snapshot().errors))
        s = hub.snapshot()
        assert s.gps.fix == "none" and s.heading.source == "none" and s.heading.deg is None
        hub._on_compass_error("Compass: IST8310 not connected (nothing found on I2C-1)")
        assert await wait_until(lambda: any("not connected" in e for e in hub.snapshot().errors))
    finally:
        await hub.stop()


async def test_gps_receiver_absent() -> None:
    srv = FakeGpsd(devices=[])  # gpsd работает, но приёмника нет
    await srv.start()
    hub = RealSensorHub(Settings(), gpsd_port=srv.port, enable_compass=False, enable_gpio=False)
    await hub.start()
    try:
        assert await wait_until(lambda: any("receiver not connected" in e for e in hub.snapshot().errors))
    finally:
        await hub.stop()
        await srv.stop()


async def test_calibration_through_hub() -> None:
    hub = RealSensorHub(Settings(), gpsd_port=1, enable_compass=False, enable_gpio=False)
    hub.calibration_start()
    for i in range(360):
        x, y, z = mag_vec(float(i))
        hub._on_mag((1.1 * x + 7.0, 0.9 * y - 3.0, z), "ist8310")
    st = hub.calibration_status()
    assert st.active and st.samples > 150 and st.coverage_pct > 95  # близкие точки прореживаются
    cal = hub.calibration_finish()
    assert cal.quality > 0.9
    assert hub.settings.compass.calibration == cal
    assert not hub.calibration_status().active


async def test_button_callbacks_in_loop() -> None:
    hub = RealSensorHub(Settings(), gpsd_port=1, enable_compass=False, enable_gpio=False)
    await hub.start()
    got: list = []
    loop_thread: list = []
    import threading

    def cb(ev):
        got.append(ev)
        loop_thread.append(threading.current_thread())

    hub.on_button(cb)
    t = threading.Thread(target=lambda: (hub.gpio._pressed(), hub.gpio._released()))
    t.start()
    t.join()
    await wait_until(lambda: got == ["press"])
    hub.gpio._pressed()
    hub.gpio._held_cb()
    hub.gpio._released()
    await wait_until(lambda: got == ["press", "long_press"])
    assert got == ["press", "long_press"]
    assert loop_thread[0] is threading.current_thread()  # вызвано в потоке event loop
    hub.set_led("blink_fast")  # без железа — молча
    hub.beep("double")
    await hub.stop()


async def test_apply_settings_and_configure_guard() -> None:
    hub = RealSensorHub(Settings(), gpsd_port=1, enable_compass=False, enable_gpio=False)
    new = Settings()
    new.heading.smoothing_s = 0.0
    hub.apply_settings(new)
    assert hub.settings.heading.smoothing_s == 0.0
    hub._configuring = True
    r = await hub.gps_configure()
    assert r["ok"] is False and "already" in r["details"]
