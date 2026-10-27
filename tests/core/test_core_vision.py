"""Камера на охране: события fnav-watch в сигнализации (без сирены), снимки к событиям, клиент /status и /arm."""

from __future__ import annotations

import json

import httpx
from test_core_alarm import make_alarm, settle

from fnav.core.vision import STALE_S, VisionClient
from fnav.models import AlarmSettings, HazardSettings, VisionSettings

JPEG = b"\xff\xd8snapshot\xff\xd9"


async def _snap() -> bytes:
    return JPEG


async def test_vision_event_no_siren_but_lights_protect_and_snapshot(tmp_path):
    protected: list = []

    async def protect():
        protected.append(1)
        return {"ok": True, "segment": "2026-10-27/x.ts"}

    svc, _, cfg = make_alarm(tmp_path, protect=protect, arm_delay_s=0, siren=True,
                             hazard=HazardSettings(enabled=True), vision=VisionSettings(enabled=True))
    svc.snapshot = _snap
    sirens: list = []

    async def siren_start(seconds):
        sirens.append(seconds)
        return True

    svc.siren.start = siren_start  # type: ignore[method-assign]
    await svc.start(background=False)
    assert not await svc.vision_event("approach", {"h": 0.4})  # не на охране
    await svc.arm()
    assert await svc.vision_event("approach", {"h": 0.4})
    await settle()
    ev = svc.events[0]
    assert ev.kind == "approach" and ev.title == "Person approaching"
    assert ev.data["segment"] == "2026-10-27/x.ts" and ev.data["snapshot"] is True
    assert sirens == [] and svc.hazard.active and protected == [1]
    assert svc.snapshot_path(ev.id).read_bytes() == JPEG
    assert svc.snapshot_path("../../etc/passwd") is None and svc.snapshot_path("0" * 32) is None

    cfg["s"] = cfg["s"].model_copy(update={"vision": VisionSettings(enabled=True, face=False)})
    assert not await svc.vision_event("face", {})  # лицо выключено
    assert not await svc.vision_event("impact", {})  # только approach / face
    cfg["s"] = cfg["s"].model_copy(update={"vision": VisionSettings(enabled=False)})
    assert not await svc.vision_event("approach", {})
    assert svc.status().vision.enabled is False

    await svc.clear_events()
    assert svc.snapshot_path(ev.id) is None
    await svc.stop()


class FakeWatch:
    def __init__(self) -> None:
        self.watching = False
        self.seq = 0
        self.events: list[dict] = []
        self.arms: list[bool] = []
        self.up = True

    def add(self, kind: str, ts: float) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "kind": kind, "ts": ts, "data": {"n": self.seq}})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=req)
        if req.url.path == "/status":
            return httpx.Response(200, json={"watching": self.watching, "seq": self.seq, "events": self.events,
                                             "people": 1 if self.watching else 0, "error": None})
        if req.url.path == "/arm":
            self.watching = json.loads(req.content)["watching"]
            self.arms.append(self.watching)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


async def test_client_arms_the_watcher_and_forwards_new_events():
    now = [1000.0]
    cfg = {"v": VisionSettings(enabled=True)}
    armed = [False]
    got: list = []

    async def on_event(kind, data):
        got.append((kind, data["n"]))

    w = FakeWatch()
    w.add("approach", now[0] - STALE_S - 5)  # старое — не тревога
    c = VisionClient(get_settings=lambda: cfg["v"], get_armed=lambda: armed[0], on_event=on_event,
                     transport=httpx.MockTransport(w), clock=lambda: now[0])
    await c.poll()
    assert c.available and not c.watching and w.arms == [] and got == []
    armed[0] = True
    await c.poll()
    assert w.arms == [True] and c.status().watching
    w.add("approach", now[0])
    w.add("face", now[0])
    await c.poll()
    assert got == [("approach", 2), ("face", 3)]
    await c.poll()
    assert len(got) == 2  # не повторяет

    w.seq, w.events, w.watching = 0, [], False  # fnav-watch перезапустился
    await c.poll()
    assert w.arms == [True, True]
    w.add("face", now[0])
    await c.poll()
    assert got[-1] == ("face", 1)

    w.up = False
    await c.poll()
    assert not c.available and "not responding" in (c.status().error or "")
    w.up = True
    cfg["v"] = VisionSettings(enabled=False)  # выключили — сказать сервису «не смотри» и больше не опрашивать
    await c.poll()
    assert w.arms[-1] is False and not c.watching
    w.up = False
    await c.poll()
    assert c.status().error is None
    await c.stop()


async def test_alarm_status_reports_vision_off_by_default(tmp_path):
    svc, _, _ = make_alarm(tmp_path)
    st = svc.status()
    assert st.vision.enabled is False and st.vision.available is False
    assert AlarmSettings().vision.enabled is False


async def test_loiter_event_texts_and_switch(tmp_path):
    """«Стоит у фургона» (loiter): без сирены, текст с секундами, свой выключатель alarm.vision.loiter."""
    svc, _, cfg = make_alarm(tmp_path, arm_delay_s=0, siren=True, vision=VisionSettings(enabled=True))
    svc.snapshot = _snap
    sirens: list = []

    async def siren_start(seconds):
        sirens.append(seconds)
        return True

    svc.siren.start = siren_start  # type: ignore[method-assign]
    await svc.start(background=False)
    await svc.arm()
    assert await svc.vision_event("loiter", {"held_s": 8.2, "source": "motion"})
    await settle()
    ev = svc.events[0]
    assert ev.kind == "loiter" and ev.title == "Someone is lingering at the van"
    assert ev.body.startswith("A person has stayed close to the van for 8 s at ")
    assert sirens == []
    cfg["s"] = cfg["s"].model_copy(update={"vision": VisionSettings(enabled=True, loiter=False)})
    assert not await svc.vision_event("loiter", {"held_s": 9})
    await svc.stop()


async def test_client_forwards_loiter():
    got: list = []

    async def on_event(kind, data):
        got.append(kind)

    w = FakeWatch()
    w.watching = True
    c = VisionClient(get_settings=lambda: VisionSettings(enabled=True), get_armed=lambda: True, on_event=on_event,
                     transport=httpx.MockTransport(w), clock=lambda: 1000.0)
    await c.poll()
    w.add("loiter", 1000.0)
    w.add("bogus", 1000.0)  # неизвестный вид — не тревога
    await c.poll()
    assert got == ["loiter"]
    await c.stop()
