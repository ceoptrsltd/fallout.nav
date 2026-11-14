"""Электростанции по BLE (fnav/core/power.py, daly.py, fossibot.py): протоколы и опрос."""

from __future__ import annotations

import asyncio

import pytest

from fnav.core import daly, fossibot
from fnav.core import power as pw
from fnav.core.ble import BleError
from fnav.models import PowerSettings

# ответ Daly с устройства 2026-11-14 (6S, 20.8 В, SOC 9 %, 26/25 °C) — собран из записанных значений
CELLS = [3492, 3487, 3434, 3490, 3494, 3486]


def daly_frame(regs: dict[int, int]) -> bytes:
    r = [0] * daly.REGS
    for k, v in regs.items():
        r[k] = v
    body = bytes([0xD2, 0x03, 2 * daly.REGS]) + b"".join(v.to_bytes(2, "big") for v in r)
    c = daly.crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


def test_requests_match_what_the_devices_answer():
    assert daly.REQUEST.hex(" ") == "d2 03 00 00 00 3e d7 b9"
    assert fossibot.REQUEST.hex(" ") == "11 04 00 00 00 50 a6 f2"


def test_daly_frame_is_glued_from_pieces_and_parsed():
    regs = {i: v for i, v in enumerate(CELLS)} | {32: 66, 33: 65, 34: 0xFF, 40: 208, 41: 30000, 42: 90, 43: 3494,
                                                     44: 3434, 49: 6, 50: 2, 56: 60}
    frame = daly_frame(regs)
    f = daly.Frames()
    assert f.feed(b"\x00\x99" + frame[:20]) is None  # мусор до начала кадра и первый кусок
    assert f.feed(frame[20:100]) is None
    got = f.feed(frame[100:])
    assert got == frame
    r = daly.parse(got)
    assert r is not None
    assert (r.voltage_v, r.current_a, r.soc_pct, r.cells_mv, r.temps_c, r.delta_mv) == (20.8, 0.0, 9.0, CELLS,
                                                                                        [26, 25], 60)


def test_broken_frames_are_rejected():
    frame = bytearray(daly_frame({40: 208, 49: 6}))
    frame[50] ^= 1
    assert daly.parse(bytes(frame)) is None
    assert daly.Frames().feed(bytes(frame)) is None
    fb = bytearray(pw.fake_fossibot(91.3))
    assert fossibot.parse(bytes(fb)) is not None
    fb[100] ^= 1
    assert fossibot.parse(bytes(fb)) is None


def test_fossibot_reading():
    frame = pw.fake_fossibot(91.3, input_w=350)
    f = fossibot.Frames()
    assert f.feed(frame[:100]) is None
    r = fossibot.parse(f.feed(frame[100:]))
    assert r is not None and r.soc_pct == 91.3 and r.charging and r.input_w == 350 and r.ac_out_v == 230.0


class ScriptLink:
    """Подключение удаётся с n-й попытки; станция отвечает кадрами FakeLink."""

    tries = 0

    def __init__(self, proto, ok_on: int, mac: str, cb) -> None:
        self.fake = pw.FakeLink(proto, mac, cb)
        self.ok_on = ok_on
        self.on_lost = None

    @property
    def connected(self) -> bool:
        return self.fake.connected

    async def connect(self, subscribe, need) -> None:
        ScriptLink.tries += 1
        if ScriptLink.tries < self.ok_on:
            raise BleError("Connect: Failed le-connection-abort-by-local")
        self.fake.connected = True

    async def write(self, uuid, data, response=False) -> None:
        await self.fake.write(uuid, data, response)

    async def disconnect(self) -> None:
        self.fake.connected = False

    async def close(self) -> None:
        self.fake.connected = False


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(pw, "CONNECT_RETRY_S", 0.01)
    monkeypatch.setattr(pw, "POLL_S", 0.05)
    ScriptLink.tries = 0


async def _until(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end
        await asyncio.sleep(0.01)


async def test_failed_connects_are_retried_and_the_readings_reach_the_screen(fast):
    s = PowerSettings(main_mac="70:AF:09:06:E9:1A", second_mac="74:c1:4e:10:00:9b")
    assert s.second_mac == "74:C1:4E:10:00:9B"
    svc = pw.PowerService(lambda: s, link_factory=None)
    svc.main.link_factory = lambda mac, cb: ScriptLink(pw.FOSSIBOT, 4, mac, cb)
    svc.second.link_factory = lambda mac, cb: ScriptLink(pw.DALY, 1, mac, cb)
    await svc.start()
    try:
        await _until(lambda: svc.main.fresh() is not None and svc.second.fresh() is not None)
        t = svc.telemetry()
        assert t is not None and t.main.soc_pct is not None and t.main.voltage_v is None
        assert t.second.voltage_v is not None and t.third.voltage_v is None
        st = svc.status()
        assert [x.slot for x in st.stations] == ["main", "second", "third"] and st.stations[0].connected
        assert st.stations[1].reading["cells_mv"]
    finally:
        await svc.stop()


async def test_no_connect_while_the_rear_view_is_on_and_stale_readings_vanish(fast):
    quiet = [True]
    r = pw.StationReader("second", pw.DALY, quiet=lambda: quiet[0],
                         link_factory=lambda mac, cb: ScriptLink(pw.DALY, 1, mac, cb))
    await r.set_mac("74:C1:4E:10:00:9B")
    try:
        await asyncio.sleep(0.1)
        assert ScriptLink.tries == 0 and r.slot_view().voltage_v is None
        quiet[0] = False
        await _until(lambda: r.fresh() is not None)
        assert r.slot_view().low is False
        r.read_at -= pw.STALE_S + 1
        await r.stop()
        assert r.slot_view().voltage_v is None
    finally:
        await r.stop()


def test_low_charge_and_main_station_details():
    s = PowerSettings(show_on_screen=False)
    svc = pw.PowerService(lambda: s)
    assert svc.telemetry().main.soc_pct is None  # телеметрия идёт и без плашки на экране
    r = svc.main
    r.reading, r.read_at = fossibot.parse(pw.fake_fossibot(15.0)), r.mono()
    v = r.slot_view()
    assert v.low and v.soc_pct == 15.0 and v.output_w == 150 and v.to_empty_min == 960 and v.input_w == 0
    with pytest.raises(ValueError):
        PowerSettings(main_mac="nope")
