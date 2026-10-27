"""Одна громкость у колонки: AVRCP absolute volume через bluealsa.

bluealsa по умолчанию держит у PCM программную громкость (SoftVolume): колонка, bluealsa и mpv регулировали каждый
своё. Здесь у PCM колонки программная громкость выключается — громкость PCM (0…127) уходит в колонку по AVRCP и
становится громкостью радио; кнопки на колонке меняют её же, радио узнаёт из PropertiesChanged. mpv при этом играет
на 100. Колонка без AVRCP-громкости (или нет bluealsa/dbus-fast) — apply() возвращает False, радио регулирует mpv.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("fnav.core.btvolume")

BLUEALSA = "org.bluealsa"
PCM_IF = "org.bluealsa.PCM1"
OM_IF = "org.freedesktop.DBus.ObjectManager"
PROPS_IF = "org.freedesktop.DBus.Properties"
LEVEL_MAX = 127
ECHO_S = 2.0  # после нашей установки колонка присылает своё (с округлением шага) — это не нажатие кнопки
ECHO_LEVELS = 2


def pct_to_level(pct: int) -> int:
    return round(max(0, min(100, pct)) * LEVEL_MAX / 100)


def level_to_pct(level: int) -> int:
    return round(max(0, min(LEVEL_MAX, level)) * 100 / LEVEL_MAX)


def pack(level: int) -> int:
    """Volume (q): старший байт — левый канал, младший — правый; бит 7 — mute."""
    return (level << 8) | level


def unpack(q: int) -> int:
    return max((q >> 8) & 0x7F, q & 0x7F)


def _dev_path(address: str) -> str:
    return "/dev_" + address.upper().replace(":", "_")


def _variant(sig: str, value: Any) -> Any:
    try:
        from dbus_fast import Variant  # type: ignore[import-not-found]
    except ImportError:  # тесты без dbus-fast
        return (sig, value)
    return Variant(sig, value)


def _val(v: Any) -> Any:
    return v.value if hasattr(v, "signature") and hasattr(v, "value") else v


class SpeakerVolume:
    """Громкость PCM колонки (A2DP-source/sink) в bluealsa по системной шине; on_change(pct) — с кнопок колонки."""

    def __init__(self, on_change: Callable[[int], None] | None = None, on_pcm: Callable[[], None] | None = None,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.on_change = on_change
        self.on_pcm = on_pcm  # PCM колонки появился — пора выставить громкость
        self.mono = mono
        self.bus: Any = None
        self.address: str | None = None
        self.pcm: str | None = None
        self.native = False  # SoftVolume выключен — громкость PCM = громкость колонки
        self._sent: tuple[int, float] | None = None  # (уровень, когда) — наша последняя установка
        self._level: int | None = None

    # ── шина ──
    async def start(self) -> None:
        try:
            from dbus_fast import BusType  # type: ignore[import-not-found]
            from dbus_fast.aio import MessageBus  # type: ignore[import-not-found]

            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self.bus.add_message_handler(self._on_message)
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                             "s", [f"type='signal',sender='{BLUEALSA}'"])
        except Exception as e:  # noqa: BLE001 — нет шины / dbus-fast: громкость регулирует mpv
            log.info("speaker volume: no system bus (%s)", e)
            self.bus = None
            return
        await self._find()

    async def stop(self) -> None:
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.disconnect()
        self.bus = None

    async def _call(self, dest: str, path: str, iface: str, member: str, signature: str = "",
                    body: list | None = None, timeout: float = 3.0) -> list:
        from dbus_fast import Message, MessageType  # type: ignore[import-not-found]

        msg = Message(destination=dest, path=path, interface=iface, member=member, signature=signature,
                      body=body or [])
        reply = await asyncio.wait_for(self.bus.call(msg), timeout)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return list(reply.body or [])

    # ── какая колонка ──
    def select(self, address: str | None) -> None:
        if address == self.address:
            return
        self.address, self.pcm, self.native, self._level = address, None, False, None
        if self.bus is not None and address:
            asyncio.get_running_loop().create_task(self._find(), name="speaker-volume-find")

    def _matches(self, path: str, props: dict) -> bool:
        return bool(self.address and _dev_path(self.address) in str(_val(props.get("Device", ""))) and
                    _val(props.get("Transport")) == "A2DP-source" and _val(props.get("Mode")) == "sink"
                    and path)

    async def _find(self) -> None:
        if self.bus is None or not self.address:
            return
        try:
            (objs,) = await self._call(BLUEALSA, "/org/bluealsa", OM_IF, "GetManagedObjects")
        except Exception as e:  # noqa: BLE001 — bluealsa не запущен: найдём по InterfacesAdded
            log.debug("speaker volume: bluealsa: %s", e)
            return
        for path, ifs in objs.items():
            props = ifs.get(PCM_IF)
            if props is not None and self._matches(path, props):
                self._take(path, props)
                return

    def _take(self, path: str, props: dict) -> None:
        self.pcm = path
        self.native = _val(props.get("SoftVolume")) is False
        vol = _val(props.get("Volume"))
        self._level = unpack(vol) if isinstance(vol, int) else None
        log.info("speaker volume: %s (%s)", path, "native" if self.native else "software")
        if self.on_pcm:
            self.on_pcm()

    # ── громкость ──
    async def apply(self, pct: int) -> bool:
        """Громкость колонки, %. True — выставлена в самой колонке (mpv держать на 100), False — не получилось."""
        if self.bus is None or self.pcm is None:
            return False
        level = pct_to_level(pct)
        try:
            if self._level != level or not self.native:
                self._sent = (level, self.mono())
                await self._call(BLUEALSA, self.pcm, PROPS_IF, "Set", "ssv", [PCM_IF, "Volume", _variant("q", pack(level))])
                self._level = level
            if not self.native:  # сначала уровень, потом выключить программную: колонка не «прыгает»
                await self._call(BLUEALSA, self.pcm, PROPS_IF, "Set", "ssv", [PCM_IF, "SoftVolume", _variant("b", False)])
                self.native = True
                log.info("speaker volume: native (AVRCP) at %d%%", pct)
        except Exception as e:  # noqa: BLE001 — PCM пропал / колонка не умеет: регулирует mpv
            log.warning("speaker volume: %s", e)
            return False
        return True

    def _on_message(self, msg: Any) -> None:
        try:
            if msg.member == "PropertiesChanged" and msg.path == self.pcm and msg.body and msg.body[0] == PCM_IF:
                changed = msg.body[1]
                if "SoftVolume" in changed:
                    self.native = _val(changed["SoftVolume"]) is False
                if "Volume" in changed:
                    self._remote(unpack(_val(changed["Volume"])))
            elif msg.member == "InterfacesAdded" and msg.body and PCM_IF in msg.body[1]:
                if self.pcm is None and self._matches(msg.body[0], msg.body[1][PCM_IF]):
                    self._take(msg.body[0], msg.body[1][PCM_IF])
            elif msg.member == "InterfacesRemoved" and msg.body and msg.body[0] == self.pcm:
                self.pcm, self.native, self._level = None, False, None
        except Exception:
            log.exception("speaker volume signal")

    def _remote(self, level: int) -> None:
        prev, self._level = self._level, level
        sent = self._sent
        if sent and self.mono() - sent[1] < ECHO_S and abs(level - sent[0]) <= ECHO_LEVELS:
            return  # эхо нашей установки
        if level != prev and self.native and self.on_change:
            self.on_change(level_to_pct(level))
