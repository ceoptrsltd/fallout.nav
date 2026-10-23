from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sensors_fakes import FakeGpsd

from fnav.models import GpsFix
from fnav.sensors.gpsd import GpsdClient, GpsState, NmeaRing, gnss_from_prn, parse_sky, parse_tpv

FIX = Path(__file__).parent / "fixtures" / "gpsd_m8n.jsonl"


def lines() -> list[str]:
    return [ln for ln in FIX.read_text(encoding="utf-8").splitlines() if ln.strip()]


def msgs() -> list[dict]:
    return [json.loads(ln) for ln in lines()]


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_parse_tpv_3d() -> None:
    tpv = next(m for m in msgs() if m["class"] == "TPV" and m["mode"] == 3)
    f = parse_tpv(tpv, GpsFix(hdop=0.9, sats_used=7, sats_visible=10))
    assert f.fix == "3d"
    assert abs(f.lat - 55.7069) < 1e-4 and abs(f.lon - 37.5853) < 1e-4
    assert f.alt_m == 165.456  # altMSL предпочтительнее
    assert f.speed_mps == 12.345 and f.course_deg == 359.5
    assert f.time_utc == "2026-10-20T16:40:01.100Z"
    assert f.hdop == 0.9 and f.sats_used == 7  # из SKY, сохраняются


def test_parse_tpv_no_fix() -> None:
    f = parse_tpv({"class": "TPV", "mode": 1, "time": "2026-10-20T16:40:01.000Z", "speed": 0.0, "track": 10})
    assert f.fix == "none" and f.lat is None and f.speed_mps is None and f.course_deg is None
    assert f.time_utc == "2026-10-20T16:40:01.000Z"


def test_parse_sky_satellites() -> None:
    sky = next(m for m in msgs() if m["class"] == "SKY" and "satellites" in m)
    r = parse_sky(sky)
    sats = r["satellites"]
    assert [s.gnss for s in sats] == ["gps", "gps", "glonass", "galileo", "sbas", "gps"]
    assert r["hdop"] == 0.94 and r["used"] == 4 and r["visible"] == 6
    assert sats[2].prn == 67 and sats[2].used and sats[2].snr == 41.0
    assert sats[5].el is None and sats[5].snr is None  # el=-91 / ss=0 — неизвестно


def test_gnss_from_prn() -> None:
    assert gnss_from_prn(7) == "gps"
    assert gnss_from_prn(70) == "glonass"
    assert gnss_from_prn(305) == "galileo"
    assert gnss_from_prn(120) == "sbas"
    assert gnss_from_prn(193) == "qzss"
    assert gnss_from_prn(210) == "beidou"


def test_state_sequence_and_staleness() -> None:
    clk = Clock()
    st = GpsState(clock=clk)
    for m in msgs():
        st.handle(m)
    f = st.current()
    # counts come from the SKY with the satellite list; the short SKY right after it only updates hdop
    assert f.fix == "3d" and f.hdop == 0.91 and f.sats_used == 4 and f.sats_visible == 6
    assert len(st.satellites) == 6  # SKY без satellites не стирает список
    assert st.device_bps == 115200 and st.version == "3.25"
    assert st.problem() is None
    clk.t += 5
    f = st.current()
    assert f.fix == "none" and f.age_s == 5.0
    assert "no data" in st.problem()


def test_state_reports_not_connected() -> None:
    clk = Clock()
    st = GpsState(clock=clk)
    st.on_connect()
    st.handle({"class": "DEVICES", "devices": []})
    assert "not connected" in st.problem()
    st2 = GpsState(clock=clk)
    st2.on_connect()
    st2.handle({"class": "DEVICES", "devices": [{"path": "/dev/ttyAMA0"}]})
    assert st2.problem() is None
    clk.t += 6
    assert "not connected" in st2.problem()  # устройство открыто, но данных нет


def test_nmea_ring() -> None:
    r = NmeaRing(500)
    for i in range(600):
        r.append(f"$X{i}")
    assert len(r.tail(1000)) == 500 and r.tail(2) == ["$X598", "$X599"] and r.tail(0) == []


