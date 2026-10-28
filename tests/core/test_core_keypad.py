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


def test_hold():
    k6 = kp.KEY_F13 + 5
    svc = kp.KeypadService({})
    # a key with a hold action: a short press fires on release
    assert svc.decode(ev(k6, 1), now=10.0) == [] and svc.held(now=10.3) == []
    assert svc.decode(ev(k6, 2) + ev(k6, 0), now=10.4) == ["shuffle"]
    # held: the hold action fires once while the key is still down, the release does nothing
    assert svc.decode(ev(k6, 1), now=20.0) == []
    assert svc.held(now=20.5) == []
    assert svc.held(now=20.0 + kp.HOLD_S) == ["favorite"]
    assert svc.held(now=22.0) == []
    assert svc.decode(ev(k6, 0), now=22.1) == []
    # press and release in one read (how the pad might send it): a press
    assert svc.decode(ev(k6, 1) + ev(k6, 0), now=30.0) == ["shuffle"]
    # a key without a hold action still fires at once
    assert svc.decode(ev(kp.KEY_F13, 1), now=40.0) == ["station_prev"]
    assert svc.decode(ev(kp.KEY_F13, 0), now=45.0) == []


async def test_dispatch_default_layout():
    calls: list[str] = []

    def h(name):
        async def fn():
            calls.append(name)
        return fn

    svc = kp.KeypadService({a["id"]: h(a["id"]) for a in kp.ACTIONS})
    for code in sorted(kp.PLACES):
        for act in svc.decode(ev(code, 1) + ev(code, 0)):
            await svc.dispatch(act)
    assert calls == ["radio_toggle", "station_prev", "station_next", "view_toggle", "camera_switch", "protect",
                     "shuffle", "volume_down", "volume_up"]
    await kp.KeypadService({}).dispatch("shuffle")  # no handler — nothing happens


def test_custom_layout():
    layout = KeypadSettings(key1="radio_toggle", key6="none", knob_press="protect")
    svc = kp.KeypadService({}, get_bindings=lambda: layout)
    assert svc.decode(ev(kp.KEY_F13, 1) + ev(kp.KEY_F13 + 5, 1) + ev(kp.KEY_ENTER, 1)) == ["radio_toggle", "protect"]


def test_favorite_hold_follows_the_mode_key():
    assert KeypadSettings().key6_hold == "favorite"
    # a layout saved before holds existed: the radio mode moved to key 3 -> holding key 3 saves the station
    moved = KeypadSettings.model_validate({"key3": "shuffle", "key6": "view_toggle"})
    assert moved.key3_hold == "favorite" and moved.key6_hold == "none"
    # holds already chosen: left alone
    assert KeypadSettings.model_validate({"key3": "shuffle", "key3_hold": "none"}).key3_hold == "none"


async def test_handler_error_does_not_raise():
    async def boom():
        raise RuntimeError("x")

    await kp.KeypadService({"protect": boom}).dispatch("protect")


async def test_key6_cycles_the_radio_mode(data_dir):
    async with running_app() as c:
        core = c.app.state.core
        mode = lambda: core.store.get().radio.mode
        assert mode() == "order"
        await core.keypad.dispatch("shuffle")
        assert mode() == "random" and core.radio.status().mode == "random"
        await core.keypad.dispatch("shuffle")
        assert mode() == "order"  # no favorites yet: skipped
        await core.radio.stop_playback()  # --mock starts with the demo station playing
        await core.keypad.dispatch("favorite")  # the radio is off: nothing
        assert core.radio.favorites == []
        await core.radio.play(RadioStation(uuid="a", name="A", url="http://x/a"))
        await core.keypad.dispatch("favorite")
        assert [s.uuid for s in core.radio.favorites] == ["a"] and core.radio.status().fav_seq == 1
        await core.keypad.dispatch("shuffle")
        await core.keypad.dispatch("shuffle")
        assert mode() == "favorites"
        await core.keypad.dispatch("shuffle")
        assert mode() == "order"


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
    holds = [p + "_hold" for p in kp.HOLD_PLACES]
    assert sorted([*kp.PLACES.values(), *holds]) == sorted(KeypadSettings.model_fields)
    assert sorted(kp.PLACES.values()) == sorted(kp.PLACE_LABELS) and set(kp.HOLD_PLACES) <= set(kp.PLACE_LABELS)


async def test_controls_api_and_remap(data_dir):
    async with running_app() as c:
        await login(c)
        r = (await c.get("/api/controls")).json()
        kpd = r["keypad"]
        assert kpd["connected"] is False and len(kpd["places"]) == 9 and kpd["defaults"]["key6"] == "shuffle"
        assert {p["place"]: p["action"] for p in kpd["places"]} == {k: v for k, v in kpd["defaults"].items()
                                                                     if not k.endswith("_hold")}
        assert {p["place"]: p["hold"] for p in kpd["places"]}["key6"] == "favorite" == kpd["defaults"]["key6_hold"]
        assert next(p for p in kpd["places"] if p["place"] == "knob_left")["hold"] is None
        assert r["gamepad"]["controls"] and r["button"]["controls"]
        # remap one key through the settings: the pad follows at once
        assert (await c.patch("/api/settings", json={"keypad": {"key6": "radio_toggle"}})).status_code == 200
        core = c.app.state.core
        assert core.keypad.decode(ev(kp.KEY_F13 + 5, 1) + ev(kp.KEY_F13 + 5, 0)) == ["radio_toggle"]
        # without a hold action the key fires on the press itself
        assert (await c.patch("/api/settings", json={"keypad": {"key6_hold": "none"}})).status_code == 200
        assert core.keypad.decode(ev(kp.KEY_F13 + 5, 1)) == ["radio_toggle"]
        places = (await c.get("/api/controls")).json()["keypad"]["places"]
        assert next(p for p in places if p["place"] == "key6")["action"] == "radio_toggle"
        assert (await c.patch("/api/settings", json={"keypad": {"key1": "self_destruct"}})).status_code == 422
