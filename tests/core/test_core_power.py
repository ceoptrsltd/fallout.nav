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
    monkeypatch.setattr(pw, "POLL_FAIL_S", 0.05)
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
        r.read_at -= pw.POLL_S + pw.STALE_S + 1
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


def test_lead_battery_module_frame_and_charge_estimate():
    from fnav.core import leadbatt

    f = leadbatt.frame(12.37, 13.8, 0x03, 3600)
    assert len(f) == leadbatt.FRAME_LEN and leadbatt.Frames().feed(f) == f
    r = leadbatt.parse(f)
    assert r is not None and r.voltage_v == 12.37 and r.input_v == 13.8 and r.input_on and r.charging
    assert r.soc_pct == 70.0 and r.uptime_s == 3600
    assert leadbatt.soc_from_voltage(11.0) == 0.0 and leadbatt.soc_from_voltage(13.5) == 100.0
    assert leadbatt.parse(f[:-1] + bytes([f[-1] ^ 1])) is None


async def test_stations_connect_one_at_a_time(fast):
    """Чип Pi ставит одно LE-подключение за раз: попытки станций не перекрываются."""
    busy = [0]
    peak = [0]

    class SlowLink(ScriptLink):
        async def connect(self, subscribe, need) -> None:
            busy[0] += 1
            peak[0] = max(peak[0], busy[0])
            await asyncio.sleep(0.05)
            busy[0] -= 1
            await super().connect(subscribe, need)

    s = PowerSettings()
    svc = pw.PowerService(lambda: s)
    for r, proto in ((svc.main, pw.FOSSIBOT), (svc.second, pw.DALY), (svc.third, pw.LEAD)):
        r.link_factory = lambda mac, cb, proto=proto: SlowLink(proto, 4, mac, cb)
    await svc.start()
    try:
        await _until(lambda: all(r.fresh() is not None for r in (svc.main, svc.second, svc.third)))
        assert peak[0] == 1
    finally:
        await svc.stop()


async def test_a_station_in_range_keeps_being_retried_in_bursts(fast, monkeypatch):
    """«abort-by-local» бывает десятки раз подряд: станция в эфире — повторы сериями, без долгих пауз."""
    monkeypatch.setattr(pw, "BURST_PAUSE_S", 0.05)
    r = pw.StationReader("third", pw.LEAD, link_factory=lambda mac, cb: ScriptLink(pw.LEAD, 50, mac, cb))
    await r.set_mac("10:BD:A3:B2:48:5A")
    try:
        await _until(lambda: r.fresh() is not None)
        assert ScriptLink.tries == 50
    finally:
        await r.stop()


# ── команды (только на имитации: ScriptLink записывает, что ушло бы станции) ──

class RecLink(ScriptLink):
    def __init__(self, proto, mac, cb) -> None:
        super().__init__(proto, 1, mac, cb)
        self.sent: list[tuple[str, bytes, bool]] = []

    async def write(self, uuid, data, response=False) -> None:
        self.sent.append((uuid, bytes(data), response))
        await super().write(uuid, data, response)


async def _service_with_links(fast):
    from fnav.models import PowerControl  # noqa: F401 — проверка импорта

    s = PowerSettings()
    svc = pw.PowerService(lambda: s)
    links = {}
    for r, proto in ((svc.main, pw.FOSSIBOT), (svc.second, pw.DALY), (svc.third, pw.LEAD)):
        def make(mac, cb, proto=proto, slot=r.slot):
            links[slot] = RecLink(proto, mac, cb)
            return links[slot]
        r.link_factory = make
    await svc.start()
    await _until(lambda: all(r.fresh() is not None for r in (svc.main, svc.second, svc.third)))
    return svc, links


def _commands(link, proto):
    return [d for u, d, _ in link.sent if d != proto.request]


