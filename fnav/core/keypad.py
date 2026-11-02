"""Мини-клавиатура: 6 клавиш + крутилка (USB 1189:8890, CH57x), прошита клавишами F13–F21 и Enter
(pi/keypad/fnav-keypad.yaml, утилита ch57x-keyboard-tool).

Клавиши читаются из evdev без захвата: экран (Chromium) и игры получают те же нажатия, но F13–F21 никак не
используют. Что делает каждая клавиша — settings.keypad (меняется на странице Controls, без перепрошивки).
По умолчанию:

    [1 станция назад]  [2 станция вперёд]  [3 карта ↔ камера]
    [4 камера: передняя ↔ задняя]  [5 защитить запись]  [6 следующая станция: по порядку → случайная → избранное]
                                                        [6 удержание: станцию в избранное]
    крутилка: влево / вправо — громкость, нажатие — радио вкл/выкл

У клавиш и нажатия крутилки может быть второе действие — удержание (settings.keypad.*_hold, дольше HOLD_S).
У такой клавиши нажатие срабатывает при отпускании, у остальных — сразу. Длительность нажатия — по меткам времени
событий evdev, не по тому, когда их прочитали.

Устройство ждём через loop.add_reader (без опроса). Действия выполняются задачами, чтобы долгое (смена станции
через каталог radio-browser на слабой связи — до минуты) не держало остальные клавиши: действия радио — в одной
очереди по порядку нажатий, громкость — в своей, прочие — каждое в своей.

Меню игр — только с геймпада (он подключается вместо этой клавиатуры).
"""

from __future__ import annotations

import asyncio
import collections
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
EVIOCSCLOCKID = 0x400445A0  # _IOW('E', 0xa0, int): метки времени событий — по CLOCK_MONOTONIC
CLOCK_MONOTONIC = 1
RESCAN_S = 5.0  # клавиатуру подключили — заметим не позже
QUEUE_MAX = 8  # столько нажатий одного вида ждут своей очереди, лишние — мимо

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
# очередь действия: радио — одна на все (порядок важен: «следующая станция», затем «в избранное» — той самой),
# громкость — своя; остальные действия — каждое в своей (по id)
QUEUES = {**dict.fromkeys(("station_prev", "station_next", "radio_toggle", "shuffle", "favorite"), "radio"),
          "volume_down": "volume", "volume_up": "volume"}

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


def _monotonic_events(fd: int) -> bool:
    """Метки времени событий этого дескриптора — по CLOCK_MONOTONIC (по умолчанию — настенные часы, а они
    у Pi без RTC прыгают при синхронизации). False — не вышло: пересчитываем из настенных."""
    try:
        import fcntl

        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", CLOCK_MONOTONIC))
        return True
    except (ImportError, OSError):
        return False


