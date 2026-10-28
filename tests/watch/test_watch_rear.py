"""Задняя камера в fnav-watch (docs/contracts/vision.md): своё кольцо, свои фон, треки и правила, своя ближняя
линия (rear_near_line), события с data.camera = "rear". Детекторы общие: за проход не больше одной нейросети, при
нехватке — по очереди. Тихая задняя — реже кадры (камера не декодирует сверх ключевых кадров). Задней нет или она
выключена — передняя работает как прежде."""

from __future__ import annotations

import importlib.util
import json
import time

import httpx
import numpy as np
import pytest
from watchhelpers import RH, Feeder, StubFace, StubPerson, gray_frame, person, scene, wait_until, with_box

from fnav.camera.shmring import RingReader
from fnav.models import VisionSettings
from fnav.watch import service as watch_service
from fnav.watch.pipeline import Plan, Watcher
from fnav.watch.server import create_app
from fnav.watch.service import NO_FRAMES_REAR, Feed, WatchService

needs_cv2 = pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="opencv-python-headless")


class ByCamera:
    """Детектор людей, общий у камер: чей кадр — по высоте (задняя 768×432, передняя 768×576)."""

    def __init__(self, fn) -> None:
        self.fn = fn  # (камера, t) → [Det]
        self.calls: list[str] = []
        self.t = 0.0

    def detect(self, bgr):
        cam = "rear" if bgr.shape[0] == RH else "front"
        self.calls.append(cam)
        return self.fn(cam, self.t)


def write_settings(path, *, rear_camera: bool, **vision) -> None:
    path.write_text(json.dumps({"alarm": {"vision": {"enabled": True, **vision}},
                                "rear_camera": {"enabled": rear_camera}}), encoding="utf-8")


def make(tmp_path, settings, person, face=None, fps=8.0) -> WatchService:
    return WatchService(settings_path=settings, ring=RingReader(tmp_path / "ring", cap=768 * 576 * 3),
                        ring_rear=RingReader(tmp_path / "ring-rear", cap=768 * 576 * 3),
                        detectors=lambda: (person, face), thermal_path=None, fps=fps)


def walker(t0: list):
    """Человек идёт к камере 2 с (рамка растёт, низ 0.45 → 0.66), потом стоит; t0[0] — когда пошёл."""
    def box_at():
        k = min(1.0, (time.monotonic() - t0[0]) / 2.0)
        return person(0.5, 0.45 + 0.21 * k, 0.15 + 0.21 * k)
    return box_at


def image(h: int, t0: list | None = None):
    """Кадр «камеры» высотой h: неподвижная сцена с шумом, с t0[0] — идущий человек."""
    def make_img(t):
        img = scene(np.random.default_rng(int(t * 100)), h=h)
        if t0 is not None and t0[0] is not None:
            img = with_box(img, walker(t0)().box, 230)
        return img
    return make_img


# ── конвейер одной камеры ──
def approacher(t):
    k = min(1.0, t / 4.0)
    return [person(0.5 + 0.02 * k, 0.45 + 0.21 * k, 0.15 + 0.21 * k)]


def walk_frames(w: Watcher, p: StubPerson, h: int, t_end: float = 16.0):
    rng = np.random.default_rng(1)
    events = []
    t = 0.0
    while t < t_end:
        p.t = t
        k = min(1.0, max(0.0, (t - 5) / 4))
        img = scene(rng, h=h)
        if t >= 5:
            img = with_box(img, (0.45, 0.3 - 0.2 * k, 0.55, 0.45 + 0.21 * k))
        events += w.process(gray_frame(t, img))
        t += 0.25
    return events


@needs_cv2
def test_rear_watcher_has_its_own_near_line_aspect_and_camera_tag():
    vs = VisionSettings(enabled=True, approach_hold_s=1.0, loiter_s=30.0, near_line=0.9, rear_near_line=0.6)
    p = StubPerson(lambda t: approacher(t - 5) if t >= 5 else [])
    rear = Watcher(p, None, vs, camera="rear")
    events = walk_frames(rear, p, RH)
    assert [e.kind for e in events] == ["approach"]
    assert events[0].data["camera"] == "rear" and events[0].data["bottom"] >= 0.6
    assert rear.near_line == 0.6 and rear.gate.bottom == pytest.approx(0.7)
    assert rear.aspect == rear.tracker.aspect == rear.loiter.aspect == pytest.approx(16 / 9)
    assert rear.render() is not None
    # та же сцена передней: её линия 0.9 — человек до неё не дошёл
    p2 = StubPerson(lambda t: approacher(t - 5) if t >= 5 else [])
    front = Watcher(p2, None, vs)
    assert walk_frames(front, p2, 576) == []
    assert front.near_line == 0.9 and front.aspect == pytest.approx(4 / 3) and front.camera == "front"


