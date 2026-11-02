"""Задний вид при заднем ходе (fnav/core/autorear.py): автомат по снимкам хаба и путь через Core — тот же, что у кнопки
камеры; ручной выбор главнее до следующего заднего хода."""

from __future__ import annotations

import time

from core_helpers import login, make_opts, running_app

from fnav.core.autorear import FORWARD_CONFIRM_S, REVERSE_CONFIRM_S, STOP_HOLD_S, AutoRear
from fnav.core.runtime import NullSensorHub
from fnav.models import GpsFix, SensorSnapshot

DT = 0.1  # телеметрия 10 Гц
# выдержки отсчитываются от первого снимка с новым состоянием: «держится N с» — это N / DT + 1 снимков


class Drive:
    """Прогон автомата: шаги по 0,1 с, вид экрана меняется по ответам (как Core.view_camera)."""

    def __init__(self, enabled: bool = True) -> None:
        self.a = AutoRear()
        self.t = 0.0
        self.view = "front"
        self.enabled = enabled
        self.switches: list[tuple[float, str]] = []

    def run(self, seconds: float, *, rev: bool, speed: float | None = 0.0) -> None:
        for _ in range(round(seconds / DT)):
            self.t = round(self.t + DT, 3)
            want = self.a.update(self.t, reversing=rev, speed_mps=speed, enabled=self.enabled, view=self.view)
            if want is not None:
                self.view = want
                self.switches.append((self.t, want))

    def press(self) -> None:
        """Кнопка камеры (клавиша 4 / кнопка GPS-модуля / админка)."""
        self.view = "front" if self.view == "rear" else "rear"
        self.a.manual()


def test_reversing_one_second_shows_rear_view() -> None:
    d = Drive()
    d.run(REVERSE_CONFIRM_S, rev=True, speed=1.0)  # первый снимок с флагом — 0,1 с: флаг держится 0,9 с
    assert d.view == "front"
    d.run(DT, rev=True, speed=1.0)
    assert d.view == "rear" and d.switches == [(1.1, "rear")]


def test_short_reversing_blips_are_ignored() -> None:
    d = Drive()
    for _ in range(5):  # флаг дребезжит: 0,5 с да, 0,1 с нет
        d.run(0.5, rev=True, speed=0.5)
        d.run(0.1, rev=False, speed=0.5)
    assert d.switches == []


def test_back_when_driving_forward_faster_than_5_kmh() -> None:
    d = Drive()
    d.run(2.0, rev=True, speed=1.5)
    assert d.view == "rear"
    d.run(2.0, rev=False, speed=1.2)  # вперёд 4,3 км/ч — ещё задний вид
    assert d.view == "rear"
    d.run(DT, rev=False, speed=8.0)  # один скачок скорости — не в счёт
    d.run(DT, rev=False, speed=0.0)
    assert d.view == "rear"
    d.run(FORWARD_CONFIRM_S, rev=False, speed=2.0)  # 7,2 км/ч …
    assert d.view == "rear"
    d.run(DT, rev=False, speed=2.0)  # … держится 0,5 с
    assert d.view == "front"
    assert [v for _, v in d.switches] == ["rear", "front"]


def test_back_after_standing_still_for_10_s() -> None:
    d = Drive()
    d.run(3.0, rev=True, speed=1.0)
    d.run(STOP_HOLD_S, rev=False, speed=0.0)
    assert d.view == "rear"
    d.run(DT, rev=False, speed=0.0)
    assert d.view == "front"


def test_slow_reversing_with_zero_speed_is_not_standing_still() -> None:
    """Static Hold приёмника показывает 0 на медленном заднем ходе — пока хаб видит задний ход, это не стоянка."""
    d = Drive()
    d.run(30.0, rev=True, speed=0.0)
    assert d.view == "rear"
    d.run(STOP_HOLD_S + DT, rev=False, speed=0.0)
    assert d.view == "front"


def test_no_fix_counts_as_standing_still_not_as_driving() -> None:
    d = Drive()
    d.run(2.0, rev=True, speed=None)
    assert d.view == "rear"
    d.run(STOP_HOLD_S + DT, rev=False, speed=None)
    assert d.view == "front"


def test_manual_switch_to_front_during_reversing_wins_until_next_episode() -> None:
    d = Drive()
    d.run(2.0, rev=True, speed=1.0)
    assert d.view == "rear"
    d.press()  # клавиша 4: обратно на переднюю, всё ещё едем назад
    d.run(10.0, rev=True, speed=1.0)
    assert d.view == "front"  # автоматика не спорит
    d.run(1.0, rev=False, speed=0.0)
    d.run(REVERSE_CONFIRM_S + DT, rev=True, speed=1.0)  # новый задний ход
    assert d.view == "rear"


def test_manual_rear_view_is_not_taken_back() -> None:
    """Задний вид, включённый вручную до заднего хода, остаётся и после: автоматика его не присваивала."""
    d = Drive()
    d.press()
    assert d.view == "rear"
    d.run(3.0, rev=True, speed=1.0)
    d.run(STOP_HOLD_S + 1, rev=False, speed=0.0)
    d.run(3.0, rev=False, speed=10.0)
    assert d.view == "rear" and d.switches == []


