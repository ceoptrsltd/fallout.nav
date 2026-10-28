"""Мини-клавиатура: 6 клавиш + крутилка (USB 1189:8890, CH57x), прошита клавишами F13–F21 и Enter
(pi/keypad/fnav-keypad.yaml, утилита ch57x-keyboard-tool).

Клавиши читаются из evdev без захвата: экран (Chromium) и игры получают те же нажатия, но F13–F21 никак не
используют. Что делает каждая клавиша — settings.keypad (меняется на странице Controls, без перепрошивки).
По умолчанию:

    [1 станция назад]  [2 станция вперёд]  [3 карта ↔ камера]
    [4 видеовыход]     [5 защитить запись] [6 следующая станция: по порядку → случайная → избранное]
                                           [6 удержание: станцию в избранное]
    крутилка: влево / вправо — громкость, нажатие — радио вкл/выкл

У клавиш и нажатия крутилки может быть второе действие — удержание (settings.keypad.*_hold, дольше HOLD_S).
У такой клавиши нажатие срабатывает при отпускании, у остальных — сразу.

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
# места, у которых есть удержание (крутилку не «держат»): поле settings.keypad — место + "_hold"
HOLD_PLACES = ("key1", "key2", "key3", "key4", "key5", "key6", "knob_press")
HOLD_S = 0.8

# действия — для веб-интерфейса (страница Controls); id — как в fnav.models.KeypadAction
ACTIONS: list[dict[str, str]] = [
    {"id": "station_prev", "title": "Previous station", "hint": "Random mode: back along the stations you heard"},
    {"id": "station_next", "title": "Next station",
     "hint": "In order through the current genre, at random from the whole catalogue (any genre), "
             "or through the favorites (the radio mode key)"},
    {"id": "radio_toggle", "title": "Radio start / stop", "hint": "Starts the last station; does nothing in a game"},
    {"id": "shuffle", "title": "Next station: in order / random / favorites",
     "hint": "Shown on the radio panel; favorites are skipped while there are none; in a game: exit to the map"},
    {"id": "favorite", "title": "Add the station to favorites",
     "hint": "The station playing now; remove favorites on the Radio page"},
    {"id": "volume_down", "title": "Volume down", "hint": "Radio volume; in a game: the speaker volume"},
    {"id": "volume_up", "title": "Volume up", "hint": "Radio volume; in a game: the speaker volume"},
    {"id": "view_toggle", "title": "Map / camera",
     "hint": "Switches to the other view; press again to go back to automatic switching by speed"},
    {"id": "camera_switch", "title": "Front / rear camera",
     "hint": "Shows the rear camera on the car screen; press again to go back (needs the rear camera turned on)"},
    {"id": "video_toggle", "title": "Video output on / off", "hint": "Turns the picture on the car monitor on or off"},
    {"id": "protect", "title": "Protect recording", "hint": "The current recording is never deleted to free space"},
    {"id": "alarm", "title": "Alarm on/off",
     "hint": "Arms the alarm (after the arming delay) or turns it off; also stops the siren"},
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
        self._held: dict[int, tuple[float, str, str]] = {}  # код → (когда нажата, нажатие, удержание)

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

    def decode(self, data: bytes, now: float | None = None) -> list[str]:
        """События клавиш → действия по текущей раскладке. Клавиша без удержания — сразу при нажатии;
        с удержанием — при отпускании (короче HOLD_S) или в held() (дольше). Автоповтор (value=2) — нет."""
        now = time.monotonic() if now is None else now
        bindings = self.get_bindings()
        acts = []
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _s, _us, etype, code, value = EVENT.unpack_from(data, off)
            if etype != EV_KEY or code not in PLACES:
                continue
            place = PLACES[code]
            if value == 1:
                press = getattr(bindings, place)
                hold = getattr(bindings, place + "_hold", "none")
                if hold != "none":
                    self._held[code] = (now, press, hold)
                elif press != "none":
                    acts.append(press)
            elif value == 0 and code in self._held:
                _t0, press, hold = self._held.pop(code)
                if hold and press != "none":  # удержание ещё не сработало — это было нажатие
                    acts.append(press)
        return acts

    def held(self, now: float | None = None) -> list[str]:
        """Удержания, дошедшие до HOLD_S, пока клавиша ещё нажата (срабатывают один раз, не дожидаясь отпускания)."""
        now = time.monotonic() if now is None else now
        acts = []
        for code, (t0, press, hold) in list(self._held.items()):
            if hold and now - t0 >= HOLD_S:
                self._held[code] = (t0, press, "")
                acts.append(hold)
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
                    self._held.clear()
                    continue
                for act in self.decode(data, now):
                    await self.dispatch(act)
            for act in self.held(now):
                await self.dispatch(act)
            await asyncio.sleep(0.03 if self.fds else 1.0)
