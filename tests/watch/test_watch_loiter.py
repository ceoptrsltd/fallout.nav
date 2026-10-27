"""Правило «стоит у фургона» (loiter): силуэт, который нейросеть потеряла, держится пятном движения; прохожие,
стоящие машины, фары, капли, дальние люди — нет; раз за присутствие, перевзвод после 10 с пустоты.
Плюс approach, когда сеть теряет замершего у капота человека."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest
from watchhelpers import StubPerson, gray_frame, person, scene

from fnav.models import VisionSettings
from fnav.watch.detect import Det
from fnav.watch.motion import Blob
from fnav.watch.track import LoiterRule, Track, Tracker

LINE = 0.6
HOLD = 8.0


def blob(cx=0.45, bottom=0.67, h=0.33, w=0.18, active=0.2, bright=0.0, level=50.0, fill=0.7) -> Blob:
    return Blob((cx - w / 2, bottom - h, cx + w / 2, bottom), fill, active, bright, level)


def drive(script, t_end=30.0, dt=0.25, rule=None, tracks=None, hold=HOLD):
    """script(t) → [Blob]; кадры 4/с. → [(t, data)]."""
    rule = rule or LoiterRule()
    out = []
    t = 0.0
    while t <= t_end:
        data = rule.update(t, script(t), tracks(t) if tracks else [], LINE, hold)
        if data:
            out.append((t, data))
        t = round(t + dt, 3)
    return out


def jiggle(t, **kw):
    """Стоит и чуть шевелится: живое пятно, центр гуляет на ±0,02."""
    return blob(cx=kw.pop("cx", 0.45) + 0.02 * np.sin(t * 2), active=kw.pop("active", 0.1 + 0.1 * (int(t * 4) % 3)),
                **kw)


def test_dark_silhouette_fires_once_after_loiter_s():
    out = drive(lambda t: [jiggle(t)] if 2 <= t else [])
    assert len(out) == 1
    t, data = out[0]
    assert 10.0 <= t <= 10.5 and data["source"] == "motion" and data["held_s"] >= HOLD


def test_clock_starts_when_the_person_arrived():
    """Подходит 4 с (пятно растёт) — отсчёт с момента, когда перестал расти."""
    def script(t):
        if t < 2:
            return []
        k = min(1.0, (t - 2) / 4)
        return [jiggle(t, h=0.2 + 0.2 * k)]

    out = drive(script)
    assert len(out) == 1 and 13.0 <= out[0][0] <= 15.0


def test_passer_by_does_not_fire():
    """Идёт мимо у капота (0,8 высоты в секунду), а за ним сразу следующий — цепочка прохожих."""
    def script(t):
        x = 0.1 + (0.2 * t) % 0.8  # каждые 4 с новый слева
        return [blob(cx=x, active=0.3)]

    assert drive(script) == []


def test_parked_car_and_static_object_do_not_fire():
    # подъехала и встала: пятно есть, но не шевелится (фон внутри пятна учится медленно — оно остаётся)
    out = drive(lambda t: [blob(w=0.4, h=0.3, active=0.3 if t < 3 else 0.0)])
    assert out == []


def test_confident_vehicle_suppresses_but_whole_frame_guess_does_not():
    rule = LoiterRule()
    rule.add_vehicles(0.0, [Det((0.3, 0.3, 0.7, 0.7), 0.8)])
    assert drive(lambda t: [jiggle(t)], t_end=2.5, rule=rule) == []  # пятно внутри машины
    rule = LoiterRule()

    def script(t):
        rule.add_vehicles(t, [Det((0.0, 0.0, 1.0, 1.0), 0.45)])  # ночью «машина» — весь кадр: не верим
        return [jiggle(t)]

    assert len(drive(script, rule=rule)) == 1


@pytest.mark.parametrize("kind", ["far", "headlights", "streak", "scattered", "edge"])
def test_not_a_close_person(kind):
    b = {
        "far": {"h": 0.15, "bottom": 0.5},  # человек вдалеке
        "headlights": {"bright": 0.95, "level": 220.0},  # свет фар / блик на стекле
        "streak": {"w": 0.03, "h": 0.35},  # струйка воды
        "scattered": {"fill": 0.15},  # россыпь капель, дворник
        "edge": {"cx": 0.05},  # у края кадра, не у центра
    }[kind]
    assert drive(lambda t: [jiggle(t, **b)]) == []


def test_short_gaps_do_not_reset_presence():
    out = drive(lambda t: [] if 5 <= t < 6.5 else [jiggle(t)])  # 1,5 с пятна не было (сброс фона от фар)
    assert len(out) == 1 and out[0][0] <= 8.5


def test_rearm_only_after_ten_seconds_empty():
    def script(t):
        if t < 15 or 20 <= t < 32 or 43 <= t:  # ушёл на 5 с — тот же визит; на 11 с — новый
            return [jiggle(t)]
        return []

    out = drive(script, t_end=60)
    assert [round(t) for t, _ in out] == [8, 51]


def test_track_close_to_the_van_counts():
    tr = Track(1)
    for i in range(4):
        tr.hist.append((i * 0.5, person(0.5, 0.66, 0.35).box, 0.8))

    def tracks(t):
        tr.hist.append((t, person(0.5, 0.66, 0.35).box, 0.8))
        return [tr]

    out = drive(lambda t: [], tracks=tracks, t_end=12)
    assert len(out) == 1 and out[0][1]["source"] == "track"


def test_approach_fires_when_the_detector_loses_the_person_at_the_line():
    """Ночью сеть теряет силуэт сразу у капота: удержание всё равно засчитывается, если видели ≤ 1,5 с назад."""
    tr = Tracker()
    events = []
    t = 0.0
    while t <= 8:
        k = min(1.0, t / 3)
        seen = t <= 3.5  # у линии с ~2,3 с, потом пропал
        tr.update([person(0.5, 0.45 + 0.21 * k, 0.15 + 0.21 * k)] if seen else [], t, 0.5)
        events += [(t, d) for _, d in tr.approach(t, LINE, 2.0)]
        t += 0.5
    assert len(events) == 1 and events[0][0] <= 5.0
    # пропал надолго до конца удержания — не сработало
    tr = Tracker()
    events = []
    t = 0.0
    while t <= 8:
        k = min(1.0, t / 3)
        tr.update([person(0.5, 0.45 + 0.21 * k, 0.15 + 0.21 * k)] if t <= 2.5 else [], t, 0.5)
        events += tr.approach(t, LINE, 2.0)
        t += 0.5
    assert events == []


@pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="opencv-python-headless")
@pytest.mark.parametrize("enabled", [True, False])
def test_watcher_loiter_from_motion_only(enabled):
    """Конвейер: нейросеть никого не видит (тёмный силуэт), а тёмное шевелящееся пятно стоит у капота."""
    from fnav.watch.pipeline import Watcher

    rng = np.random.default_rng(5)
    base = scene(rng, 0).astype(np.float32) + 60
    p = StubPerson(lambda t: [])
    w = Watcher(p, None, VisionSettings(enabled=True, face=False, loiter=enabled, loiter_s=5.0))
    events = []
    t = 0.0
    while t < 16:
        img = scene(rng, 2.0, base)
        if t >= 3:  # тёмный силуэт у капота, чуть качается
            x = 0.38 + 0.02 * np.sin(t * 3)
            img[int(0.33 * 576):int(0.68 * 576), int(x * 768):int((x + 0.16) * 768)] = 25
        p.t = t
        events += w.process(gray_frame(t, img))
        t += 0.25
    kinds = [e.kind for e in events]
    assert kinds == (["loiter"] if enabled else [])
    if enabled:
        assert events[0].data["source"] == "motion" and events[0].data["held_s"] >= 5.0
