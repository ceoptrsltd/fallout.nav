"""Bluetooth LE: подключение к одному устройству через BlueZ по D-Bus (dbus-fast) — подписки и запись в GATT.

Для электростанций (fnav/core/power.py, docs/contracts/power.md). У каждого устройства своё соединение с системной
шиной: фильтр поиска LE не мешает поиску колонок (fnav/core/bluetooth.py), сигналы — только этого устройства.

Встроенный чип Pi (CYW43455) подключение LE устанавливает не с первого раза: BlueZ «Connect» нередко заканчивается
``le-connection-abort-by-local`` (в эфире — «Connection Failed to be Established», 0x3e: устройство не ответило на
первые пакеты; замер 2026-11-14 — удалась 1 попытка из 5). Поэтому подключение держим постоянно, а неудачные
попытки повторяет вызывающий (power.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("fnav.core.ble")

SCAN_S = 12.0  # BlueZ не знает устройство (давно не видел) — столько искать его в эфире
CONNECT_TIMEOUT_S = 30.0
RESOLVE_S = 10.0  # характеристики появляются, когда BlueZ разобрал сервисы

BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
DEVICE_IF, CHAR_IF, ADAPTER_IF = "org.bluez.Device1", "org.bluez.GattCharacteristic1", "org.bluez.Adapter1"
PROPS_IF, OM_IF = "org.freedesktop.DBus.Properties", "org.freedesktop.DBus.ObjectManager"


class BleError(Exception):
    pass


def uuid16(short: str) -> str:
    """«fff1» → полный UUID Bluetooth SIG."""
    return f"0000{short.lower()}-0000-1000-8000-00805f9b34fb"


def _unvariant(v: Any) -> Any:
    if hasattr(v, "signature") and hasattr(v, "value"):
        v = v.value
    if isinstance(v, dict):
        return {k: _unvariant(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unvariant(x) for x in v]
    return v


class BleLink:
    """Подключение к одному устройству. connect(subscribe=[uuid…]) — найти (если надо), подключиться, подписаться;
    write(uuid, data) — запись; on_value(uuid, bytes) — пришло уведомление; connected — подключение живо (BlueZ
    сообщает о разрыве, тогда же вызывается on_lost); close() — отключиться и закрыть шину."""

    def __init__(self, mac: str, on_value: Callable[[str, bytes], None], adapter: str = ADAPTER) -> None:
        self.mac = mac.upper()
        self.adapter = adapter
        self.path = f"{adapter}/dev_{self.mac.replace(':', '_')}"
        self.on_value = on_value
        self.on_lost: Callable[[], None] | None = None
        self.bus: Any = None
        self.connected = False
        self.chars: dict[str, str] = {}  # uuid → путь характеристики
        self._by_path: dict[str, str] = {}

    async def _call(self, path: str, iface: str, member: str, signature: str = "", body: list | None = None,
                    timeout: float = 10.0, dest: str = BLUEZ) -> list:
        from dbus_fast import Message, MessageType  # type: ignore[import-not-found]

        if self.bus is None:
            raise BleError("not open")
        msg = Message(destination=dest, path=path, interface=iface, member=member, signature=signature,
                      body=body or [])
        try:
            reply = await asyncio.wait_for(self.bus.call(msg), timeout)
        except TimeoutError as e:
            raise BleError(f"{member}: no answer from BlueZ") from e
        if reply.message_type == MessageType.ERROR:
            text = reply.body[0] if reply.body else ""
            raise BleError(f"{member}: {(reply.error_name or '').rsplit('.', 1)[-1]} {text}".strip())
        return list(reply.body or [])

    async def _open(self) -> None:
        if self.bus is not None:
            return
        from dbus_fast import BusType  # type: ignore[import-not-found]
        from dbus_fast.aio import MessageBus  # type: ignore[import-not-found]

        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except (OSError, ValueError) as e:
            raise BleError(f"system bus: {e}") from e
        self.bus.add_message_handler(self._on_message)
        await self._call("/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                         [f"type='signal',sender='{BLUEZ}',path_namespace='{self.path}'"], dest="org.freedesktop.DBus")

    def _on_message(self, msg: Any) -> bool:
        try:
            if msg.member != "PropertiesChanged" or not msg.path or not msg.body:
                return False
            iface, changed = msg.body[0], _unvariant(msg.body[1])
            if iface == DEVICE_IF and msg.path == self.path and changed.get("Connected") is False:
                if self.connected:
                    self.connected = False
                    if self.on_lost is not None:
                        self.on_lost()
            elif iface == CHAR_IF and "Value" in changed and msg.path in self._by_path:
                self.on_value(self._by_path[msg.path], bytes(changed["Value"]))
        except Exception:
            log.exception("ble signal")
        return False

    async def _objects(self) -> dict[str, dict[str, dict[str, Any]]]:
        (objs,) = await self._call("/", OM_IF, "GetManagedObjects")
        return _unvariant(objs)

    async def _find(self, scan_s: float) -> None:
        """Устройства нет в BlueZ (давно не видел) — поискать в эфире (только LE, только его адрес)."""
        if self.path in await self._objects():
            return
        from dbus_fast import Variant  # type: ignore[import-not-found]

        with contextlib.suppress(BleError):
            await self._call(self.adapter, ADAPTER_IF, "SetDiscoveryFilter", "a{sv}",
                             [{"Transport": Variant("s", "le"), "Pattern": Variant("s", self.mac)}])
        await self._call(self.adapter, ADAPTER_IF, "StartDiscovery")
        try:
            end = time.monotonic() + scan_s
            while time.monotonic() < end:
                await asyncio.sleep(0.5)
                if self.path in await self._objects():
                    return
            raise BleError("not found nearby")
        finally:
            with contextlib.suppress(BleError):
                await self._call(self.adapter, ADAPTER_IF, "StopDiscovery")

    async def connect(self, subscribe: list[str], need: list[str], scan_s: float = SCAN_S) -> None:
        """subscribe — UUID характеристик для уведомлений; need — какие ещё должны быть (запись)."""
        await self._open()
        await self._find(scan_s)
        try:
            await self._call(self.path, DEVICE_IF, "Connect", timeout=CONNECT_TIMEOUT_S)
        except BleError as e:
            if "AlreadyConnected" not in str(e):
                raise
        self.connected = True
        try:
            end = time.monotonic() + RESOLVE_S
            while True:
                objs = await self._objects()
                self.chars = {str(p[CHAR_IF].get("UUID", "")).lower(): path for path, p in objs.items()
                              if path.startswith(self.path + "/") and CHAR_IF in p}
                if all(u in self.chars for u in [*subscribe, *need]):
                    break
                if not self.connected or time.monotonic() > end:
                    raise BleError("expected GATT characteristics not found")
                await asyncio.sleep(0.5)
            self._by_path = {self.chars[u]: u for u in subscribe}
            for u in subscribe:
                await self._call(self.chars[u], CHAR_IF, "StartNotify")
        except BaseException:
            await self.disconnect()
            raise

    async def write(self, uuid: str, data: bytes, response: bool = False) -> None:
        if not self.connected or uuid not in self.chars:
            raise BleError("not connected")
        from dbus_fast import Variant  # type: ignore[import-not-found]

        await self._call(self.chars[uuid], CHAR_IF, "WriteValue", "aya{sv}",
                         [data, {"type": Variant("s", "request" if response else "command")}])

    async def disconnect(self) -> None:
        self.connected = False
        if self.bus is not None:
            with contextlib.suppress(Exception):
                await self._call(self.path, DEVICE_IF, "Disconnect", timeout=10.0)

    async def close(self) -> None:
        await self.disconnect()
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.disconnect()
        self.bus = None
