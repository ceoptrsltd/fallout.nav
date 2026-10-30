"""Правила событий: сопровождение и «подходит» (рост + ближняя линия + удержание), прохожий, лицо у стекла,
расписание нейросетей в конвейере."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest
from watchhelpers import StubFace, StubPerson, face, gray_frame, person, scene, with_box

from fnav.models import VisionSettings
from fnav.watch.track import FaceRule, Tracker

LINE = 0.6
HOLD = 2.0


def drive(script, dt=0.5, t_end=12.0):
    """script(t) → [Det]; детектор людей с частотой 1/dt. → [(t, data)] событий."""
    tr = Tracker()
    events = []
    t = 0.0
    while t <= t_end:
        tr.update(script(t), t, dt if t > 0 else None)
        events += [(t, data) for _, data in tr.approach(t, LINE, HOLD)]
        t = round(t + dt, 3)
    return events, tr


def approacher(t):
    """Идёт к фургону 4 с (рамка растёт 0.15 → 0.36, низ 0.45 → 0.66), потом стоит у капота."""
    k = min(1.0, t / 4.0)
    return [person(0.5 + 0.02 * k, 0.45 + 0.21 * k, 0.15 + 0.21 * k)]


def test_approach_fires_once_after_hold():
    events, _ = drive(approacher)
    assert len(events) == 1
    t, data = events[0]
    # линию (0.6) пересёк на ~3.2 с; плюс удержание 2 с
    assert 5.0 <= t <= 6.5
    assert data["growth"] >= 1.25 and data["bottom"] >= LINE and data["held_s"] >= HOLD


def test_passer_by_does_not_fire():
    """Идёт мимо у самого капота (1,4 м/с ≈ 0,8 высоты в секунду): высота постоянна."""
    events, _ = drive(lambda t: [person(0.0 + 0.2 * t, 0.66, 0.33)] if t < 5 else [])
    assert events == []


def test_diagonal_passer_by_does_not_fire():
    """Идёт мимо наискосок: рамка растёт, но быстро вбок."""
    events, tr = drive(lambda t: [person(0.0 + 0.2 * t, 0.5 + 0.04 * t, 0.2 + 0.04 * t)], t_end=4.5)
    assert events == []
    assert any(x.grew for x in tr.tracks)  # рост был — не сработало именно из-за скорости вбок


def test_too_short_at_the_line_does_not_fire():
    def script(t):
        if t <= 4.0:
            return approacher(t)
        if t <= 4.5:  # у линии меньше approach_hold_s и ушёл
            return [person(0.52, 0.66, 0.36)]
        return [person(0.52, 0.5, 0.3)]

    events, _ = drive(script)
    assert events == []


def test_standing_far_does_not_fire():
    events, _ = drive(lambda t: [person(0.5, 0.5, 0.15)])  # стоит вдали: низ выше линии
    assert events == []


def test_no_growth_at_the_line_does_not_fire():
    events, _ = drive(lambda t: [person(0.5, 0.66, 0.36)])  # появился уже у капота и стоит: роста не было
    assert events == []


def test_retrigger_only_after_leaving():
    def script(t):
        if t <= 8:
            return approacher(t)
        if t <= 13:
            return [person(0.52, 0.48, 0.2)]  # отошёл от линии
        return approacher(t - 13)  # и снова подошёл

    events, _ = drive(script, t_end=26)
    assert len(events) == 2
    assert events[0][1]["track"] == events[1][1]["track"]  # тот же трек, перевзвод после ухода от линии


def test_new_track_of_the_same_person_does_not_repeat_the_alarm():
    """Ночью сеть теряет замершего у капота на несколько секунд: трек закрылся, вернувшаяся детекция — новый трек
    там же. Тревога не повторяется (живой прогон 27.10); ушёл совсем и через 20 с подошёл снова — тревога."""
    def script(t):
        if t <= 7:
            return approacher(t)
        if t <= 11:
            return []  # потерян 4 с — трек закрыт
        if t <= 20:
            return approacher(t - 11)  # «новый» трек: снова растёт и у линии
        if t <= 40:
            return []
        return approacher(t - 40)

    events, _ = drive(script, t_end=50)
    assert [round(t) for t, _ in events] == [5, 45]
    assert events[0][1]["track"] != events[1][1]["track"]


def test_missed_detections_keep_the_track():
    def script(t):
        if int(t * 2) % 3 == 1:
            return []  # каждая третья детекция пропущена
        return approacher(t)

    events, _ = drive(script)
    assert len(events) == 1


def test_tracker_matches_by_centre_and_height():
    tr = Tracker()
    tr.update([person(0.3, 0.5, 0.2), person(0.7, 0.5, 0.2)], 0.0)
    tr.update([person(0.72, 0.52, 0.22), person(0.32, 0.51, 0.21)], 0.5, 0.5)
    ids = sorted((round((t.box[0] + t.box[2]) / 2, 2), t.id) for t in tr.tracks)
    assert len(tr.tracks) == 2 and ids[0][1] == 1 and ids[1][1] == 2
    tr.update([person(0.5, 0.9, 0.6)], 1.0, 0.5)  # совсем другой масштаб — новый трек
    assert len(tr.tracks) == 3


# ── лицо ──
def drive_face(script, dt=0.5, t_end=10.0, hold=3.0, face_min=0.08):
    rule = FaceRule()
    out = []
    t = 0.0
    while t <= t_end:
        data = rule.update(script(t), t, face_min, hold)
        if data:
            out.append((t, data))
        t = round(t + dt, 3)
    return out


def test_face_fires_after_hold():
    out = drive_face(lambda t: [face(0.5, 0.4, 0.12)] if t >= 1.0 else [])
    assert len(out) == 1
    t, data = out[0]
    assert 3.0 <= t <= 4.0 and data["width"] >= 0.08 and data["hits"].startswith("6/")


def test_face_4_of_6_is_enough_but_3_is_not():
    four = drive_face(lambda t: [face(0.5, 0.4, 0.12)] if int(t * 2) % 3 != 1 else [], t_end=6)
    assert len(four) == 1
    three = drive_face(lambda t: [face(0.5, 0.4, 0.12)] if int(t * 2) % 2 == 0 else [], t_end=6)
    assert three == []


@pytest.mark.parametrize("bad", ["small", "pale", "landmarks"])
def test_face_rejected(bad):
    def script(t):
        if bad == "small":
            return [face(0.5, 0.4, 0.05)]
        if bad == "pale":
            return [face(0.5, 0.4, 0.12, contrast=4.0)]  # бледное отражение в стекле
        f = face(0.5, 0.4, 0.12)
        f.landmarks[3], f.landmarks[0] = f.landmarks[0], f.landmarks[3]  # «рот» над «глазом»
        return [f]

    assert drive_face(script) == []


def test_face_short_does_not_fire_and_retriggers_after_leaving():
    assert drive_face(lambda t: [face(0.5, 0.4, 0.12)] if 1 <= t < 2.5 else []) == []
    out = drive_face(lambda t: [face(0.5, 0.4, 0.12)] if t < 4 or 9 <= t < 13 else [], t_end=16)
    assert len(out) == 2


# ── конвейер: нейросети только по делу ──
needs_cv2 = pytest.mark.skipif(importlib.util.find_spec("cv2") is None, reason="opencv-python-headless")


@needs_cv2
def test_watcher_runs_detectors_only_with_motion():
    from fnav.watch.pipeline import Watcher

    rng = np.random.default_rng(0)
    p = StubPerson(lambda t: [])
    f = StubFace(lambda t: [])
    w = Watcher(p, f, VisionSettings(enabled=True))
    t = 0.0
    for _ in range(40):  # 10 с неподвижной сцены
        p.t = f.t = t
        w.process(gray_frame(t, scene(rng)))
        t += 0.25
    assert p.calls == [] and f.calls == []
    for i in range(16):  # 4 с движения у центра
        p.t = f.t = t
        x = 0.3 + 0.02 * i
        w.process(gray_frame(t, with_box(scene(rng), (x, 0.2, x + 0.1, 0.6))))
        t += 0.25
    assert 3 <= len(p.calls) <= 6  # людей нет — ~1 Гц
    assert 5 <= len(f.calls) <= 9  # движение у центра — лица ~2 Гц
    assert len(p.calls) + len(f.calls) <= 16  # не больше одной нейросети на кадр


@needs_cv2
def test_watcher_reports_approach_event():
    from fnav.watch.pipeline import Watcher

    rng = np.random.default_rng(1)
    p = StubPerson(lambda t: approacher(t - 5) if t >= 5 else [])
    w = Watcher(p, None, VisionSettings(enabled=True, approach_hold_s=1.0))
    events = []
    t = 0.0
    while t < 16:
        p.t = t
        k = min(1.0, max(0.0, (t - 5) / 4))
        img = scene(rng) if t < 5 else with_box(scene(rng), (0.45, 0.3 - 0.2 * k, 0.55, 0.45 + 0.21 * k))
        events += w.process(gray_frame(t, img))
        t += 0.25
    assert [e.kind for e in events] == ["approach"]
    assert w.people == 1
    assert w.render() is not None  # картинка для /debug.jpg


@needs_cv2
def test_person_detector_backs_off_on_motion_without_people():
    """Долго движение без людей (деревья, дождь) — детектор людей реже; нашёл человека — снова часто."""
    from fnav.watch.pipeline import BACKOFF_RUNS, Watcher

    rng = np.random.default_rng(3)
    p = StubPerson(lambda t: [])
    w = Watcher(p, None, VisionSettings(enabled=True, face=False))
    t = 0.0

    def moving(n):
        nonlocal t
        for i in range(n):
            p.t = t
            x = 0.3 + 0.02 * (i % 10)
            w.process(gray_frame(t, with_box(scene(rng), (x, 0.2, x + 0.1, 0.6))))
            t += 0.25

    moving(12)  # фон и первые прогоны
    w._empty_runs = BACKOFF_RUNS
    n = len(p.calls)
    moving(32)  # 8 с
    assert 3 <= len(p.calls) - n <= 5  # 0,5 Гц, а не 1 Гц
