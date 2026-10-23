"""Мини-клавиатура: 6 клавиш + крутилка (USB 1189:8890, CH57x), прошита клавишами F13–F21
(pi/keypad/fnav-keypad.yaml, утилита ch57x-keyboard-tool).

Клавиши читаются из evdev без захвата: экран (Chromium) и игры получают те же нажатия, но F13–F21 никак не
используют. Раскладка:

    [1 F13 станция назад] [2 F14 станция вперёд]  [3 F15 карта ↔ камера]
    [4 F16 видеовыход]    [5 F17 защитить запись] [6 F18 следующая станция: по порядку ↔ случайная]
    крутилка: влево F19 / вправо F21 — громкость, нажатие — Enter (подтверждение в RetroArch и играх)

Меню игр — только с геймпада (он подключается вместо этой клавиатуры).
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import struct
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("fnav.core.keypad")

KEYPAD_IDS = {tuple(x.split(":")) for x in os.environ.get("FNAV_KEYPAD_IDS", "1189:8890").split(",") if ":" in x}
EV_KEY = 1
EVENT = struct.Struct("llHHi")
KEY_F13 = 183

# код клавиши → действие
ACTIONS: dict[int, str] = {
    KEY_F13: "station_prev",
    KEY_F13 + 1: "station_next",
    KEY_F13 + 2: "view_toggle",
    KEY_F13 + 3: "video_toggle",
    KEY_F13 + 4: "protect",
    KEY_F13 + 5: "shuffle",
    KEY_F13 + 6: "volume_down",
    KEY_F13 + 8: "volume_up",
    # нажатие крутилки — Enter: подтверждение там, где оно есть (RetroArch, игры); ядру делать нечего
}


def keypad_events(pattern: str = "/sys/class/input/event*") -> list[str]:
    """Устройства /dev/input/eventN клавиатуры (по vendor:product из sysfs)."""
    out = []
    for p in sorted(glob.glob(pattern)):
        try:
            with open(f"{p}/device/id/vendor") as fv, open(f"{p}/device/id/product") as fp:
                vid, pid = fv.read().strip(), fp.read().strip()
        except OSError:
            continue
        if (vid, pid) in KEYPAD_IDS:
            out.append("/dev/input/" + os.path.basename(p))
    return out


class KeypadService:
    def __init__(self, handlers: dict[str, Callable[[], Awaitable[object]]]) -> None:
        self.handlers = handlers
        self.fds: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._rescan_at = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="keypad")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for path in list(self.fds):
            self._drop(path)

    def _drop(self, path: str) -> None:
        fd = self.fds.pop(path, None)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _rescan(self) -> None:
        for path in keypad_events():
            if path not in self.fds:
                try:
                    self.fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    log.info("keypad: %s", path)
                except OSError as e:
                    log.debug("keypad %s: %s", path, e)

    def decode(self, data: bytes) -> list[str]:
        """Нажатия (value=1; автоповтор и отпускание — нет) → действия."""
        acts = []
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _s, _us, etype, code, value = EVENT.unpack_from(data, off)
            if etype == EV_KEY and value == 1 and code in ACTIONS:
                acts.append(ACTIONS[code])
        return acts

    async def dispatch(self, action: str) -> None:
        fn = self.handlers.get(action)
        if fn is None:
            return
        try:
            await fn()
        except Exception as e:  # noqa: BLE001 — одно нажатие не должно ронять цикл
            log.warning("keypad %s: %s", action, e)

    async def _loop(self) -> None:
        while True:
            now = time.monotonic()
            if now >= self._rescan_at:
                self._rescan_at = now + 5.0
                self._rescan()
            for path, fd in list(self.fds.items()):
                try:
                    data = os.read(fd, EVENT.size * 32)
                except BlockingIOError:
                    continue
                except OSError:  # клавиатуру отключили
                    self._drop(path)
                    continue
                for act in self.decode(data):
                    await self.dispatch(act)
            await asyncio.sleep(0.03 if self.fds else 1.0)
