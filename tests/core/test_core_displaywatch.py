"""Сторож экрана (fnav/core/displaywatch.py) и сигнал «alive» страницы кинескопа через /ws."""

from __future__ import annotations

import asyncio
import json

from websockets.asyncio.client import connect

from fnav.core import displaywatch as dw


def _watch(active=True):
    now = [1000.0]
    calls: list[str] = []
    state = {"active": active}

    async def restart():
        calls.append("restart")
        return True

    async def is_active():
        return state["active"]

    return dw.DisplayWatch(restart, is_active, mono=lambda: now[0]), now, calls, state


async def test_a_silent_screen_page_gets_the_display_restarted_with_growing_pauses():
    w, now, calls, _ = _watch()
    now[0] += dw.DEAD_S - 1
    assert not await w.check() and calls == []  # ядро только что стартовало: страница ещё поднимается
    w.alive()
    now[0] += dw.DEAD_S - 1
    assert not await w.check()
    now[0] += 2  # тишина дольше DEAD_S
    assert await w.check() and calls == ["restart"]
    now[0] += dw.RETRY_S[0] - 1
    assert not await w.check()  # страница поднимается
    now[0] += 2
    assert await w.check() and calls == ["restart"] * 2  # так и не ожила
    now[0] += dw.RETRY_S[0] + 1
    assert not await w.check()  # теперь пауза длиннее
    now[0] += dw.RETRY_S[1]
    assert await w.check() and w.restarts == 3
    w.alive()  # ожила: снова с первой паузы
    assert w.restarts == 0


async def test_a_display_stopped_on_purpose_is_left_alone_and_gets_time_when_it_is_back():
    """fnav-display остановлена ради игры: сигналов нет, но перезапускать нечего; вернулась — страница поднимается,
    отсчёт тишины — с её возврата."""
    w, now, calls, state = _watch(active=False)
    w.alive()
    for _ in range(10):
        now[0] += dw.DEAD_S
        assert not await w.check()
    state["active"] = True
    now[0] += dw.CHECK_S
    assert not await w.check() and calls == []
    now[0] += dw.DEAD_S
    assert await w.check() and calls == ["restart"]


async def test_only_the_screen_page_counts_as_alive(live_server):
    """?client=display с самого Pi — сигнал сторожу (при подключении и по "alive"); страница без пометки (браузер
    в локальной сети, админка) — нет."""
    srv = live_server()
    core = srv.app.state.core
    seen: list[int] = []
    core.ws.on_display_alive = lambda: seen.append(1)

    async def alive_then_pong(ws):
        await ws.send(json.dumps({"type": "alive"}))
        await ws.send(json.dumps({"type": "ping"}))
        while json.loads(await asyncio.wait_for(ws.recv(), 3))["type"] != "pong":
            pass

    async with connect(srv.ws_url) as ws:
        await alive_then_pong(ws)
    assert seen == []
    async with connect(srv.ws_url + "?client=display") as ws:
        await alive_then_pong(ws)
    assert len(seen) == 2
