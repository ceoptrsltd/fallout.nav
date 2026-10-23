"""Мини-клавиатура: коды F13–F21 → действия; пока открыто меню игр, крутилку и клавишу 6 забирает экран."""

from __future__ import annotations

from fnav.core import keypad as kp


def ev(code: int, value: int) -> bytes:
    return kp.EVENT.pack(0, 0, kp.EV_KEY, code, value)


def test_decode_presses_only():
    svc = kp.KeypadService({})
    data = ev(kp.KEY_F13, 1) + ev(kp.KEY_F13, 2) + ev(kp.KEY_F13, 0) + ev(kp.KEY_F13 + 8, 1) + ev(30, 1)
    assert svc.decode(data) == ["station_prev", "volume_up"]  # автоповтор, отпускание и чужие клавиши — нет


async def test_dispatch_and_menu_guard():
    calls: list[str] = []

    def h(name):
        async def fn():
            calls.append(name)
        return fn

    svc = kp.KeypadService({a: h(a) for a in kp.ACTIONS.values()})
    await svc.dispatch("volume_up")
    await svc.dispatch("protect")
    svc.set_games_menu(True)
    for a in ("volume_down", "volume_up", "games", "video_toggle"):
        await svc.dispatch(a)
    svc.set_games_menu(False)
    await svc.dispatch("games")
    assert calls == ["volume_up", "protect", "video_toggle", "games"]
    # из меню запустили игру: экран закрылся молча — меню больше не считается открытым
    running = {"v": False}
    svc2 = kp.KeypadService({"games": h("exit")}, game_running=lambda: running["v"])
    svc2.set_games_menu(True)
    await svc2.dispatch("games")
    running["v"] = True
    await svc2.dispatch("games")
    assert calls[-1] == "exit" and calls.count("exit") == 1


async def test_handler_error_does_not_raise():
    async def boom():
        raise RuntimeError("x")

    await kp.KeypadService({"protect": boom}).dispatch("protect")