async def test_commands_send_exactly_the_documented_frames(fast):
    from fnav.models import PowerControl

    svc, links = await _service_with_links(fast)
    try:
        await svc.control("main", PowerControl(output="ac", on=True))
        await svc.control("second", PowerControl(output="charge", on=False))
        await svc.control("second", PowerControl(output="discharge", on=True))
        await svc.control("third", PowerControl(output="charger", mode="auto"))
        assert _commands(links["main"], pw.FOSSIBOT) == [bytes.fromhex("11 06 00 1A 00 01 5D 6B")]
        assert links["main"].sent[-1][2] is True  # Fossibot: запись с подтверждением, как чтение
        # Daly: кадры из перехвата приложения Daly (batmon-ha #356)
        assert _commands(links["second"], pw.DALY) == [bytes.fromhex("D2 06 00 A5 00 00 8A 4A"),
                                                        bytes.fromhex("D2 06 00 A6 00 01 BB 8A")]
        assert _commands(links["third"], pw.LEAD) == [b"\x10"]
    finally:
        await svc.stop()


async def test_anything_else_is_refused(fast):
    from fnav.models import PowerControl

    svc, links = await _service_with_links(fast)
    try:
        for slot, cmd in (("main", PowerControl(output="charge", on=True)),  # у Fossibot нет MOSFET
                          ("second", PowerControl(output="ac", on=True)),
                          ("third", PowerControl(output="charger", on=True)),  # у свинцового — режим, не on
                          ("main", PowerControl(output="ac"))):
            with pytest.raises(pw.PowerError) as e:
                await svc.control(slot, cmd)
            assert e.value.status == 422
        with pytest.raises(pw.PowerError) as e:
            await svc.control("fourth", PowerControl(output="ac", on=True))
        assert e.value.status == 404
        await svc.second.stop()
        with pytest.raises(pw.PowerError) as e:
            await svc.control("second", PowerControl(output="charge", on=True))
        assert e.value.status == 409
        assert all(_commands(links[k], p) == [] for k, p in (("main", pw.FOSSIBOT), ("third", pw.LEAD)))
    finally:
        await svc.stop()


async def test_alarm_flashes_the_light_and_puts_it_back(fast, monkeypatch):
    monkeypatch.setattr(pw, "LIGHT_BLINK_S", 0.02)
    svc, links = await _service_with_links(fast)
    try:
        svc.flash_light(0.1)
        await asyncio.sleep(0.2)
        light = [d for d in _commands(links["main"], pw.FOSSIBOT)]
        on, off = fossibot.output_request("light", True), fossibot.output_request("light", False)
        assert len(light) >= 4 and set(light) == {on, off}
        assert light[0] == on and light[-1] == off  # был выключен — выключен и остался
        links["main"].sent.clear()
        svc.flash_light(5)
        await asyncio.sleep(0.05)
        await svc.stop_light()  # охрану сняли
        assert _commands(links["main"], pw.FOSSIBOT)[-1] == off
        n = len(links["main"].sent)
        await asyncio.sleep(0.1)
        assert len(links["main"].sent) == n  # больше не мигает
        s2 = PowerSettings(light_on_alarm=False)
        svc.get_settings = lambda: s2
        svc.flash_light(5)
        assert svc._light_task is None or svc._light_task.done()
    finally:
        await svc.stop()