def test_pick_most_overdue_then_whoever_waited_longer(tmp_path):
    a, b = Feed("front", RingReader(tmp_path / "a")), Feed("rear", RingReader(tmp_path / "b"))

    def plans(da, db, na="person", nb="person"):
        return [(a, Plan(None, None, na, da)), (b, Plan(None, None, nb, db))]  # type: ignore[arg-type]

    assert WatchService._pick(plans(1.2, 1.5)) is b
    assert WatchService._pick(plans(1.6, 1.0, nb="face")) is a
    assert WatchService._pick(plans(1.6, 9.0, nb="")) is a  # задней ничего не пора
    assert WatchService._pick(plans(0.0, 0.0, "", "")) is None
    a.last_net, b.last_net = 10.0, 5.0
    assert WatchService._pick(plans(3.0, 5.0)) is b  # обе просрочены вдвое — кто дольше ждал
    a.last_net, b.last_net = 5.0, 10.0
    assert WatchService._pick(plans(3.0, 5.0)) is a


@needs_cv2
def test_one_detector_one_run_per_pass_in_turn(tmp_path):
    """Обе камеры с движением и человеком: каждой нужно 2 Гц, а проходов 2 в секунду — нейросеть по очереди, не
    больше одной за проход; детектор один на обе."""
    settings = tmp_path / "settings.json"
    write_settings(settings, rear_camera=True, face=False, loiter=False)
    p = ByCamera(lambda cam, t: [person(0.5, 0.55, 0.3)])
    svc = make(tmp_path, settings, p)
    assert svc.rear_on
    assert svc._ensure_detectors()
    svc._sync_feeds(time.monotonic())
    assert svc.front.watcher is not None and svc.rear.watcher is not None
    assert svc.front.watcher.person is svc.rear.watcher.person is p
    rng = np.random.default_rng(2)
    per_pass = []
    t = 0.0
    for i in range(40):
        p.t = t
        x = 0.3 + 0.01 * (i % 10)
        box = (x, 0.25, x + 0.12, 0.55)
        n = len(p.calls)
        svc._process([(svc.front, gray_frame(t, with_box(scene(rng), box))),
                      (svc.rear, gray_frame(t, with_box(scene(rng, h=RH), box)))])
        per_pass.append(len(p.calls) - n)
        t += 0.5
    assert max(per_pass) == 1
    front, rear = p.calls.count("front"), p.calls.count("rear")
    assert rear >= 12 and abs(front - rear) <= 2, p.calls
    svc.watching = True
    st = svc.status()
    assert st["cameras"]["front"]["people"] == 1 and st["cameras"]["rear"]["people"] == 1 and st["people"] == 2


@needs_cv2
def test_rear_ring_event_and_idle_frame_rate(tmp_path, monkeypatch):
    """Кадры в два кольца: сзади подходит человек → approach с camera "rear"; спереди тихо — нейросеть только
    сзади. Пока сзади тихо — у камеры просим REAR_IDLE_FPS, движение — полную частоту; сняли охрану — ничего."""
    monkeypatch.setattr(watch_service, "REAR_IDLE_FPS", 2.0)
    settings = tmp_path / "settings.json"
    write_settings(settings, rear_camera=True, approach_hold_s=0.5, near_line=0.9, rear_near_line=0.6)
    t0: list = [None]
    box_at = walker(t0)
    p = ByCamera(lambda cam, t: [box_at()] if cam == "rear" and t0[0] is not None else [])
    svc = make(tmp_path, settings, p, StubFace(lambda t: []))
    front = Feeder(tmp_path / "ring", image(576))
    rear = Feeder(tmp_path / "ring-rear", image(RH, t0))
    svc.start()
    try:
        svc.set_watching(True)
        wait_until(lambda: (tmp_path / "ring").exists() and (tmp_path / "ring-rear").exists(), what="rings")
        front.thr.start()
        rear.thr.start()
        wait_until(lambda: svc.front.frames_read >= 8 and svc.rear.frames_read >= 4, what="background")
        assert rear.writer.demand_fps() == 2.0  # сзади тихо: реже (камера и так декодирует только ключевые)
        assert front.writer.demand_fps() == 8.0
        t0[0] = time.monotonic()
        wait_until(lambda: svc.seq >= 1, timeout=20, what="rear event")
        st = svc.status()
        ev = st["events"][0]
        assert ev["kind"] == "approach" and ev["data"]["camera"] == "rear" and ev["data"]["bottom"] >= 0.6
        assert rear.writer.demand_fps() == 8.0  # сзади движение — полная частота
        cams = st["cameras"]
        assert cams["rear"]["watching"] and cams["rear"]["people"] >= 1 and cams["rear"]["error"] is None
        assert cams["front"]["people"] == 0 and st["people"] == cams["rear"]["people"] and st["error"] is None
        assert set(p.calls) == {"rear"}  # спереди движения не было — нейросеть только по заднему кадру
        assert (svc.debug_jpeg("rear") or b"")[:2] == b"\xff\xd8"
        assert svc.debug_info()["rear"]["near_line"] == 0.6
        svc.set_watching(False)
        wait_until(lambda: rear.writer.demand_fps() == 0 and front.writer.demand_fps() == 0, what="release")
    finally:
        front.stop.set()
        rear.stop.set()
        svc.stop()


