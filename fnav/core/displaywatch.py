"""Сторож экрана: страница кинескопа (Chromium в fnav-display) умерла — перезапустить fnav-display.

10 ноября 2026 страница погибла вскоре после загрузки Pi — молча: ни нехватки памяти, ни отчёта о сбое; Chromium
упавшую вкладку сам не открывает, а оболочка (cage) жива, служба «active». Экран был чёрным почти двое суток.

Страница экрана подключается к /ws с ``?client=display`` (только с самого Pi — ``Auth.is_local``) и шлёт
``{"type": "alive"}`` раз в ~10 с — в ответ на телеметрию, не по таймеру: таймеры скрытой страницы (видеовыход
выключен) Chromium замедляет до раза в минуту, а приём сообщений — нет. Упала или зависла — сигналов нет. Нет их дольше
DEAD_S, а служба запущена (остановленную ради игры не трогаем) — ``systemctl restart fnav-display``; следующий
перезапуск — не раньше паузы RETRY_S (растёт, пока страница так и не ожила).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("fnav.core.display")

DEAD_S = 120.0  # сигналов нет дольше — страница мертва (с запасом на замедление скрытой страницы до раза в минуту)
CHECK_S = 15.0
RETRY_S = (180.0, 600.0, 1800.0)  # пауза после перезапуска: страница поднимается; не ожила — реже


class DisplayWatch:
    def __init__(self, restart: Callable[[], Awaitable[bool]], active: Callable[[], Awaitable[bool]],
                 mono: Callable[[], float] = time.monotonic) -> None:
        self._restart = restart
        self._active = active
        self._mono = mono
        self._since = mono()  # отсчёт тишины: последний сигнал, старт ядра, перезапуск или возврат службы
        self._hold_until = 0.0
        self.restarts = 0  # перезапусков подряд без сигнала от страницы
        self.alive_at: float | None = None

    def alive(self) -> None:
        """Страница экрана подала сигнал (или только что подключилась)."""
        self.alive_at = self._since = self._mono()
        if self.restarts:
            log.info("display: the screen page is alive again")
        self.restarts = 0

    async def check(self) -> bool:
        """True — перезапустили fnav-display."""
        now = self._mono()
        if now - self._since < DEAD_S or now < self._hold_until:
            return False
        if not await self._active():  # остановлена нарочно (игра) — вернётся, отсчёт с её возврата
            self._since = now
            return False
        silent = now - (self.alive_at if self.alive_at is not None else self._since)
        log.warning("display: no sign of life from the screen page for %.0f s - restarting fnav-display", silent)
        ok = await self._restart()
        if not ok:
            log.warning("display: restarting fnav-display failed")
        self._hold_until = now + RETRY_S[min(self.restarts, len(RETRY_S) - 1)]
        self.restarts += 1
        self._since = now
        return True

    async def run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                await self.check()
            except Exception:
                log.exception("display watch")


async def restart_display() -> bool:
    from fnav.core.system import run

    rc, _out, err = await run("sudo", "-n", "/usr/bin/systemctl", "restart", "fnav-display", timeout=60)
    if rc != 0:
        log.warning("display: systemctl restart fnav-display: %s", (err or "").strip()[:200] or rc)
    return rc == 0


async def display_active() -> bool:
    from fnav.core.system import run

    _rc, out, _err = await run("systemctl", "is-active", "fnav-display", timeout=10)
    return out.strip() == "active"