def test_readings_carry_the_switch_states():
    r = daly.parse(daly_frame({40: 208, 49: 6, 52: 0, 53: 1, 54: 0}))
    assert r.charge_on is True and r.discharge_on is False and r.balancing is False
    st = pw.StationReader("second", pw.DALY)
    st.reading, st.read_at = daly.parse(daly_frame({40: 208, 42: 500, 49: 6, 56: 60})), st.mono()
    assert (st.slot_view().cell_delta_mv, st.slot_view().cell_delta_bad) == (60, False)
    assert st.slot_view().balancing is False
    assert st.slot_view().cells_mv == list(st.reading.cells_mv)
    st.reading = daly.parse(daly_frame({40: 208, 42: 500, 49: 6, 52: 1}))
    assert st.slot_view().balancing is True
    st.reading = daly.parse(daly_frame({40: 208, 42: 500, 49: 6, 56: 140}))
    assert st.slot_view().cell_delta_bad
    body = bytearray(pw.fake_fossibot(80.0))
    body[6 + 2 * 41:8 + 2 * 41] = (0x0AA4).to_bytes(2, "big")  # как на нашей станции: AC и USB
    c = daly.crc16(bytes(body[:-2]))
    body[-2:] = bytes([c >> 8, c & 0xFF])
    f = fossibot.parse(bytes(body))
    assert f.outputs == {"usb": True, "dc": False, "ac": True, "light": False}
    from fnav.core import leadbatt
    assert leadbatt.parse(leadbatt.frame(12.6, 0, 0b1010)).mode == "off"
    assert leadbatt.parse(leadbatt.frame(12.6, 14, 0b0111)).mode == "on"


async def test_a_station_released_for_its_phone_app_lets_go_and_comes_back(fast):
    from fnav.core.power import PowerError
    from fnav.models import PowerControl, PowerRelease

    svc, links = await _service_with_links(fast)
    try:
        st = svc.release("second", PowerRelease())  # power.release_min, 15 мин
        assert 14 * 60 < st.stations[1].released_s <= 15 * 60
        await _until(lambda: not svc.second.connected)
        assert svc.telemetry().second.released_s and svc.telemetry().second.voltage_v is None, "no data while released"
        sent = len(links["second"].sent)
        await asyncio.sleep(0.2)  # опрос (POLL_S 0.05) не подключается и не читает
        assert not svc.second.connected and len(links["second"].sent) == sent
        with pytest.raises(PowerError) as e:
            await svc.control("second", PowerControl(output="charge", on=True))
        assert e.value.status == 409
        assert svc.main.connected and svc.third.connected, "the others stay"

        svc.release("second", PowerRelease(minutes=0))  # «подключиться сейчас»
        await _until(lambda: svc.second.connected and svc.telemetry().second.voltage_v is not None)
        assert svc.status().stations[1].released_s is None

        svc.second.release(0.5)  # время выходит само
        await _until(lambda: not svc.second.connected)
        await _until(lambda: svc.second.connected)
        with pytest.raises(PowerError) as e:
            svc.release("nope", PowerRelease())
        assert e.value.status == 404
    finally:
        await svc.stop()


async def test_reconnect_now_cuts_the_pause_between_failed_tries(fast, monkeypatch):
    from fnav.models import PowerRelease

    monkeypatch.setattr(pw, "AWAY_BACKOFF_S", (60.0,))
    away = [True]

    class Away(ScriptLink):
        async def connect(self, subscribe, need) -> None:
            if away[0]:
                raise BleError("Device not found nearby")
            self.fake.connected = True

    s = PowerSettings(main_mac="", second_mac="74:C1:4E:10:00:9B", third_mac="")
    svc = pw.PowerService(lambda: s)
    svc.second.link_factory = lambda mac, cb: Away(pw.DALY, 1, mac, cb)
    await svc.start()
    try:
        await _until(lambda: svc.second.error is not None)  # теперь — пауза 60 с
        away[0] = False
        svc.release("second", PowerRelease(minutes=0))
        await _until(lambda: svc.second.connected)
    finally:
        await svc.stop()


async def test_after_a_connect_the_link_is_slowed_down_and_a_failure_of_that_is_not_fatal(fast):
    calls = []

    async def slow(fast):
        calls.append(fast)
        raise BleError("no sudo")

    s = PowerSettings(main_mac="", second_mac="74:C1:4E:10:00:9B", third_mac="")
    svc = pw.PowerService(lambda: s, tune=slow)
    svc.second.link_factory = lambda mac, cb: ScriptLink(pw.DALY, 1, mac, cb)
    await svc.start()
    try:
        await _until(lambda: svc.second.fresh() is not None)
        assert calls == [False], "once per connect, slow"
    finally:
        await svc.stop()