@needs_cv2
def test_rear_disabled_reads_nothing_then_follows_settings(tmp_path):
    settings = tmp_path / "settings.json"
    write_settings(settings, rear_camera=False)
    svc = make(tmp_path, settings, ByCamera(lambda cam, t: []))
    front = Feeder(tmp_path / "ring", image(576))
    svc.start()
    try:
        svc.set_watching(True)
        front.thr.start()
        wait_until(lambda: svc.front.frames_read >= 4, what="front frames")
        assert not (tmp_path / "ring-rear").exists()  # задняя выключена: кольца нет, камера ничего не копирует
        st = svc.status()
        assert set(st["cameras"]) == {"front"} and svc.rear.watcher is None and svc.debug_info()["rear"] is None

        write_settings(settings, rear_camera=True)  # включили заднюю (alarm.vision.rear по умолчанию — да)
        svc.reload()
        wait_until(lambda: (tmp_path / "ring-rear").exists(), what="rear ring")
        rear = Feeder(tmp_path / "ring-rear", image(RH))
        rear.thr.start()
        try:
            wait_until(lambda: svc.rear.frames_read >= 2, what="rear frames")
            assert svc.status()["cameras"]["rear"]["watching"] is True

            write_settings(settings, rear_camera=True, rear=False)  # камера есть, но смотреть её на охране не надо
            svc.reload()
            wait_until(lambda: rear.writer.demand_fps() == 0, what="rear released")
            assert "rear" not in svc.status()["cameras"]
            n = svc.front.frames_read
            wait_until(lambda: svc.front.frames_read >= n + 3, what="front keeps going")
        finally:
            rear.stop.set()
    finally:
        front.stop.set()
        svc.stop()


@needs_cv2
def test_no_rear_frames_front_keeps_working(tmp_path, monkeypatch):
    """Задняя включена, но кадров нет (камера выключена / fnav-camera-rear не работает): у задней ошибка в
    cameras.rear, общая — нет; передняя ловит своё событие как прежде (camera "front")."""
    monkeypatch.setattr(watch_service, "REAR_NO_FRAMES_S", 0.3)
    settings = tmp_path / "settings.json"
    write_settings(settings, rear_camera=True, approach_hold_s=0.5)
    t0: list = [None]
    box_at = walker(t0)
    svc = make(tmp_path, settings, ByCamera(lambda cam, t: [box_at()] if t0[0] is not None else []))
    front = Feeder(tmp_path / "ring", image(576, t0))
    svc.start()
    try:
        svc.set_watching(True)
        front.thr.start()
        wait_until(lambda: svc.status()["cameras"]["rear"]["error"] == NO_FRAMES_REAR, what="rear error")
        wait_until(lambda: svc.front.frames_read >= 6, what="background")
        t0[0] = time.monotonic()
        wait_until(lambda: svc.seq >= 1, timeout=20, what="front event")
        st = svc.status()
        assert st["events"][0]["kind"] == "approach" and st["events"][0]["data"]["camera"] == "front"
        assert st["error"] is None and st["watching"] is True and st["fps"] is not None
        assert st["cameras"]["rear"] == {"watching": True, "people": 0, "fps": None, "error": NO_FRAMES_REAR}
        assert svc.rear.frames_read == 0
    finally:
        front.stop.set()
        svc.stop()


async def test_debug_endpoints_per_camera(tmp_path):
    settings = tmp_path / "settings.json"
    write_settings(settings, rear_camera=False)
    svc = make(tmp_path, settings, StubPerson(lambda t: []))
    app = create_app(svc, manage_service=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w") as c:
        assert (await c.get("/debug.jpg?camera=rear")).status_code == 503
        assert (await c.get("/debug.jpg?camera=side")).status_code == 400
        assert (await c.get("/debug.json")).json()["rear"] is None
        write_settings(settings, rear_camera=True, rear_near_line=0.7)
        r = (await c.post("/reload")).json()
        assert r["settings"]["rear"] is True and r["settings"]["rear_near_line"] == 0.7
        assert (await c.get("/status")).json()["cameras"]["rear"] == {"watching": False, "people": 0, "fps": None,
                                                                      "error": None}
