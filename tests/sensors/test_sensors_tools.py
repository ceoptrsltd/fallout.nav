from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from sensors_fakes import FakeGpsd, FakeIST8310Bus

from fnav.models import Settings
from fnav.sensors import compass as cm
from fnav.sensors import selftest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
i2c_scan = importlib.import_module("i2c_scan")


def test_i2c_scan_identifies_ist8310() -> None:
    bus = FakeIST8310Bus()
    assert i2c_scan.scan(bus) == [0x0E]
    assert "IST8310" in i2c_scan.identify(bus, 0x0E)
    assert i2c_scan.scan(FakeIST8310Bus(present=False)) == []


async def test_selftest_gpsd_no_receiver() -> None:
    srv = FakeGpsd(lines=[], devices=[{"class": "DEVICE", "path": "/dev/ttyAMA0", "bps": 38400}])
    await srv.start()
    try:
        r = await selftest._gpsd_collect("127.0.0.1", srv.port, 0.5)
        assert r["error"] is None and r["version"] == "3.25" and r["tpv"] == []
        assert r["devices"][0]["bps"] == 38400
    finally:
        await srv.stop()


async def test_selftest_gpsd_fix() -> None:
    tpv = json.dumps({"class": "TPV", "mode": 3, "lat": 55.7, "lon": 37.6, "speed": 0.1, "track": 3})
    srv = FakeGpsd(lines=[tpv] * 3)
    await srv.start()
    try:
        r = await selftest._gpsd_collect("127.0.0.1", srv.port, 0.5)
        assert len(r["tpv"]) == 3 and r["tpv"][-1][1]["mode"] == 3
    finally:
        await srv.stop()


def test_selftest_compass_fake_bus(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cm, "open_bus", lambda n: FakeIST8310Bus(field=(15.0, 0.0, -50.0)))
    s = Settings()
    s.compass.rate_hz = 100
    rep = selftest.Report()
    selftest.check_compass(rep, s, n=10)
    out = capsys.readouterr().out
    assert not rep.failed, out
    assert "WHO_AM_I = 0x10" in out and "ist8310" in out


def test_selftest_compass_not_connected(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cm, "open_bus", lambda n: FakeIST8310Bus(present=False))
    rep = selftest.Report()
    selftest.check_compass(rep, Settings(), n=10)
    assert rep.failed and "NOT CONNECTED" in capsys.readouterr().out


def test_ubx_config_tool_help() -> None:
    import ubx_config

    with pytest.raises(SystemExit) as e:
        ubx_config.main(["--help"])
    assert e.value.code == 0