async def test_while_music_plays_the_stations_are_read_rarely_and_retried_rarely(fast, monkeypatch):
    monkeypatch.setattr(pw, "POLL_MUSIC_S", 30.0)
    monkeypatch.setattr(pw, "MUSIC_RETRY_S", 30.0)
    music = [True]
    s = PowerSettings(main_mac="", second_mac="74:C1:4E:10:00:9B", third_mac="")
    svc = pw.PowerService(lambda: s, busy=lambda: music[0])
    links = []

    def make(mac, cb):
        links.append(RecLink(pw.DALY, mac, cb))
        return links[-1]
    svc.second.link_factory = make
    await svc.start()
    try:
        await _until(lambda: svc.second.fresh() is not None)
        await asyncio.sleep(0.3)  # POLL_S 0.05 дал бы ~6 чтений
        assert len(links[0].sent) == 1, "one read, the next in POLL_MUSIC_S"
        assert svc.second.poll_s() == 30.0
        music[0] = False
        assert svc.second.poll_s() == pw.POLL_S
    finally:
        await svc.stop()

    ScriptLink.tries = 0
    music[0] = True
    svc = pw.PowerService(lambda: s, busy=lambda: music[0])
    svc.second.link_factory = lambda mac, cb: ScriptLink(pw.DALY, 99, mac, cb)  # не подключается
    await svc.start()
    try:
        await _until(lambda: svc.second.error is not None)
        await asyncio.sleep(0.3)  # CONNECT_RETRY_S 0.01 дал бы десятки попыток
        assert ScriptLink.tries == 1, "no bursts while the music plays"
    finally:
        await svc.stop()


async def test_alarm_on_keeps_the_main_station_ready_and_the_light_gets_a_fast_link(fast, monkeypatch):
    from fnav.core.power import PowerError
    from fnav.models import PowerRelease

    monkeypatch.setattr(pw, "LIGHT_BLINK_S", 0.03)
    monkeypatch.setattr(pw, "LIGHT_WAIT_S", 0.02)
    monkeypatch.setattr(pw, "RELEASE_CHECK_S", 0.05)
    monkeypatch.setattr(pw, "AFTER_COMMAND_S", 0.01)
    armed = [False]
    tuned = []

    async def tune(fast_):
        tuned.append(fast_)

    s = PowerSettings(second_mac="", third_mac="")
    svc = pw.PowerService(lambda: s, tune=tune, busy=lambda: True, guard=lambda: armed[0])
    links = []

    def make(mac, cb):
        links.append(RecLink(pw.FOSSIBOT, mac, cb))
        return links[-1]
    svc.main.link_factory = make
    await svc.start()
    try:
        await _until(lambda: svc.main.fresh() is not None)
        assert svc.main.busy(), "music: retries are rare"
        svc.release("main", PowerRelease(minutes=10))
        await _until(lambda: not svc.main.connected)
        armed[0] = True  # охрану включили: станция возвращается сама, отпустить её нельзя
        await _until(lambda: svc.main.connected)
        assert not svc.main.busy(), "alarm on: no music pauses between tries"
        with pytest.raises(PowerError) as e:
            svc.release("main", PowerRelease())
        assert e.value.status == 409

        # тревога в момент, когда связи нет: мигание дожидается подключения
        await links[-1].disconnect()
        tuned.clear()
        svc.flash_light(0.4)
        await _until(lambda: svc._light_task is None, timeout=5)
        cmds = _commands(links[-1], pw.FOSSIBOT)
        light_on, light_off = fossibot.output_request("light", True), fossibot.output_request("light", False)
        assert len(cmds) >= 3 and set(cmds) <= {light_on, light_off}, "only the light register is written"
        assert cmds[-1] == light_off, "put back as it was"
        assert True in tuned and tuned[-1] is False, "fast while flashing, slow again after"
    finally:
        await svc.stop()
