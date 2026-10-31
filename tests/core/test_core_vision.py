"""Камера на охране: события fnav-watch в сигнализации (без сирены), снимки к событиям, клиент /status и /arm."""

from __future__ import annotations

import json

import httpx
from test_core_alarm import FakeRelay, make_alarm, settle

from fnav.core.alarm import make_event
from fnav.core.tracking import RelayConfig
from fnav.core.vision import STALE_S, VisionClient
from fnav.models import AlarmSettings, AlarmVisionCamera, HazardSettings, VisionSettings

JPEG = b"\xff\xd8snapshot\xff\xd9"


async def _snap(before_s: float = 0.0) -> bytes:
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
        self.cameras: dict | None = None  # новые поля /status; None — старый fnav-watch без них

    def add(self, kind: str, ts: float, **data) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "kind": kind, "ts": ts, "data": {"n": self.seq, **data}})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=req)
        if req.url.path == "/status":
            st = {"watching": self.watching, "seq": self.seq, "events": self.events,
                  "people": 1 if self.watching else 0, "error": None}
            if self.cameras is not None:
                st["cameras"] = self.cameras
            return httpx.Response(200, json=st)
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


# ── задняя камера ──
def test_rear_event_texts_front_unchanged():
    ts = 1_800_000_000.0
    front = {k: make_event(k, {"held_s": 9.4, "camera": "front"}, ts) for k in ("approach", "face", "loiter")}
    assert [front[k].title for k in ("approach", "face", "loiter")] == [
        "Person approaching", "Someone is looking in", "Someone is lingering at the van"]
    assert front["face"].body.startswith("A face close to the windshield at ")
    assert make_event("approach", {}, ts).title == "Person approaching"  # старый fnav-watch: без camera
    rear = {k: make_event(k, {"held_s": 9.4, "camera": "rear"}, ts) for k in ("approach", "face", "loiter")}
    assert rear["approach"].title == "Person approaching behind the van"
    assert rear["approach"].body.startswith("Someone came close to the back of the van at ")
    assert rear["face"].title == "Someone is looking into the rear camera"
    assert rear["face"].body.startswith("A face close to the rear camera at ")
    assert rear["loiter"].title == "Someone is lingering behind the van"
    assert rear["loiter"].body.startswith("A person has stayed close to the back of the van for 9 s at ")
    assert all(ev.kind == k and ev.url == "#/alarm" and ev.data["camera"] == "rear" for k, ev in rear.items())
    assert make_event("impact", {"peak_g": 0.3, "camera": "rear"}, ts).title == "Impact detected"  # не камера


async def test_rear_vision_event_no_siren_own_switch_and_own_push_cooldown(tmp_path):
    relay = FakeRelay()
    svc, clk, cfg = make_alarm(tmp_path, arm_delay_s=0, siren=True, push_cooldown_s=60,
                               relay=RelayConfig("http://relay", "tok"), transport=httpx.MockTransport(relay),
                               vision=VisionSettings(enabled=True))
    svc.snapshot = _snap
    sirens: list = []

    async def siren_start(seconds):
        sirens.append(seconds)
        return True

    svc.siren.start = siren_start  # type: ignore[method-assign]
    await svc.start(background=False)
    await svc.arm()
    assert await svc.vision_event("approach", {"track": 1, "camera": "front"})
    clk.tick(10)
    assert await svc.vision_event("approach", {"track": 2, "camera": "rear"})  # сзади — своя пауза push
    clk.tick(10)
    assert await svc.vision_event("approach", {"track": 3, "camera": "rear"})  # в журнал — да, push — нет
    await settle()
    assert [e.title for e in svc.events] == ["Person approaching behind the van", "Person approaching behind the van",
                                             "Person approaching"]
    assert svc.events[0].data["camera"] == "rear" and svc.events[0].data["snapshot"] is True
    assert sirens == [] and svc.outbox.count() == 2
    cfg["s"] = cfg["s"].model_copy(update={"vision": VisionSettings(enabled=True, rear=False)})
    assert not await svc.vision_event("loiter", {"held_s": 9, "camera": "rear"})  # задняя на охране выключена
    assert await svc.vision_event("loiter", {"held_s": 9, "camera": "front"})
    await svc.stop()


