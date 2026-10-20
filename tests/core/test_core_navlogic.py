from fnav.core.navlogic import NavLogic, has_fix, zoom_for_speed
from fnav.models import DisplaySettings, GpsFix, NavSettings, ZoomStep


def fix(speed: float | None, kind: str = "3d", age: float | None = 0.1) -> GpsFix:
    return GpsFix(fix=kind, lat=55.7, lon=37.6, speed_mps=speed, age_s=age)


NOFIX = GpsFix(fix="none")


def logic(**nav_kw) -> NavLogic:
    return NavLogic(NavSettings(**nav_kw), DisplaySettings(), start_time=0.0, startup_grace_s=5.0)


def run(nl: NavLogic, t0: float, t1: float, gps: GpsFix, dt: float = 0.1):
    t = t0
    st = None
    while t <= t1 + 1e-9:
        st = nl.update(t, gps)
        t += dt
    return st


def test_zoom_table_lookup():
    nav = NavSettings()
    assert zoom_for_speed(nav, 0) == 19
    assert zoom_for_speed(nav, 4.99) == 19
    assert zoom_for_speed(nav, 5.0) == 18
    assert zoom_for_speed(nav, 12) == 17
    assert zoom_for_speed(nav, 17) == 16
    assert zoom_for_speed(nav, 40) == 15
    # неотсортированная таблица и скорость ниже всех порогов
    nav2 = NavSettings(zoom_table=[ZoomStep(min_mps=10, zoom=16), ZoomStep(min_mps=3, zoom=18)])
    assert zoom_for_speed(nav2, 1) == 18
    assert zoom_for_speed(nav2, 11) == 16


def test_has_fix():
    assert has_fix(fix(1.0))
    assert has_fix(fix(1.0, "2d"))
    assert not has_fix(NOFIX)
    assert not has_fix(fix(1.0, age=10))  # устаревшие данные
    assert not has_fix(GpsFix(fix="3d", lat=None, lon=None))


def test_startup_then_no_fix():
    nl = logic()
    st = nl.update(0.0, NOFIX)
    assert (st.mode, st.reason) == ("camera", "startup")
    st = nl.update(6.0, NOFIX)
    assert (st.mode, st.reason) == ("camera", "no_fix")


def test_no_fix_mode_map():
    nl = logic(no_fix_mode="map")
    st = nl.update(10.0, NOFIX)
    assert (st.mode, st.reason) == ("map", "no_fix")


def test_first_fix_selects_mode_immediately():
    nl = logic()
    st = nl.update(0.0, fix(10.0))
    assert (st.mode, st.reason, st.target_zoom) == ("map", "speed", 18)
    nl2 = logic()
    st = nl2.update(0.0, fix(0.5))
    assert (st.mode, st.target_zoom) == ("camera", 19)


def test_hysteresis_map_to_camera_needs_delay():
    nl = logic()
    assert nl.update(0.0, fix(10.0)).mode == "map"
    # скорость упала ниже 2 м/с: 1.9 с — ещё карта
    st = run(nl, 0.1, 1.9, fix(1.0))
    assert st.mode == "map"
    st = run(nl, 2.0, 2.2, fix(1.0))
    assert st.mode == "camera"


def test_hysteresis_band_and_reset():
    nl = logic()
    nl.update(0.0, fix(0.0))
    assert nl.update(0.1, fix(0.0)).mode == "camera"
    # 2.2 м/с — между порогами (2.0 и 2.5): остаёмся на камере сколько угодно
    assert run(nl, 0.2, 10.0, fix(2.2)).mode == "camera"
    # 1.5 с выше 2.5, провал, снова 1.5 с — таймер сбрасывается, переключения нет
    run(nl, 10.1, 11.6, fix(3.0))
    nl.update(11.7, fix(2.2))
    assert run(nl, 11.8, 13.3, fix(3.0)).mode == "camera"
    # а теперь держим 2 с
    assert run(nl, 13.4, 16.0, fix(3.0)).mode == "map"
    # 2.2 м/с на карте — карта остаётся (выше camera_below)
    assert run(nl, 16.1, 30.0, fix(2.2)).mode == "map"


def test_zoom_delay():
    nl = logic()
    assert nl.update(0.0, fix(6.0)).target_zoom == 18
    # 12 м/с → ступень 17, но только через 3 с
    assert run(nl, 0.1, 2.9, fix(12.0)).target_zoom == 18
    assert run(nl, 3.0, 3.2, fix(12.0)).target_zoom == 17
    # кратковременный всплеск не меняет зум
    run(nl, 3.3, 4.5, fix(20.0))
    assert nl.update(4.6, fix(12.0)).target_zoom == 17
    assert run(nl, 4.7, 10.0, fix(12.0)).target_zoom == 17


def test_zoom_candidate_change_restarts_timer():
    nl = logic()
    nl.update(0.0, fix(6.0))  # 18
    run(nl, 0.1, 2.0, fix(12.0))  # кандидат 17
    st = run(nl, 2.1, 4.0, fix(20.0))  # кандидат сменился на 16 → таймер заново
    assert st.target_zoom == 18
    assert run(nl, 4.1, 5.2, fix(20.0)).target_zoom == 16


def test_no_fix_keeps_zoom_and_regains():
    nl = logic()
    nl.update(0.0, fix(12.0))
    st = nl.update(1.0, NOFIX)
    assert (st.mode, st.reason, st.target_zoom) == ("camera", "no_fix", 17)
    st = nl.update(2.0, fix(12.0))
    assert (st.mode, st.reason) == ("map", "speed")


def test_force_mode():
    nl = logic()
    nl.update(0.0, fix(12.0))
    nl.apply_settings(NavSettings(), DisplaySettings(force_mode="camera"))
    st = nl.update(0.1, fix(12.0))
    assert (st.mode, st.reason) == ("camera", "forced")
    st = nl.update(0.2, NOFIX)
    assert (st.mode, st.reason) == ("camera", "forced")
    nl.apply_settings(NavSettings(), DisplaySettings(force_mode="map"))
    assert nl.update(0.3, NOFIX).mode == "map"
    # возврат в auto — сразу корректный авто-режим
    nl.apply_settings(NavSettings(), DisplaySettings(force_mode="auto", orientation="north_up"))
    st = nl.update(0.4, fix(12.0))
    assert (st.mode, st.reason, st.orientation) == ("map", "speed", "north_up")


def test_zero_delays_switch_immediately():
    nl = logic(switch_delay_s=0, zoom_delay_s=0)
    nl.update(0.0, fix(10.0))
    st = nl.update(0.1, fix(0.0))
    assert (st.mode, st.target_zoom) == ("camera", 19)


def test_time_going_backwards_is_safe():
    nl = logic()
    nl.update(100.0, fix(10.0))
    run(nl, 100.1, 101.5, fix(1.0))
    st = nl.update(50.0, fix(1.0))  # часы прыгнули назад — выдержка начинается заново
    assert st.mode == "map"
    assert run(nl, 50.1, 52.5, fix(1.0)).mode == "camera"
