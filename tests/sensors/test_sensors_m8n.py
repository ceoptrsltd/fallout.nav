from __future__ import annotations

from sensors_fakes import FakeM8N

from fnav.sensors import m8n, ubx


def make(dev: FakeM8N) -> m8n.M8NConfigurator:
    return m8n.M8NConfigurator(dev, clock=dev.clock, sleep=dev.sleep)


def test_rate_candidates() -> None:
    assert m8n.max_rate_for(["gps"]) == 18
    assert m8n.max_rate_for(["gps", "glonass", "galileo"]) == 10
    assert m8n.rate_candidates(10, ["gps", "glonass"]) == [10, 8, 5, 4, 2, 1]
    assert m8n.rate_candidates(18, ["gps", "glonass"])[0] == 10
    assert m8n.rate_candidates(18, ["gps"])[0] == 18


def test_detect_baud_factory_38400() -> None:
    dev = FakeM8N(baud=38400)
    c = make(dev)
    assert c.detect_baud(first=115200) == 38400


def test_full_configure_from_factory() -> None:
    dev = FakeM8N(baud=38400)
    res = make(dev).configure(baud=115200, rate_hz=10, constellations=["gps", "glonass", "galileo"])
    assert res["ok"], res
    assert res["baud_found"] == 38400 and res["baud"] == 115200
    assert dev.device_baud == 115200
    assert sorted(dev.majors()) == ["galileo", "glonass", "gps"]
    assert dev.resets == 1  # GNSS перезапущен после смены созвездий
    assert dev.nav5_dyn == 4
    assert res["rate_hz"] == 10 and abs(dev.rate_hz - 10) < 1e-9
    assert dev.saved
    # GSV/GSA раз в секунду, GLL выключен
    assert dev.msg_rates[(0xF0, ubx.NMEA_IDS["GSV"])] == 10
    assert dev.msg_rates[(0xF0, ubx.NMEA_IDS["GLL"])] == 0
    assert res["version"]["sw"].startswith("ROM CORE")
    assert not res["warnings"]


def test_already_configured_is_idempotent() -> None:
    dev = FakeM8N(baud=115200)
    c = make(dev)
    c.configure(baud=115200, rate_hz=5, constellations=["gps", "glonass"])
    resets = dev.resets
    res = make(dev).configure(baud=115200, rate_hz=5, constellations=["gps", "glonass"])
    assert res["ok"] and res["baud_found"] == 115200
    assert dev.resets == resets  # созвездия не менялись — перезапуска нет


def test_rate_reduced_when_module_cannot_keep_up() -> None:
    dev = FakeM8N(baud=115200, rate_cap=6.0)
    res = make(dev).configure(baud=115200, rate_hz=10, constellations=["gps", "glonass", "galileo"])
    assert res["ok"]
    assert res["rate_hz"] == 5
    assert any("reduced" in w for w in res["warnings"])
    assert res["rate_measured"][10] < 8.5


def test_rate_capped_by_constellation_limit() -> None:
    dev = FakeM8N(baud=115200)
    res = make(dev).configure(baud=115200, rate_hz=18, constellations=["gps", "glonass"])
    assert res["ok"] and res["rate_hz"] == 10
    assert any("not supported" in w for w in res["warnings"])


def test_galileo_nak_falls_back() -> None:
    dev = FakeM8N(baud=115200, galileo_ok=False)
    res = make(dev).configure(baud=115200, rate_hz=5, constellations=["gps", "glonass", "galileo"])
    assert res["ok"]
    assert res["constellations"] == ["gps", "glonass"]
    assert any("rejected" in w for w in res["warnings"])


def test_not_connected() -> None:
    dev = FakeM8N(alive=False)
    res = make(dev).configure()
    assert not res["ok"]
    assert "not connected" in res["error"]


def test_configure_receiver_port_error_without_gpsd() -> None:
    def bad_open(device: str, baud: int):
        raise OSError("no such device")

    res = m8n.configure_receiver("/dev/nope", manage_gpsd=False, open_port=bad_open)
    assert not res["ok"] and "cannot open" in res["error"]
