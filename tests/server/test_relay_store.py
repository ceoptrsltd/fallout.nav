"""Журнал fnav-relay: слияние точек в места и визиты, фильтр по времени, сохранность."""

from __future__ import annotations

from relay_helpers import Store, at, haversine_m, pt


def visits(store: Store) -> list[tuple]:
    return [tuple(r) for r in store.db.execute(
        "SELECT place_id, start_ts, end_ts, samples FROM visits ORDER BY id")]


def test_helper_distance():
    assert abs(haversine_m(*at(), *at(m_north=100)) - 100) < 0.5
    assert abs(haversine_m(*at(), *at(m_east=100)) - 100) < 0.5


def test_same_place_extends_visit(store):
    res = store.ingest([pt(1000), pt(1060, 10), pt(1120, -15, 20)])
    assert res == {"accepted": 3, "skipped": 0, "last_ts": 1120}
    assert visits(store) == [(1, 1000, 1120, 3)]
    (p,) = store.places()
    assert p["visits"] == 1 and p["longest_s"] == 120 and p["total_s"] == 120


def test_new_place_beyond_radius(store):
    store.ingest([pt(1000), pt(1060, 49), pt(1120, 120)])
    # 49 м — то же место, 120 м от первого места — новое
    assert visits(store) == [(1, 1000, 1060, 2), (2, 1120, 1120, 1)]
    assert len(store.places()) == 2


def test_return_to_old_place_is_new_visit_there(store):
    store.ingest([pt(1000), pt(1060, 800), pt(1120, 1600), pt(1180, 20)])
    assert visits(store) == [(1, 1000, 1000, 1), (2, 1060, 1060, 1), (3, 1120, 1120, 1), (1, 1180, 1180, 1)]
    d = store.place(1)
    assert d["place"]["visits"] == 2
    assert [v["start_ts"] for v in d["visits"]] == [1180, 1000]  # новые первыми


def test_nearest_of_several_places(store):
    store.ingest([pt(1000), pt(1060, 70), pt(1120, 2000)])
    store.ingest([pt(1180, 45)])  # 45 м от первого места и 25 м от второго — берём ближайшее
    assert visits(store)[-1][0] == 2


def test_duplicate_and_old_samples_are_ignored(store):
    store.ingest([pt(1000), pt(1060)])
    res = store.ingest([pt(1060), pt(1030, 500), pt(1120)])
    assert res["accepted"] == 1 and res["skipped"] == 2 and res["last_ts"] == 1120
    assert visits(store) == [(1, 1000, 1120, 3)]
    # повторная отправка той же пачки ничего не меняет
    assert store.ingest([pt(1000), pt(1060)])["accepted"] == 0


def test_unsorted_batch_is_processed_in_time_order(store):
    store.ingest([pt(1120, 1000), pt(1000), pt(1060, 5)])
    assert visits(store) == [(1, 1000, 1060, 2), (2, 1120, 1120, 1)]


def test_invalid_samples_are_skipped(store):
    bad = [{"ts": 1000}, {"ts": True, "lat": 1, "lon": 1}, {"ts": 1000, "lat": 95, "lon": 1},
           {"ts": 1000, "lat": float("nan"), "lon": 1}, {"lat": 1, "lon": 1}, "junk", None]
    res = store.ingest([*bad, pt(2000)])
    assert res == {"accepted": 1, "skipped": len(bad), "last_ts": 2000}


def test_gap_at_same_place_keeps_one_visit(store):
    # стоянка на ночь: устройство было выключено 10 часов
    store.ingest([pt(1000), pt(1060)])
    store.ingest([pt(1060 + 10 * 3600, 8)])
    assert visits(store) == [(1, 1000, 1060 + 10 * 3600, 3)]
    assert store.places()[0]["kind"] == "stop"


def test_kind_stop_or_transit(store):
    store.ingest([pt(1000), pt(1240, 5)])  # 4 мин — промежуточная
    store.ingest([pt(1300, 900), pt(1600, 905)])  # 5 мин — стоянка
    kinds = {p["id"]: p["kind"] for p in store.places()}
    assert kinds == {1: "transit", 2: "stop"}


def test_range_filtering(store):
    store.ingest([pt(1000), pt(1100)])  # место 1: 1000–1100
    store.ingest([pt(2000, 900), pt(2300, 900)])  # место 2: 2000–2300
    store.ingest([pt(5000), pt(5050)])  # место 1 снова: 5000–5050
    assert [p["id"] for p in store.places(1500, 2100)] == [2]  # визит пересекает границу
    assert [p["id"] for p in store.places(2400, 4000)] == []
    p1 = {p["id"]: p for p in store.places(4000, None)}[1]
    assert p1["visits"] == 1 and p1["first_ts"] == 5000 and p1["total_s"] == 50
    all1 = {p["id"]: p for p in store.places()}[1]
    assert all1["visits"] == 2 and all1["total_s"] == 150
    assert [x[0] for x in store.track()] == [1000, 2000, 5000]
    assert [x[0] for x in store.track(1500, 2100)] == [2000]
    assert store.track(1500, 2100)[0][1:] == list(at(900))


def test_last_sample(store):
    assert store.last() == {}
    store.ingest([pt(1000), pt(1060, 3, speed_mps=1.5, course_deg=90.0)], now=1234.5)
    last = store.last()
    assert last["ts"] == 1060 and last["speed_mps"] == 1.5 and last["course_deg"] == 90.0
    assert last["received_ts"] == 1234.5
    assert (last["lat"], last["lon"]) == at(3)


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    s.ingest([pt(1000), pt(1060)])
    s.set_auth("scrypt$1$1$1$a$b", "secret-0123456789")
    s.close()
    s = Store(path)
    try:
        assert s.auth() == ("scrypt$1$1$1$a$b", "secret-0123456789")
        assert s.ingest([pt(1060), pt(1120, 5)])["accepted"] == 1
        assert visits(s) == [(1, 1000, 1120, 3)]  # текущий визит продолжается после перезапуска
    finally:
        s.close()


def test_place_unknown(store):
    assert store.place(42) is None
