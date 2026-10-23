"""Мини-клавиатура: коды F13–F21 и Enter → места на клавиатуре → действия по settings.keypad."""

from __future__ import annotations

from typing import get_args

from core_helpers import login, running_app

from fnav.core import keypad as kp
from fnav.models import KeypadAction, KeypadSettings, RadioStation


def ev(code: int, value: int) -> bytes:
    return kp.EVENT.pack(0, 0, kp.EV_KEY, code, value)


def test_decode_presses_only():
    svc = kp.KeypadService({})
    data = ev(kp.KEY_F13, 1) + ev(kp.KEY_F13, 2) + ev(kp.KEY_F13, 0) + ev(kp.KEY_F13 + 8, 1) + ev(30, 1)
    assert svc.decode(data) == ["station_prev", "volume_up"]  # автоповтор, отпускание и чужие клавиши — нет


async def test_dispatch_default_layout():
    calls: list[str] = []

    def h(name):
        async def fn():
            calls.append(name)
        return fn

    svc = kp.KeypadService({a["id"]: h(a["id"]) for a in kp.ACTIONS})
    for code in sorted(kp.PLACES):
        for act in svc.decode(ev(code, 1)):
            await svc.dispatch(act)
    assert calls == ["radio_toggle", "station_prev", "station_next", "view_toggle", "video_toggle", "protect",
                     "shuffle", "volume_down", "volume_up"]
    await kp.KeypadService({}).dispatch("shuffle")  # no handler — nothing happens


def test_custom_layout():
    layout = KeypadSettings(key1="radio_toggle", key6="none", knob_press="protect")
    svc = kp.KeypadService({}, get_bindings=lambda: layout)
    assert svc.decode(ev(kp.KEY_F13, 1) + ev(kp.KEY_F13 + 5, 1) + ev(kp.KEY_ENTER, 1)) == ["radio_toggle", "protect"]


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


def test_actions_and_places_match_the_settings_model():
    assert sorted(a["id"] for a in kp.ACTIONS) == sorted(get_args(KeypadAction))
    assert sorted(kp.PLACES.values()) == sorted(KeypadSettings.model_fields) == sorted(kp.PLACE_LABELS)


async def test_controls_api_and_remap(data_dir):
    async with running_app() as c:
        await login(c)
        r = (await c.get("/api/controls")).json()
        kpd = r["keypad"]
        assert kpd["connected"] is False and len(kpd["places"]) == 9 and kpd["defaults"]["key6"] == "shuffle"
        assert {p["place"]: p["action"] for p in kpd["places"]} == kpd["defaults"]
        assert r["gamepad"]["controls"] and r["button"]["controls"]
        # remap one key through the settings: the pad follows at once
        assert (await c.patch("/api/settings", json={"keypad": {"key6": "radio_toggle"}})).status_code == 200
        core = c.app.state.core
        assert core.keypad.decode(ev(kp.KEY_F13 + 5, 1)) == ["radio_toggle"]
        places = (await c.get("/api/controls")).json()["keypad"]["places"]
        assert next(p for p in places if p["place"] == "key6")["action"] == "radio_toggle"
        assert (await c.patch("/api/settings", json={"keypad": {"key1": "self_destruct"}})).status_code == 422
