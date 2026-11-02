"""Мини-клавиатура: коды F13–F21 и Enter → места на клавиатуре → действия по settings.keypad."""

from __future__ import annotations

import asyncio
import os
import time
from typing import get_args

from core_helpers import login, running_app

from fnav.core import keypad as kp
from fnav.models import KeypadAction, KeypadSettings, RadioStation


def ev(code: int, value: int, t: float = 0.0) -> bytes:
    """An evdev event; t - its timestamp (0 - none: the time it was read counts)."""
    return kp.EVENT.pack(int(t), round((t % 1) * 1e6), kp.EV_KEY, code, value)


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
        # the GPS-module button switches the cameras (Core.handle_button), not the video output
        assert [b["title"] for b in r["button"]["controls"]] == ["Front / rear camera", "Protect recording"]
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


# ── B9: время нажатия — по меткам событий, действия — задачами по очередям ──


def test_hold_is_measured_by_the_event_timestamps():
    """After a long action the loop read press + release in one batch: a 1.5 s hold of key 6 used to become a
    short press (by the read time). The events' own timestamps decide."""
    k6 = kp.KEY_F13 + 5
    svc = kp.KeypadService({})
    assert svc.decode(ev(k6, 1, t=100.0) + ev(k6, 0, t=101.5), now=130.0) == ["favorite"]
    assert svc.decode(ev(k6, 1, t=200.0) + ev(k6, 0, t=200.2), now=230.0) == ["shuffle"]
    # pressed long ago by its timestamp, read only now: the hold is due at once
    assert svc.decode(ev(k6, 1, t=300.0), now=310.0) == [] and svc.held(now=310.0) == ["favorite"]
    assert svc.decode(ev(k6, 0, t=310.5), now=310.6) == []
    # pressed just now: the hold waits for its HOLD_S from the press itself
    assert svc.decode(ev(k6, 1, t=400.0), now=400.1) == [] and svc.held(now=400.5) == []
    assert svc.held(now=400.0 + kp.HOLD_S + 0.001) == ["favorite"]
    svc.decode(ev(k6, 0, t=401.0), now=401.0)
    # wall-clock timestamps (no CLOCK_MONOTONIC on the device): shifted by the offset to time.monotonic()
    wall = 1_700_000_000.0
    assert svc.decode(ev(k6, 1, t=wall), now=50.0, offset=50.0 - wall) == []
    assert svc.held(now=50.0 + kp.HOLD_S + 0.001) == ["favorite"]
    svc.decode(ev(k6, 0, t=wall + 1), now=51.0, offset=50.0 - wall)
    # a timestamp from the future (clock trouble): no later than the read time
    assert svc.decode(ev(k6, 1, t=900.0), now=500.0) == [] and svc.held(now=500.0 + kp.HOLD_S - 0.01) == []
    assert svc.held(now=500.0 + kp.HOLD_S + 0.001) == ["favorite"]


async def test_a_slow_radio_action_does_not_block_the_other_keys():
    """"Next station" may wait up to a minute for the radio-browser catalogue on a weak signal: meanwhile the knob
    volume, "protect recording" and the alarm still work; the radio keys wait their turn in the order pressed."""
    calls: list[str] = []
    gate = asyncio.Event()

    def h(name, wait=False):
        async def fn():
            if wait:
                await gate.wait()
            calls.append(name)
        return fn

    svc = kp.KeypadService({"station_next": h("station_next", wait=True), "radio_toggle": h("radio_toggle"),
                            "favorite": h("favorite"), "volume_up": h("volume_up"), "protect": h("protect"),
                            "alarm": h("alarm")})
    for act in ("station_next", "favorite", "volume_up", "protect", "radio_toggle", "alarm", "volume_up"):
        svc.submit(act)
    for _ in range(10):
        await asyncio.sleep(0)
    assert sorted(calls) == ["alarm", "protect", "volume_up", "volume_up"]
    gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert calls[4:] == ["station_next", "favorite", "radio_toggle"]
    assert svc._workers == {}
    # presses piling up behind a stuck one: at most QUEUE_MAX wait, the rest are dropped
    gate.clear()
    calls.clear()
    svc.submit("station_next")
    await asyncio.sleep(0)  # in progress
    for _ in range(kp.QUEUE_MAX + 5):
        svc.submit("station_next")
    gate.set()
    for _ in range(40):
        await asyncio.sleep(0)
    assert calls == ["station_next"] * (kp.QUEUE_MAX + 1)
    svc.submit("no_such_action")  # no handler: nothing
    assert "no_such_action" not in svc._queues
    await svc.stop()


async def test_reads_the_device_when_it_is_readable(monkeypatch):
    """The device is waited on with loop.add_reader (no 33 polls a second); a hold fires on a timer while the key
    is still down; an unplugged pad is dropped."""
    monkeypatch.setattr(kp, "HOLD_S", 0.05)
    calls: list[str] = []

    def h(name):
        async def fn():
            calls.append(name)
        return fn

    svc = kp.KeypadService({"station_prev": h("station_prev"), "favorite": h("favorite"), "shuffle": h("shuffle")})
    r, w = os.pipe()
    svc.fds["pad"] = r
    os.write(w, ev(kp.KEY_F13, 1, t=time.time()) + ev(kp.KEY_F13, 0, t=time.time()))
    svc._on_readable("pad")
    await asyncio.sleep(0.01)
    assert calls == ["station_prev"]
    os.write(w, ev(kp.KEY_F13 + 5, 1, t=time.time()))  # key 6 held down
    svc._on_readable("pad")
    await asyncio.sleep(0.15)
    assert calls == ["station_prev", "favorite"]
    os.write(w, ev(kp.KEY_F13 + 5, 0, t=time.time()))
    svc._on_readable("pad")
    await asyncio.sleep(0.01)
    assert calls == ["station_prev", "favorite"]  # the release after a hold does nothing
    os.write(w, ev(kp.KEY_F13 + 5, 1, t=time.time()))
    svc._on_readable("pad")
    os.close(w)  # unplugged with the key down
    svc._on_readable("pad")
    assert svc.fds == {} and svc._held == {} and svc._hold_timer is None
    await svc.stop()