async def test_client_reports_rear_camera_and_forwards_camera():
    got: list = []

    async def on_event(kind, data):
        got.append((kind, data.get("camera")))

    cfg = {"v": VisionSettings(enabled=True)}
    armed = [True]
    w = FakeWatch()
    w.watching = True
    c = VisionClient(get_settings=lambda: cfg["v"], get_armed=lambda: armed[0], on_event=on_event,
                     transport=httpx.MockTransport(w), clock=lambda: 1000.0)
    await c.poll()
    assert c.status().rear is None  # старый fnav-watch / задняя не смотрится
    w.cameras = {"front": {"watching": True, "people": 0, "fps": 4.0, "error": None},
                 "rear": {"watching": True, "people": 2, "fps": 1.0, "error": "No frames from the rear camera"}}
    w.add("approach", 1000.0, camera="rear")
    await c.poll()
    assert got == [("approach", "rear")]
    st = c.status()
    assert st.rear == AlarmVisionCamera(watching=True, people=2, error="No frames from the rear camera")
    assert st.error is None and st.watching
    armed[0] = False  # охрану сняли: ядро говорит «не смотри», о людях сзади больше не сообщаем
    await c.poll()
    assert w.arms[-1] is False and c.status().rear == AlarmVisionCamera(watching=False, people=0,
                                                                         error="No frames from the rear camera")
    w.cameras = {"rear": "broken"}
    await c.poll()
    assert c.status().rear is None
    w.up = False
    await c.poll()
    assert c.status().rear is None and not c.status().available
    w.up = True
    w.cameras = {"rear": {"watching": True, "people": 1, "error": None}}
    await c.poll()
    cfg["v"] = VisionSettings(enabled=False)
    assert c.status().rear is None  # распознавание выключено — о задней не говорим
    await c.stop()


# ── миниатюры снимков для списков ──


def _real_jpeg(w: int = 768, h: int = 576) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((w, h), 60).convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


async def test_snapshot_thumbnail_is_made_once_small_and_goes_with_the_snapshot(tmp_path, monkeypatch):
    import io
    import os

    from PIL import Image

    from fnav.core import alarm as alarm_mod

    big = _real_jpeg()

    async def snap(before_s: float = 0.0) -> bytes:
        return big

    svc, _, _ = make_alarm(tmp_path, arm_delay_s=0, vision=VisionSettings(enabled=True))
    svc.snapshot = snap
    await svc.start(background=False)
    await svc.arm()
    assert await svc.vision_event("approach", {})
    await settle()
    ev = svc.events[0]
    assert await svc.snapshot_thumb("0" * 32) is None and await svc.snapshot_thumb("../x") is None
    p = await svc.snapshot_thumb(ev.id)
    assert p is not None and p.parent.name == "thumbs"
    with Image.open(p) as im:
        assert im.size == (192, 144)  # 768×576 / 4 — уменьшение при декодировании
    assert p.stat().st_size < len(big) / 4
    mtime = p.stat().st_mtime_ns
    assert await svc.snapshot_thumb(ev.id) == p and p.stat().st_mtime_ns == mtime  # второй раз — из кэша
    assert Image.open(io.BytesIO(p.read_bytes())).format == "JPEG"

    monkeypatch.setattr(alarm_mod, "SNAPSHOTS_MAX", 1)  # хранится 2 снимка: новые вытесняют старый — с миниатюрой
    os.utime(svc.snapshot_path(ev.id), (1_000_000, 1_000_000))
    svc._write_snapshot("e" * 32, big)
    svc._write_snapshot("f" * 32, big)
    assert not p.exists() and svc.snapshot_path(ev.id) is None
    monkeypatch.undo()

    assert await svc.vision_event("face", {})
    await settle()
    p2 = await svc.snapshot_thumb(svc.events[0].id)
    assert p2 is not None and p2.exists()
    await svc.clear_events()  # очистка журнала — и миниатюры
    assert not p2.exists()
    await svc.stop()


async def test_snapshot_thumbnail_api(data_dir):
    from core_helpers import login, running_app

    big = _real_jpeg()
    async with running_app() as c:
        assert (await c.get("/api/alarm/snapshot/" + "a" * 32 + ".jpg", params={"thumb": 1})).status_code == 401
        await login(c)
        c.app.state.core.alarm._write_snapshot("a" * 32, big)
        full = await c.get("/api/alarm/snapshot/" + "a" * 32 + ".jpg")
        small = await c.get("/api/alarm/snapshot/" + "a" * 32 + ".jpg", params={"thumb": 1})
        assert full.status_code == small.status_code == 200 and full.content == big
        assert small.headers["content-type"] == "image/jpeg" and len(small.content) < len(big) / 4
        assert "max-age" in small.headers["cache-control"]
        r = await c.get("/api/alarm/snapshot/" + "0" * 32 + ".jpg", params={"thumb": 1})
        assert r.status_code == 404
