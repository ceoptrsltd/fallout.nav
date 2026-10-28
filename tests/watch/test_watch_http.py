"""HTTP-контракт fnav-watch (docs/contracts/vision.md): /status, /arm, /reload, /debug.*; и клиент ядра
(fnav/core/vision.py VisionClient) против настоящего приложения: «смотри» → кадры из кольца → событие → ядро."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import time

import httpx
import numpy as np
import pytest
from watchhelpers import Feeder, StubFace, StubPerson, scene, with_box

from fnav.camera.shmring import RingReader
from fnav.watch import service as watch_service
from fnav.watch.server import create_app
from fnav.watch.service import NO_FRAMES, WatchService

needs_cv2 = pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="opencv-python-headless")


@pytest.fixture
def settings_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"alarm": {"vision": {"enabled": True, "approach_hold_s": 0.5}}}), encoding="utf-8")
    return p


def make_service(tmp_path, settings_file, person=None, face=None, fps=8.0):
    ring = RingReader(tmp_path / "ring", cap=768 * 576 * 3)
    person = person or StubPerson(lambda t: [])
    face = face or StubFace(lambda t: [])
    svc = WatchService(settings_path=settings_file, ring=ring, ring_rear=RingReader(tmp_path / "ring-rear"),
                       detectors=lambda: (person, face), thermal_path=None, fps=fps)
    return svc


async def wait_for(fn, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if inspect.isawaitable(v):
            v = await v
        if v:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError("timeout")


async def test_status_arm_reload(tmp_path, settings_file, monkeypatch):
    monkeypatch.setattr(watch_service, "NO_FRAMES_S", 0.3)
    svc = make_service(tmp_path, settings_file)
    svc.start()
    app = create_app(svc, manage_service=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w") as c:
            st = (await c.get("/status")).json()
            assert st == {"watching": False, "people": 0, "seq": 0, "events": [], "error": None, "fps": None,
                          "cameras": {"front": {"watching": False, "people": 0, "fps": None, "error": None}}}
            assert (await c.post("/arm", json={"watching": "yes"})).status_code == 400
            assert (await c.post("/arm", content=b"nope")).status_code == 400
            r = await c.post("/arm", json={"watching": True})
            assert r.status_code == 200 and r.json() == {"watching": True}
            # камеры нет — кадров нет — ошибка в статусе, но смотреть продолжаем
            st = await wait_for(lambda: _status(c, lambda s: s["error"] == NO_FRAMES))
            assert st["watching"] is True and st["fps"] is None
            assert (tmp_path / "ring").exists()  # кольцо создал читатель
            settings_file.write_text(json.dumps({"alarm": {"vision": {"near_line": 0.72}}}), encoding="utf-8")
            r = await c.post("/reload")
            assert r.json()["ok"] is True and r.json()["settings"]["near_line"] == 0.72
            r = await c.post("/arm", json={"watching": False})
            assert r.json() == {"watching": False}
            st = await wait_for(lambda: _status(c, lambda s: s["error"] is None))
            assert st["watching"] is False
            assert (await c.get("/debug.json")).json()["watching"] is False
    finally:
        svc.stop()


async def _status(c, pred):
    st = (await c.get("/status")).json()
    return st if pred(st) else None


@needs_cv2
async def test_event_reaches_core_client(tmp_path, settings_file):
    """Ядро говорит «смотри» → кадры с движением → заглушка находит подходящего человека → событие в /status →
    VisionClient передаёт его в AlarmService.vision_event; «не смотри» — кадры больше не копируются."""
    from watchhelpers import person

    from fnav.core.vision import VisionClient
    from fnav.models import VisionSettings

    t_start = [None]

    def walk(t):  # человек идёт к фургону 2 с, потом стоит у капота
        k = min(1.0, t / 2.0)
        return person(0.5, 0.45 + 0.21 * k, 0.15 + 0.21 * k)

    def script_person(_t):
        if t_start[0] is None:
            return []
        return [walk(time.monotonic() - t_start[0])]

    def image(t):
        rng = np.random.default_rng(int(t * 100))
        img = scene(rng)
        if t_start[0] is not None:
            b = walk(time.monotonic() - t_start[0]).box
            img = with_box(img, b, 230)
        return img

    svc = make_service(tmp_path, settings_file, person=StubPerson(script_person))
    svc.start()
    feeder = Feeder(tmp_path / "ring", image)
    feeder.thr.start()
    app = create_app(svc, manage_service=False)
    got: list[tuple[str, dict]] = []

    async def on_event(kind, data):
        got.append((kind, data))

    armed = [True]
    client = VisionClient(get_settings=lambda: VisionSettings(enabled=True), get_armed=lambda: armed[0],
                          on_event=on_event, transport=httpx.ASGITransport(app=app))
    try:
        await client.poll()  # сервис не смотрит — ядро шлёт /arm
        assert svc.watching and client.watching
        await wait_for(lambda: svc.frames_read >= 6)  # фон набран
        t_start[0] = time.monotonic()
        await wait_for(lambda: svc.seq >= 1, timeout=15)
        await client.poll()
        assert [k for k, _ in got] == ["approach"]
        assert got[0][1]["bottom"] >= 0.6
        st = svc.status()
        assert st["seq"] == 1 and st["events"][0]["kind"] == "approach" and st["people"] >= 1
        assert st["fps"] is not None and st["error"] is None
        r = await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w").get("/debug.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
        armed[0] = False
        await client.poll()  # охрана снята — ядро шлёт «не смотри»
        assert not svc.watching
        await asyncio.sleep(0.3)
        n = feeder.writer.frames
        await asyncio.sleep(0.6)
        assert feeder.writer.frames == n  # камера больше не копирует кадры
    finally:
        feeder.stop.set()
        await client.stop()
        svc.stop()


def test_not_watching_loads_nothing(tmp_path, settings_file):
    calls = []

    def detectors():
        calls.append(1)
        return StubPerson(lambda t: []), StubFace(lambda t: [])

    svc = WatchService(settings_path=settings_file, ring=RingReader(tmp_path / "ring"), detectors=detectors,
                       thermal_path=None)
    svc.start()
    try:
        time.sleep(0.3)
        assert calls == [] and not (tmp_path / "ring").exists()  # не смотрим: ни моделей, ни кольца
    finally:
        svc.stop()


def test_thermal_slows_down(tmp_path, settings_file):
    temp = tmp_path / "temp"
    svc = WatchService(settings_path=settings_file, ring=RingReader(tmp_path / "ring"), thermal_path=temp,
                       detectors=lambda: (None, None))
    for milli, slow in ((60000, 1.0), (76000, 2.0), (73500, 2.0), (71000, 1.0), (82000, 3.0), (78500, 3.0),
                        (76000, 2.0)):
        temp.write_text(f"{milli}\n")
        svc._thermal()
        assert svc.slow == slow, milli
