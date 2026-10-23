"""Мини-клавиатура: 6 клавиш + крутилка (USB 1189:8890, CH57x), прошита клавишами F13–F21 и Enter
(pi/keypad/fnav-keypad.yaml, утилита ch57x-keyboard-tool).

Клавиши читаются из evdev без захвата: экран (Chromium) и игры получают те же нажатия, но F13–F21 никак не
используют. Что делает каждая клавиша — settings.keypad (меняется на странице Controls, без перепрошивки).
По умолчанию:

    [1 станция назад]  [2 станция вперёд]  [3 карта ↔ камера]
    [4 видеовыход]     [5 защитить запись] [6 следующая станция: по порядку ↔ случайная]
    крутилка: влево / вправо — громкость, нажатие — радио вкл/выкл

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

from fnav.models import KeypadSettings

log = logging.getLogger("fnav.core.keypad")

KEYPAD_IDS = {tuple(x.split(":")) for x in os.environ.get("FNAV_KEYPAD_IDS", "1189:8890").split(",") if ":" in x}
EV_KEY = 1
EVENT = struct.Struct("llHHi")
KEY_ENTER = 28
KEY_F13 = 183

# код клавиши (так прошита клавиатура) → место на клавиатуре (поле settings.keypad)
PLACES: dict[int, str] = {
    KEY_F13: "key1", KEY_F13 + 1: "key2", KEY_F13 + 2: "key3",
    KEY_F13 + 3: "key4", KEY_F13 + 4: "key5", KEY_F13 + 5: "key6",
    KEY_F13 + 6: "knob_left", KEY_F13 + 8: "knob_right", KEY_ENTER: "knob_press",
}
PLACE_LABELS = {"key1": "Key 1", "key2": "Key 2", "key3": "Key 3", "key4": "Key 4", "key5": "Key 5", "key6": "Key 6",
                "knob_left": "Knob left", "knob_right": "Knob right", "knob_press": "Knob press"}

# действия — для веб-интерфейса (страница Controls); id — как в fnav.models.KeypadAction
ACTIONS: list[dict[str, str]] = [
    {"id": "station_prev", "title": "Previous station", "hint": "Shuffle mode: back along the stations you heard"},
    {"id": "station_next", "title": "Next station", "hint": "Through the current genre: in order, or random"},
    {"id": "radio_toggle", "title": "Radio start / stop", "hint": "Starts the last station; does nothing in a game"},
    {"id": "shuffle", "title": "Next station: in order / random",
     "hint": "Shown on the radio panel; in a game: exit to the map"},
    {"id": "volume_down", "title": "Volume down", "hint": "Radio volume; in a game: the speaker volume"},
    {"id": "volume_up", "title": "Volume up", "hint": "Radio volume; in a game: the speaker volume"},
    {"id": "view_toggle", "title": "Map / camera",
     "hint": "Switches to the other view; press again to go back to automatic switching by speed"},
    {"id": "video_toggle", "title": "Video output on / off", "hint": "Turns the picture on the car monitor on or off"},
    {"id": "protect", "title": "Protect recording", "hint": "The current recording is never deleted to free space"},
    {"id": "none", "title": "Nothing", "hint": "The key is not used"},
]


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
    def __init__(self, handlers: dict[str, Callable[[], Awaitable[object]]],
                 get_bindings: Callable[[], KeypadSettings] = KeypadSettings) -> None:
        self.handlers = handlers
        self.get_bindings = get_bindings
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
        """Нажатия (value=1; автоповтор и отпускание — нет) → действия по текущей раскладке."""
        bindings = self.get_bindings()
        acts = []
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _s, _us, etype, code, value = EVENT.unpack_from(data, off)
            if etype == EV_KEY and value == 1 and code in PLACES:
                act = getattr(bindings, PLACES[code])
                if act != "none":
                    acts.append(act)
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