class KeypadService:
    def __init__(self, handlers: dict[str, Callable[[], Awaitable[object]]],
                 get_bindings: Callable[[], KeypadSettings] = KeypadSettings) -> None:
        self.handlers = handlers
        self.get_bindings = get_bindings
        self.fds: dict[str, int] = {}
        self._mono: set[str] = set()  # устройства с метками времени по CLOCK_MONOTONIC
        self._task: asyncio.Task | None = None
        self._held: dict[int, tuple[float, str, str]] = {}  # код → (когда нажата, нажатие, удержание)
        self._hold_timer: asyncio.TimerHandle | None = None
        self._queues: dict[str, collections.deque[str]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._add_reader_failed = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="keypad")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._workers.values()) if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None
        for path in list(self.fds):
            self._drop(path)

    def _drop(self, path: str) -> None:
        fd = self.fds.pop(path, None)
        self._mono.discard(path)
        if fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(fd)
            with contextlib.suppress(OSError):
                os.close(fd)

    async def _loop(self) -> None:
        while True:
            self._rescan()
            await asyncio.sleep(RESCAN_S)

    def _rescan(self) -> None:
        loop = asyncio.get_running_loop()
        for path in keypad_events():
            if path in self.fds:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                log.debug("keypad %s: %s", path, e)
                continue
            try:
                loop.add_reader(fd, self._on_readable, path)
            except (NotImplementedError, OSError, ValueError) as e:  # цикл без add_reader (Windows Proactor)
                os.close(fd)
                if not self._add_reader_failed:
                    log.warning("keypad %s: cannot wait for key presses: %s", path, e)
                    self._add_reader_failed = True
                continue
            self.fds[path] = fd
            if _monotonic_events(fd):
                self._mono.add(path)
            log.info("keypad: %s", path)

    def _on_readable(self, path: str) -> None:
        fd = self.fds.get(path)
        if fd is None:
            return
        now = time.monotonic()
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # клавиатуру отключили
            self._drop(path)
            self._held.clear()
            self._arm_hold()
            return
        offset = 0.0 if path in self._mono else now - time.time()
        for act in self.decode(data, now, offset):
            self.submit(act)
        self._arm_hold()

    def decode(self, data: bytes, now: float | None = None, offset: float = 0.0) -> list[str]:
        """События клавиш → действия по текущей раскладке. Клавиша без удержания — сразу при нажатии;
        с удержанием — при отпускании: короче HOLD_S — нажатие, дольше — удержание (если оно ещё не сработало
        в held()). Время — по меткам событий (+ offset → time.monotonic): нажатие и отпускание, прочитанные
        разом после задержки, остаются удержанием. Без метки — время чтения now. Автоповтор (value=2) — нет."""
        now = time.monotonic() if now is None else now
        bindings = self.get_bindings()
        acts = []
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            sec, usec, etype, code, value = EVENT.unpack_from(data, off)
            if etype != EV_KEY or code not in PLACES:
                continue
            t = min(now, sec + usec / 1e6 + offset) if sec or usec else now
            place = PLACES[code]
            if value == 1:
                press = getattr(bindings, place)
                hold = getattr(bindings, place + "_hold", "none")
                if hold != "none":
                    self._held[code] = (t, press, hold)
                elif press != "none":
                    acts.append(press)
            elif value == 0 and code in self._held:
                t0, press, hold = self._held.pop(code)
                if hold and t - t0 >= HOLD_S:  # держали, но held() не успел (события прочитаны поздно)
                    acts.append(hold)
                elif hold and press != "none":  # удержание ещё не сработало — это было нажатие
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

    def _arm_hold(self) -> None:
        """Таймер на ближайшее удержание (клавиша ещё нажата — оно сработает, не дожидаясь отпускания)."""
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None
        due = [t0 + HOLD_S for t0, _press, hold in self._held.values() if hold]
        if due:
            delay = max(0.0, min(due) - time.monotonic()) + 0.005
            self._hold_timer = asyncio.get_running_loop().call_later(delay, self._on_hold_timer)

    def _on_hold_timer(self) -> None:
        self._hold_timer = None
        for act in self.held():
            self.submit(act)
        self._arm_hold()

    def submit(self, action: str) -> None:
        """Выполнить действие задачей — в его очереди (QUEUES), после нажатий, что уже ждут в ней."""
        if action not in self.handlers:
            return
        name = QUEUES.get(action, action)
        q = self._queues.setdefault(name, collections.deque())
        if len(q) >= QUEUE_MAX:
            log.info("keypad %s: %d presses are still waiting, this one is dropped", action, len(q))
            return
        q.append(action)
        if name not in self._workers:
            self._workers[name] = asyncio.get_running_loop().create_task(self._drain(name), name=f"keypad-{name}")

    async def _drain(self, name: str) -> None:
        q = self._queues[name]
        try:
            while q:
                await self.dispatch(q.popleft())
        finally:
            self._workers.pop(name, None)

    async def dispatch(self, action: str) -> None:
        fn = self.handlers.get(action)
        if fn is None:
            return
        try:
            await fn()
        except Exception as e:  # noqa: BLE001 — одно нажатие не должно ронять очередь
            log.warning("keypad %s: %s", action, e)
