"""Мелкие общие помощники fnav-core."""

from __future__ import annotations

import asyncio
import contextlib


async def cancel_tasks(*tasks: asyncio.Task | None) -> None:
    """Отменить задачи и дождаться их завершения, проглотив отмену/ошибки."""
    real = [t for t in tasks if t is not None]
    for t in real:
        t.cancel()
    for t in real:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


_BACKGROUND: set[asyncio.Task] = set()


def spawn(coro, *, name: str | None = None) -> asyncio.Task:
    """create_task с удержанием ссылки (иначе «висящую» задачу может собрать GC)."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task
