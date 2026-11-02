from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from sensors_fakes import FakeGpsd, FakeIST8310Bus

from fnav.models import Settings
from fnav.sensors import compass as cm
from fnav.sensors import m8n, selftest
from fnav.sensors.declination import declination_deg

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


def _fake_configure(calls: list[dict]):
    def fake(*args, **kw):
        calls.append({"args": args, **kw})
        return {"ok": True, "steps": [], "warnings": []}

    return fake


def test_ubx_config_tool_takes_static_hold_and_pvt_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """B21: Static Hold выключен (0) в настройках — инструмент не пишет во flash заводские 50 см/с / 20 м."""
    import ubx_config

    s = Settings()
    s.gps.static_hold_cm_s, s.gps.static_hold_max_dist_m = 0, 7
    calls: list[dict] = []
    monkeypatch.setattr(ubx_config, "load_settings", lambda: s)
    monkeypatch.setattr(m8n, "configure_receiver", _fake_configure(calls))
    assert ubx_config.main(["--no-gpsd"]) == 0
    c = calls[-1]
    assert c["static_hold_cm_s"] == 0 and c["static_hold_max_dist_m"] == 7
    assert c["enable_pvt"] is True and c["manage_gpsd"] is False
    assert c["args"][:2] == ("/dev/gps0", 115200)
    assert ubx_config.main(["--no-gpsd", "--static-hold", "30", "--pvt", "keep"]) == 0
    assert calls[-1]["static_hold_cm_s"] == 30 and calls[-1]["enable_pvt"] is None
    with pytest.raises(SystemExit):
        ubx_config.main(["--baud", "100000"])  # модуль на такой скорости потом не найти


def _compass_out(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, rep: selftest.Report) -> str:
    monkeypatch.setattr(cm, "open_bus", lambda n: FakeIST8310Bus(field=(15.0, 0.0, 50.0)))
    s = Settings()
    s.compass.rate_hz = 100
    selftest.check_compass(rep, s, n=10)
    return capsys.readouterr().out


def test_selftest_declination_auto_from_last_known_position(tmp_path, monkeypatch, capsys) -> None:
    """declination auto: склонение по последней позиции хаба (sensors_state.json), а не по Москве; нет — неизвестно."""
    monkeypatch.setenv("FNAV_DATA_DIR", str(tmp_path))
    out = _compass_out(monkeypatch, capsys, selftest.Report())
    assert "true unknown (declination auto" in out and "decl " not in out
    (tmp_path / "sensors_state.json").write_text(json.dumps({"lat": 52.2, "lon": 21.0, "autocal": {}}))
    assert selftest.last_known_position() == (52.2, 21.0)
    out = _compass_out(monkeypatch, capsys, selftest.Report())
    assert f"(decl {declination_deg(52.2, 21.0):.1f}," in out
    rep = selftest.Report()
    rep.position = (40.4, -3.7)  # фикс из проверки GPS важнее файла
    out = _compass_out(monkeypatch, capsys, rep)
    assert f"(decl {declination_deg(40.4, -3.7):.1f}," in out
    (tmp_path / "sensors_state.json").write_text("{broken")
    assert selftest.last_known_position() is None
