"""Приём вещания Bluetooth LE (advertisements) без подключения — для устройств, которые сами рассылают показания:
Victron SmartSolar (fnav/core/victron.py) и датчик воздуха Qingping (fnav/core/air.py). docs/contracts/power.md
«Вещание», docs/contracts/air.md.

Слушаем окнами: WINDOW_S секунд раз в PERIOD_S, а пока играет музыка — раз в PERIOD_MUSIC_S (радио у чипа одно на
Wi-Fi, колонку и станции; поиск идёт «редким» окном 30 из 200 мс — pi/bluetooth/main.conf). Пока на экране задний
вид (quiet) — не слушаем. Окно берёт общий замок подключений станций (fnav/core/power.py): поиск и установление
связи друг другу мешают.

BlueZ отдаёт вещание свойствами устройства: ManufacturerData {id: bytes}, ServiceData {uuid: bytes}; с фильтром
DuplicateData они приходят на каждый пакет.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from fnav.core.util import cancel_tasks

log = logging.getLogger("fnav.core.advscan")

WINDOW_S = 15.0
PERIOD_S = 60.0
PERIOD_MUSIC_S = 180.0
QUIET_POLL_S = 2.0
BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
ADAPTER_IF, DEVICE_IF = "org.bluez.Adapter1", "org.bluez.Device1"

# (данные производителя {id: bytes}, данные сервисов {uuid: bytes})
AdvCallback = Callable[[dict[int, bytes], dict[str, bytes]], None]


def _plain(v: Any) -> Any:
    return v.value if hasattr(v, "signature") and hasattr(v, "value") else v


class AdvScanner:
    """watch(mac, cb) — звать cb на каждый пакет устройства; unwatch(mac). Без подписчиков эфир не слушаем."""

    def __init__(self, quiet: Callable[[], bool] = lambda: False, busy: Callable[[], bool] = lambda: False,
                 lock: asyncio.Lock | None = None, adapter: str = ADAPTER) -> None:
        self.quiet = quiet
        self.busy = busy  # играет музыка — слушать реже
        self.lock = lock or asyncio.Lock()
        self.adapter = adapter
        self.watched: dict[str, AdvCallback] = {}  # путь устройства в BlueZ → подписчик
        self.error: str | None = None
        self.bus: Any = None
        self._task: asyncio.Task | None = None

    def _path(self, mac: str) -> str:
        return f"{self.adapter}/dev_{mac.upper().replace(':', '_')}"

    def watch(self, mac: str, cb: AdvCallback) -> None:
        self.watched[self._path(mac)] = cb

    def unwatch(self, mac: str) -> None:
        self.watched.pop(self._path(mac), None)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="advscan")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        self._task = None
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.disconnect()
        self.bus = None

    def period_s(self) -> float:
        return PERIOD_MUSIC_S if self.busy() else PERIOD_S

    async def _run(self) -> None:
        while True:
            if not self.watched or self.quiet():
                await asyncio.sleep(QUIET_POLL_S)
                continue
            try:
                async with self.lock:
                    await self._window()
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — нет dbus-fast, шина, адаптер выключен
                if str(e) != self.error:
                    log.info("advscan: %s", e)
                self.error = str(e) or type(e).__name__
            await asyncio.sleep(max(1.0, self.period_s() - WINDOW_S))

    # ── BlueZ ──
    async def _open(self) -> None:
        if self.bus is not None:
            return
        from dbus_fast import BusType, Message  # type: ignore[import-not-found]
        from dbus_fast.aio import MessageBus  # type: ignore[import-not-found]

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.add_message_handler(self._on_message)
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                                    body=[f"type='signal',sender='{BLUEZ}',path_namespace='{self.adapter}'"]))

    async def _adapter(self, member: str, signature: str = "", body: list | None = None) -> None:
        from dbus_fast import Message, MessageType  # type: ignore[import-not-found]

        async with asyncio.timeout(10):
            reply = await self.bus.call(Message(destination=BLUEZ, path=self.adapter, interface=ADAPTER_IF,
                                                member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {(reply.error_name or '').rsplit('.', 1)[-1]}")

    def _on_message(self, msg: Any) -> bool:
        try:
            cb = self.watched.get(msg.path or "")
            if cb is None or not msg.body:
                return False
            if msg.member == "PropertiesChanged" and msg.body[0] == DEVICE_IF:
                props = msg.body[1]
            elif msg.member == "InterfacesAdded":
                props = msg.body[1].get(DEVICE_IF, {})
            else:
                return False
            mfr = {int(k): bytes(_plain(v)) for k, v in (_plain(props.get("ManufacturerData")) or {}).items()}
            svc = {str(k).lower(): bytes(_plain(v)) for k, v in (_plain(props.get("ServiceData")) or {}).items()}
            if mfr or svc:
                cb(mfr, svc)
        except Exception:
            log.exception("advscan signal")
        return False

    async def _window(self) -> None:
        from dbus_fast import Variant  # type: ignore[import-not-found]

        await self._open()
        with contextlib.suppress(RuntimeError):
            await self._adapter("SetDiscoveryFilter", "a{sv}",
                                [{"Transport": Variant("s", "le"), "DuplicateData": Variant("b", True)}])
        await self._adapter("StartDiscovery")
        try:
            await asyncio.sleep(WINDOW_S)
        finally:
            with contextlib.suppress(Exception):
                await self._adapter("StopDiscovery")


class FakeAdvScanner(AdvScanner):
    """--mock: «устройства» вещают настоящими кадрами (feed — словарь mac → функция, дающая (mfr, svc))."""

    def __init__(self, period_s: float = 5.0) -> None:
        super().__init__()
        self.feeds: dict[str, Callable[[], tuple[dict[int, bytes], dict[str, bytes]]]] = {}
        self.fake_period_s = period_s

    def feed(self, mac: str, fn: Callable[[], tuple[dict[int, bytes], dict[str, bytes]]]) -> None:
        self.feeds[self._path(mac)] = fn

    async def _run(self) -> None:
        while True:
            for path, cb in list(self.watched.items()):
                fn = self.feeds.get(path)
                if fn is not None:
                    cb(*fn())
            await asyncio.sleep(self.fake_period_s)
