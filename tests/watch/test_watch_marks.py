"""Отметки движения без тревоги (MarkRule): эпизоды движения → «движение» / «машина» / «человек»; кусок с
тревогой — без отметки; длинный эпизод — кусками по MARK_CHUNK_S; начало куска — по часам кадров."""

from __future__ import annotations

from fnav.watch.detect import Det
from fnav.watch.marks import MARK_CHUNK_S, MARK_GAP_S, MarkRule

BLOB = (0.4, 0.4, 0.6, 0.6)


def run(moving, t_end=40.0, dt=0.25, vehicles=None, people=None, alarm=None, wall0=1000.0):
    """moving(t) → bool; vehicles(t) → [Det] | None (прогон сети на кадре); → [(t, data)]."""
    rule = MarkRule()
    out = []
    t = 0.0
    while t <= t_end:
        mv = moving(t)
        data = rule.update(t, wall0 + t, mv, [BLOB] if mv else [], vehicles=vehicles(t) if vehicles else None,
                           people=bool(people and people(t)), alarm=bool(alarm and alarm(t)))
        if data:
            out.append((t, data))
        t = round(t + dt, 3)
    return out


def test_episode_gives_one_mark_after_the_gap_from_its_start():
    out = run(lambda t: 2 <= t < 6 or 9 <= t < 12)  # пауза 3 с — тот же эпизод
    assert len(out) == 1
    t, d = out[0]
    assert t > 12 + MARK_GAP_S - 0.5 and d == {"what": "motion", "start": 1002.0, "dur_s": 9.8}


def test_two_episodes_two_marks():
    out = run(lambda t: 1 <= t < 3 or 20 <= t < 22, t_end=40)
    assert [d["start"] for _, d in out] == [1001.0, 1020.0]


def test_vehicle_only_where_the_motion_is():
    car_on_blob = [Det((0.35, 0.35, 0.65, 0.65), 0.7)]
    parked_elsewhere = [Det((0.0, 0.7, 0.2, 0.9), 0.9)]
    whole_frame = [Det((0.0, 0.0, 1.0, 0.95), 0.9)]  # кабина / сам фургон
    weak = [Det((0.35, 0.35, 0.65, 0.65), 0.45)]
    for dets, what in ((car_on_blob, "vehicle"), (parked_elsewhere, "motion"), (whole_frame, "motion"),
                       (weak, "motion")):
        out = run(lambda t: 2 <= t < 6, vehicles=lambda t, d=dets: d if t == 3.0 else None)
        assert [d["what"] for _, d in out] == [what]


def test_person_seen_is_a_person_mark_and_an_alarm_suppresses_it():
    out = run(lambda t: 2 <= t < 6, vehicles=lambda t: [Det(BLOB, 0.9)], people=lambda t: t == 4.0)
    assert [d["what"] for _, d in out] == ["person"]  # человек важнее машины
    assert run(lambda t: 2 <= t < 6, people=lambda t: t >= 3, alarm=lambda t: t == 5.0) == []


def test_long_episode_is_cut_into_chunks():
    out = run(lambda t: t >= 1, t_end=190)
    starts = [d["start"] - 1000 for _, d in out]
    assert starts == [1.0, 1.0 + MARK_CHUNK_S, 1.0 + 2 * MARK_CHUNK_S]
    assert all(abs(t - (s + MARK_CHUNK_S)) < 0.3 for (t, _), s in zip(out, starts, strict=True))