async def test_client_json_and_reconnect() -> None:
    srv = FakeGpsd(lines=lines()[3:], close_after=True)
    await srv.start()
    st = GpsState()
    cli = GpsdClient("127.0.0.1", srv.port, on_message=st.handle, on_connect=st.on_connect,
                     reconnect_min_s=0.05)
    task = asyncio.create_task(cli.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if srv.connections >= 2 and st.fix.fix == "3d":
                break
        assert srv.connections >= 2  # сервер закрыл соединение → клиент переподключился
        assert st.fix.fix == "3d" and st.fix.speed_mps == 12.345
        assert srv.watches[0] == '?WATCH={"enable":true,"json":true};'
    finally:
        task.cancel()
        await srv.stop()


async def test_client_nmea_connection() -> None:
    srv = FakeGpsd(nmea_lines=["$GNRMC,1*00", "$GNGGA,2*00"])
    await srv.start()
    ring = NmeaRing()
    cli = GpsdClient("127.0.0.1", srv.port, nmea=True, on_line=ring.append)
    task = asyncio.create_task(cli.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(ring.tail()) >= 2:
                break
        assert ring.tail() == ["$GNRMC,1*00", "$GNGGA,2*00"]
        assert srv.watches[0] == '?WATCH={"enable":true,"nmea":true};'
    finally:
        task.cancel()
        await srv.stop()


async def test_client_no_server_then_server_appears() -> None:
    probe = FakeGpsd(lines=lines()[3:])
    await probe.start()
    port = probe.port
    await probe.stop()  # порт свободен — gpsd «не запущен»
    st = GpsState()
    cli = GpsdClient("127.0.0.1", port, on_message=st.handle, reconnect_min_s=0.05, reconnect_max_s=0.1)
    task = asyncio.create_task(cli.run())
    try:
        for _ in range(400):  # на Windows отказ в соединении приходит через ~2 с
            await asyncio.sleep(0.02)
            if cli.status and "cannot connect" in cli.status:
                break
        assert cli.status and "cannot connect" in cli.status
        srv = FakeGpsd(lines=lines()[3:])
        await srv.start(port)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if st.fix.fix == "3d":
                break
        assert cli.status is None and st.fix.fix == "3d"
        await srv.stop()
    finally:
        task.cancel()


def _sky_list(used: int, unused: int, t: str | None = None) -> dict:
    sats = [{"PRN": i + 1, "gnssid": 0, "used": i < used, "ss": 30} for i in range(used + unused)]
    m = {"class": "SKY", "nSat": used + unused, "uSat": used, "satellites": sats}
    if t:
        m["time"] = t
    return m


def test_sat_counts_from_the_full_gsv_cycle() -> None:
    """Live gpsd 3.25 + NEO-M8N (NMEA 4.10), as captured: every SKY is preceded by a TPV of the same epoch;
    short SKY (GSA) say uSat=12 at most; the list comes per constellation (25, then 38 20 ms later) once in 2 s.
    The screen must not flicker 12 / 19 / 25 / 38."""
    clk = Clock()
    st = GpsState(clock=clk)

    def tpv(t):
        return {"class": "TPV", "mode": 3, "lat": 1.0, "lon": 2.0, "time": t}

    st.handle(tpv("T0"))
    st.handle({"class": "SKY", "uSat": 12, "hdop": 0.8})  # no list yet: take what there is
    assert st.current().sats_used == 12
    for m in (tpv("T1"), {"class": "SKY", "uSat": 12}, tpv("T1"), _sky_list(13, 12, "T1"),
              tpv("T1"), _sky_list(19, 19, "T1")):
        st.handle(m)
        assert st.current().sats_used == 12  # the cycle is not over yet
    st.handle(tpv("T2"))  # next epoch: the last list of the cycle is the whole sky
    f = st.current()
    assert (f.sats_used, f.sats_visible, len(st.satellites)) == (19, 38, 38)
    for k in range(3, 12):
        clk.t += 0.2
        st.handle(tpv(f"T{k}"))
        st.handle({"class": "SKY", "uSat": 12, "hdop": 0.7})
        assert st.current().sats_used == 19
    assert st.current().hdop == 0.7
    # without epoch times the list is applied 0.5 s after it came
    st.handle(_sky_list(5, 5))
    assert st.current().sats_used == 19
    clk.t += 0.6
    assert st.current().sats_used == 5
    clk.t += 6  # no lists any more (GSV off): the short SKY count is used again
    st.handle({"class": "SKY", "uSat": 11})
    assert st.current().sats_used == 11
