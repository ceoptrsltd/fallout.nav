"""Мини-клавиатура: коды F13–F21 → действия."""

from __future__ import annotations

from core_helpers import running_app

from fnav.core import keypad as kp
from fnav.models import RadioStation


def ev(code: int, value: int) -> bytes:
    return kp.EVENT.pack(0, 0, kp.EV_KEY, code, value)


def test_decode_presses_only():
    svc = kp.KeypadService({})
    data = ev(kp.KEY_F13, 1) + ev(kp.KEY_F13, 2) + ev(kp.KEY_F13, 0) + ev(kp.KEY_F13 + 8, 1) + ev(30, 1)
    assert svc.decode(data) == ["station_prev", "volume_up"]  # автоповтор, отпускание и чужие клавиши — нет


async def test_dispatch():
    calls: list[str] = []

    def h(name):
        async def fn():
            calls.append(name)
        return fn

    svc = kp.KeypadService({a: h(a) for a in kp.ACTIONS.values()})
    for code in sorted(kp.ACTIONS):
        for act in svc.decode(ev(code, 1)):
            await svc.dispatch(act)
    assert calls == ["radio_toggle", "station_prev", "station_next", "view_toggle", "video_toggle", "protect",
                     "shuffle", "volume_down", "volume_up"]
    await kp.KeypadService({}).dispatch("shuffle")  # no handler — nothing happens


async def test_handler_error_does_not_raise():
    async def boom():
        raise RuntimeError("x")

    await kp.KeypadService({"protect": boom}).dispatch("protect")


async def test_key6_toggles_shuffle(data_dir):
    async with running_app() as c:
        core = c.app.state.core
        assert core.store.get().radio.shuffle is False
        await core.keypad.dispatch("shuffle")
        assert core.store.get().radio.shuffle is True and core.radio.status().shuffle is True
        await core.keypad.dispatch("shuffle")
        assert core.store.get().radio.shuffle is False


async def test_knob_press_starts_and_stops_the_radio(data_dir):
    async with running_app() as c:
        core = c.app.state.core
        await core.radio.play(RadioStation(uuid="a", name="A", url="http://x/a"))
        await core.keypad.dispatch("radio_toggle")
        assert core.radio.want_play is False and core.radio.state().state == "stopped"
        await core.keypad.dispatch("radio_toggle")
        assert core.radio.want_play is True and core.radio.station.uuid == "a"