def test_manual_choice_after_auto_rear_stops_the_automatic_return() -> None:
    """Кнопкой — на переднюю и снова на заднюю во время эпизода: задний вид теперь ручной, вперёд — остаётся."""
    d = Drive()
    d.run(2.0, rev=True, speed=1.0)
    d.press()
    d.press()
    assert d.view == "rear"
    d.run(3.0, rev=False, speed=10.0)
    assert d.view == "rear"


def test_new_episode_while_auto_rear_is_still_on_keeps_it() -> None:
    d = Drive()
    d.run(2.0, rev=True, speed=1.0)
    d.run(3.0, rev=False, speed=0.0)  # постояли 3 с (не 10)
    d.run(2.0, rev=True, speed=1.0)  # снова назад — задний вид как был
    d.run(STOP_HOLD_S + DT, rev=False, speed=0.0)
    assert [v for _, v in d.switches] == ["rear", "front"]


def test_turned_off_does_nothing_and_turning_off_returns() -> None:
    d = Drive(enabled=False)
    d.run(3.0, rev=True, speed=1.0)
    assert d.view == "front"
    d.enabled = True
    d.run(3.0, rev=True, speed=1.0)
    assert d.view == "front"  # включили посреди эпизода — ждём следующего
    d.run(0.5, rev=False, speed=0.0)
    d.run(2.0, rev=True, speed=1.0)
    assert d.view == "rear"
    d.enabled = False  # выключили настройку (или заднюю камеру), пока задний вид держит автоматика
    d.run(DT, rev=True, speed=1.0)
    assert d.view == "front"


def test_view_changed_elsewhere_releases_the_automation() -> None:
    a = AutoRear()
    out = [a.update(i * DT, reversing=True, speed_mps=1.0, enabled=True, view="front") for i in range(11)]
    assert out[-1] == "rear"
    assert a.active
    # Core сам вернул переднюю (задняя камера выключена в настройках) — автомат больше не считает вид своим
    assert a.update(1.3, reversing=True, speed_mps=1.0, enabled=True, view="front") is None
    assert not a.active


# ── через Core: тот же путь, что у кнопки камеры ──


class RevSnap(SensorSnapshot):
    reversing: bool = False  # поле хаба (слияние курса); до слияния веток его в SensorSnapshot может не быть


class FakeHub(NullSensorHub):
    def __init__(self, settings) -> None:
        super().__init__("test")
        self.snap = RevSnap(gps=GpsFix(fix="3d", lat=52.2, lon=21.0, speed_mps=0.0))

    def snapshot(self) -> SensorSnapshot:
        return self.snap

    def set(self, reversing: bool, speed: float | None) -> None:
        gps = self.snap.gps.model_copy(update={"speed_mps": speed, "fix": "3d" if speed is not None else "none"})
        self.snap = RevSnap(gps=gps, reversing=reversing)


def tick(core, t: float, seconds: float, *, rev: bool, speed: float | None) -> float:
    """Снимки хаба 10 раз в секунду — как цикл телеметрии fnav-core."""
    core.hub.set(rev, speed)
    for _ in range(round(seconds / DT)):
        t = round(t + DT, 3)
        core.build_telemetry(now_mono=t)
    return t


async def test_core_reversing_switches_the_car_screen_like_the_camera_button(data_dir):
    async with running_app(make_opts(hub_factory=FakeHub)) as c:
        core = c.app.state.core
        await login(c)
        t = tick(core, time.monotonic(), 3.0, rev=True, speed=1.0)
        assert core.view_camera == "front"  # задняя камера выключена — нечего показывать
        t = tick(core, t, 1.0, rev=False, speed=0.0)
        await c.patch("/api/settings", json={"rear_camera": {"enabled": True}})
        t = tick(core, t, 1.5, rev=True, speed=1.0)
        disp = core.build_telemetry(now_mono=t).display
        assert (disp.mode, disp.reason, disp.camera, disp.rear_mirror) == ("camera", "rear", "rear", True)
        assert (await c.get("/api/camera/view")).json()["camera"] == "rear"
        t = tick(core, t, FORWARD_CONFIRM_S + DT, rev=False, speed=3.0)
        assert core.view_camera == "front"
        assert core.build_telemetry(now_mono=t).display.reason != "rear"


async def test_core_manual_switch_has_priority_and_setting_turns_it_off(data_dir):
    async with running_app(make_opts(hub_factory=FakeHub)) as c:
        core = c.app.state.core
        await login(c)
        await c.patch("/api/settings", json={"rear_camera": {"enabled": True}})
        t = tick(core, time.monotonic(), 1.5, rev=True, speed=1.0)
        assert core.view_camera == "rear"
        r = await c.post("/api/camera/view", json={"camera": "front"})  # админка — ручной выбор
        assert r.json()["camera"] == "front"
        t = tick(core, t, 5.0, rev=True, speed=1.0)
        assert core.view_camera == "front"
        await core.handle_button("press")  # кнопка GPS-модуля: задний вид вручную
        assert core.view_camera == "rear"
        t = tick(core, t, 1.0, rev=False, speed=0.0)
        t = tick(core, t, 3.0, rev=False, speed=5.0)
        assert core.view_camera == "rear"  # ручной — автоматика не возвращает
        await core.switch_camera()  # клавиша 4
        assert core.view_camera == "front"
        await c.patch("/api/settings", json={"display": {"auto_rear_on_reverse": False}})
        t = tick(core, t, 3.0, rev=True, speed=1.0)
        assert core.view_camera == "front"
        assert (await c.get("/api/settings")).json()["display"]["auto_rear_on_reverse"] is False
