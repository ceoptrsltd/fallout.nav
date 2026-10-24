"""Bluetooth: BlueZ по D-Bus (dbus-fast, asyncio) — адаптер, поиск, сопряжение, колонка по умолчанию.

docs/contracts/radio.md. Колонка по умолчанию (последняя подключённая аудиоустройство) хранится в
data_dir/bluetooth_state.json, переподключается каждые 30 с; о её подключении/отключении узнаёт радио
(set_output) и переключает mpv на alsa/bluealsa:DEV=<mac>. Нет dbus-fast или адаптера → available: false.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fnav.models import BluetoothSettings, BtDevice, BtState, RadioOutput

log = logging.getLogger("fnav.core.bluetooth")

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
AUDIO_UUIDS = ("0000110b", "0000110d", "0000111e")  # A2DP sink, A2DP, Handsfree
AUTO_CONNECT_S = 30.0
PAIR_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 40.0
BLUEZ = "org.bluez"
ADAPTER_IF, DEVICE_IF = "org.bluez.Adapter1", "org.bluez.Device1"
PROPS_IF, OM_IF = "org.freedesktop.DBus.Properties", "org.freedesktop.DBus.ObjectManager"
AGENT_PATH = "/fnav/agent"
SYS_BT = Path("/sys/class/bluetooth")
OPEN_TIMEOUT_S = 5.0  # первое подключение к BlueZ не задерживает старт fnav-core дольше этого
# Встроенный чип (CYW43455) иногда «падает»: в ядре «hci0: hardware error», адаптер выключается и больше
# не включается (HCI-команды без ответа). Две неудачные попытки включить подряд — перезапуск чипа
# (pi/bin/fnav-bt-reset), не чаще раза в RESET_EVERY_S.
RESET_AFTER_FAILURES = 2
RESET_EVERY_S = 300.0

OutputListener = Callable[[RadioOutput | None], None]


class BluetoothError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def norm_mac(mac: str) -> str:
    m = (mac or "").strip().upper().replace("-", ":")
    if not MAC_RE.match(m):
        raise BluetoothError(400, "Invalid Bluetooth address")
    return m


def is_audio(uuids: list[str] | None, icon: str | None) -> bool:
    if (icon or "").startswith("audio-"):
        return True
    return any(str(u).lower().startswith(AUDIO_UUIDS) for u in uuids or [])


def sort_devices(devs: list[BtDevice]) -> list[BtDevice]:
    return sorted(devs, key=lambda d: (not d.connected, not d.paired, not d.audio,
                                       -(d.rssi if d.rssi is not None else -999), (d.name or d.address).lower()))


class BluetoothBase:
    """Общая логика: колонка по умолчанию, автоподключение, оповещение радио. Транспорт — в наследниках."""

    def __init__(self, state_path: Path, get_settings: Callable[[], BluetoothSettings]) -> None:
        self.state_path = state_path
        self.get_settings = get_settings
        self.available = False
        self.powered = False
        self.discovering = False
        self.error: str | None = None
        self.devices: dict[str, BtDevice] = {}
        self.audio_device: str | None = None
        self.audio_name: str = ""
        self._listeners: list[OutputListener] = []
        self._last_output: tuple | None = None
        self._tasks: list[asyncio.Task] = []
        self._scan_stop: asyncio.Task | None = None
        self._busy: set[str] = set()  # идёт pair/connect — автоподключение не мешает
        self._auto_failed: str | None = None
        self.reset_controller: Callable[[], Awaitable[None]] | None = None  # перезапуск чипа (create_bluetooth)
        self._user_off = False  # выключили из веб-интерфейса — сами не включаем
        self._power_failures = 0
        self._reset_at: float | None = None

    # ── колонка по умолчанию ──
    def _load_state(self) -> None:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.audio_device = norm_mac(d["audio_device"]) if d.get("audio_device") else None
            self.audio_name = str(d.get("name") or "")
        except (OSError, ValueError, KeyError, BluetoothError):
            pass

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"audio_device": self.audio_device, "name": self.audio_name}), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("bluetooth: cannot save %s: %s", self.state_path, e)

    def on_output(self, cb: OutputListener) -> None:
        self._listeners.append(cb)

    def output(self) -> RadioOutput | None:
        if not self.audio_device:
            return None
        d = self.devices.get(self.audio_device)
        name = (d.name if d and d.name else self.audio_name) or self.audio_device
        return RadioOutput(address=self.audio_device, name=name, connected=bool(d and d.connected))

    def _emit_output(self) -> None:
        out = self.output()
        key = (out.address, out.name, out.connected) if out else None
        if key == self._last_output:
            return
        self._last_output = key
        if out and out.connected:
            log.info("bluetooth: audio output %s (%s) connected", out.name, out.address)
        for cb in self._listeners:
            try:
                cb(out)
            except Exception:
                log.exception("bluetooth output listener")

    def state(self) -> BtState:
        return BtState(available=self.available, powered=self.powered, discovering=self.discovering,
                       audio_device=self.audio_device, devices=sort_devices(list(self.devices.values())),
                       error=self.error)

    # ── жизненный цикл ──
    async def start(self, background: bool = True) -> None:
        self._load_state()
        try:
            await asyncio.wait_for(self._open(), OPEN_TIMEOUT_S)
        except TimeoutError:
            self.available, self.error = False, "BlueZ does not answer"  # повторим через 30 с
        if self.available and self.get_settings().enabled and not self.powered:
            await self._safe_power(True)
        self._emit_output()
        if background:
            self._tasks.append(asyncio.create_task(self._maintain(), name="bt-maintain"))

    async def stop(self) -> None:
        for t in [*self._tasks, self._scan_stop]:
            if t:
                t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        await self._close()

    def apply_settings(self, s: BluetoothSettings) -> None:
        if self.available and s.enabled and not self.powered:
            asyncio.get_running_loop().create_task(self._safe_power(True), name="bt-power")

    async def _safe_power(self, on: bool) -> None:
        try:
            await self._power(on)
            self.powered = on
            self._power_failures = 0
        except BluetoothError as e:
            self.error = e.detail
            if on:
                self._power_failures += 1
            log.warning("bluetooth: cannot switch the adapter %s: %s", "on" if on else "off", e.detail)

    async def _recover_power(self) -> None:
        """Адаптер выключился сам (не из веб-интерфейса): включить; не включается — перезапустить чип."""
        await self._safe_power(True)
        if self.powered or self._power_failures < RESET_AFTER_FAILURES or self.reset_controller is None:
            return
        now = time.monotonic()
        if self._reset_at is not None and now - self._reset_at < RESET_EVERY_S:
            return
        self._reset_at = now
        log.warning("bluetooth: the adapter does not power on (%s), restarting the Bluetooth chip", self.error)
        try:
            await self.reset_controller()
            self._power_failures = 0
        except Exception as e:  # noqa: BLE001 — восстановление не должно ронять обслуживание
            log.warning("bluetooth: chip restart failed: %s", e)

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(AUTO_CONNECT_S)
            try:
                await self._tick()
            except Exception:
                log.exception("bluetooth maintenance")

    async def _tick(self) -> None:
        if not self.available:
            await self._open()  # адаптер/bluetoothd появились позже (или после перезапуска)
            if self.available and self.get_settings().enabled and not self.powered:
                await self._safe_power(True)
            self._emit_output()
            return
        s = self.get_settings()
        if s.enabled and not self.powered and not self._user_off:
            await self._recover_power()
            self._emit_output()
            return
        mac = self.audio_device
        if not (s.auto_connect and self.powered and mac) or mac in self._busy:
            return
        d = self.devices.get(mac)
        if d is None or d.connected or self.discovering:
            return
        try:
            self._busy.add(mac)
            await self._connect(mac)
            self._auto_failed = None
            log.info("bluetooth: auto-connected %s", d.name or mac)
        except BluetoothError as e:
            if self._auto_failed != e.detail:
                log.info("bluetooth: auto-connect %s: %s (will keep trying)", d.name or mac, e.detail)
            self._auto_failed = e.detail
        finally:
            self._busy.discard(mac)
            self._emit_output()

    # ── команды API ──
    def _need(self) -> None:
        if not self.available:
            raise BluetoothError(503, self.error or "Bluetooth is not available")

    async def set_power(self, on: bool) -> BtState:
        self._need()
        await self._power(on)
        self.powered = on
        self._user_off = not on
        if not on:
            self.discovering = False
        self._emit_output()
        return self.state()

    async def scan(self, seconds: int) -> BtState:
        self._need()
        if not self.powered:
            raise BluetoothError(409, "Bluetooth is off")
        seconds = max(1, min(60, int(seconds)))
        await self._start_discovery()
        self.discovering = True
        if self._scan_stop:
            self._scan_stop.cancel()

        async def later() -> None:
            await asyncio.sleep(seconds)
            await self._stop_discovery_quiet()

        self._scan_stop = asyncio.get_running_loop().create_task(later(), name="bt-scan-stop")
        return self.state()

    async def _stop_discovery_quiet(self) -> None:
        with contextlib.suppress(BluetoothError):
            await self._stop_discovery()
        self.discovering = False

    def _device(self, mac: str) -> BtDevice:
        d = self.devices.get(mac)
        if d is None:
            raise BluetoothError(404, "Device not found (scan first)")
        return d

    async def connect(self, mac: str) -> BtState:
        self._need()
        mac = norm_mac(mac)
        d = self._device(mac)
        if not self.powered:
            raise BluetoothError(409, "Bluetooth is off")
        self._busy.add(mac)
        try:
            if self.discovering:  # поиск мешает сопряжению и подключению
                if self._scan_stop:
                    self._scan_stop.cancel()
                await self._stop_discovery_quiet()
            if not d.paired:
                await self._pair(mac)
            if not d.trusted:
                await self._trust(mac)
            await self._connect(mac)
        finally:
            self._busy.discard(mac)
        d = self.devices.get(mac, d)
        if d.audio:
            self.audio_device, self.audio_name = mac, d.name
            await asyncio.to_thread(self._save_state)
        self._emit_output()
        return self.state()

    async def disconnect(self, mac: str) -> BtState:
        self._need()
        mac = norm_mac(mac)
        self._device(mac)
        await self._disconnect(mac)
        self._emit_output()
        return self.state()

    async def remove(self, mac: str) -> BtState:
        self._need()
        mac = norm_mac(mac)
        self._device(mac)
        await self._remove(mac)
        self.devices.pop(mac, None)
        if self.audio_device == mac:
            self.audio_device, self.audio_name = None, ""
            await asyncio.to_thread(self._save_state)
        self._emit_output()
        return self.state()

    # ── транспорт (наследники) ──
    async def _open(self) -> None: ...
    async def _close(self) -> None: ...
    async def _power(self, on: bool) -> None: raise BluetoothError(503, "Bluetooth is not available")
    async def _start_discovery(self) -> None: raise BluetoothError(503, "Bluetooth is not available")
    async def _stop_discovery(self) -> None: ...
    async def _pair(self, mac: str) -> None: raise BluetoothError(503, "Bluetooth is not available")
    async def _trust(self, mac: str) -> None: ...
    async def _connect(self, mac: str) -> None: raise BluetoothError(503, "Bluetooth is not available")
    async def _disconnect(self, mac: str) -> None: ...
    async def _remove(self, mac: str) -> None: ...


# ───────────────────────── BlueZ ─────────────────────────


def _unvariant(v: Any) -> Any:
    if hasattr(v, "signature") and hasattr(v, "value"):
        v = v.value
    if isinstance(v, dict):
        return {k: _unvariant(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unvariant(x) for x in v]
    return v


def _variant(sig: str, value: Any) -> Any:
    try:
        from dbus_fast import Variant  # type: ignore[import-not-found]
    except ImportError:  # тесты без dbus-fast
        return (sig, value)
    return Variant(sig, value)


def _dev_path(adapter: str, mac: str) -> str:
    return f"{adapter}/dev_{mac.replace(':', '_')}"


class BlueZBluetooth(BluetoothBase):
    """Системная шина: вызовы методов + сигналы ObjectManager/PropertiesChanged в кэш объектов."""

    def __init__(self, state_path: Path, get_settings: Callable[[], BluetoothSettings]) -> None:
        super().__init__(state_path, get_settings)
        self.bus: Any = None
        self.agent: Any = None
        self.adapter: str | None = None
        self.objects: dict[str, dict[str, dict[str, Any]]] = {}

    # ── шина ──
    async def _connect_bus(self) -> None:
        from dbus_fast import BusType  # type: ignore[import-not-found]
        from dbus_fast.aio import MessageBus  # type: ignore[import-not-found]

        from fnav.core.bt_agent import PairingAgent

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.bus.add_message_handler(self._on_message)
        for rule in (f"type='signal',sender='{BLUEZ}'",
                     f"type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{BLUEZ}'"):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                             "s", [rule])
        self.agent = PairingAgent()
        self.bus.export(AGENT_PATH, self.agent)

    async def _call(self, dest: str, path: str, iface: str, member: str, signature: str = "",
                    body: list | None = None, timeout: float = 10.0) -> list:
        from dbus_fast import Message, MessageType  # type: ignore[import-not-found]

        if self.bus is None:
            raise BluetoothError(503, "Bluetooth is not available")
        msg = Message(destination=dest, path=path, interface=iface, member=member, signature=signature,
                      body=body or [])
        try:
            reply = await asyncio.wait_for(self.bus.call(msg), timeout)
        except TimeoutError as e:
            raise BluetoothError(504, f"{member}: no answer from BlueZ") from e
        if reply.message_type == MessageType.ERROR:
            text = reply.body[0] if reply.body else reply.error_name
            raise BluetoothError(502, f"{(reply.error_name or '').rsplit('.', 1)[-1]}: {text}".strip(": "))
        return list(reply.body or [])

    async def _open(self) -> None:
        # без адаптера к BlueZ не обращаемся: вызов org.bluez запускает D-Bus-активацию bluetoothd, которая при
        # невыполненном условии юнита висит до таймаута (встроенный BT выключен, пока GPS занимает UART0)
        if not any(SYS_BT.glob("hci*")):
            self.available, self.error, self.devices = False, "No Bluetooth adapter", {}
            self.powered = self.discovering = False
            return
        try:
            if self.bus is None:
                await self._connect_bus()
            await self._call(BLUEZ, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                             [AGENT_PATH, "NoInputNoOutput"])
            with contextlib.suppress(BluetoothError):
                await self._call(BLUEZ, "/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent", "o",
                                 [AGENT_PATH])
        except ImportError:
            self.available, self.error = False, "dbus-fast is not installed"
            return
        except BluetoothError as e:
            if "AlreadyExists" not in e.detail:  # агент уже зарегистрирован этим соединением — не ошибка
                down = any(x in e.detail for x in ("ServiceUnknown", "NoSuchUnit", "NameHasNoOwner"))
                self.available = False
                self.error = "Bluetooth service (bluetoothd) is not running" if down else f"BlueZ: {e.detail}"
                return
        except (OSError, ValueError) as e:
            self.available, self.error = False, f"System bus: {e}"
            return
        await self._refresh()

    async def _close(self) -> None:
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.disconnect()
        self.bus = None

    async def _refresh(self) -> None:
        try:
            (objs,) = await self._call(BLUEZ, "/", OM_IF, "GetManagedObjects")
        except BluetoothError as e:
            self.available, self.error = False, f"BlueZ: {e.detail}"
            return
        self.objects = _unvariant(objs)
        self._rebuild()

    def _rebuild(self) -> None:
        adapters = sorted(p for p, ifs in self.objects.items() if ADAPTER_IF in ifs)
        self.adapter = adapters[0] if adapters else None
        if self.adapter is None:
            self.available, self.error, self.powered, self.discovering = False, "No Bluetooth adapter", False, False
            self.devices = {}
            return
        a = self.objects[self.adapter][ADAPTER_IF]
        self.available, self.error = True, None
        self.powered = bool(a.get("Powered"))
        self.discovering = bool(a.get("Discovering"))
        devs: dict[str, BtDevice] = {}
        for ifs in self.objects.values():
            d = ifs.get(DEVICE_IF)
            if not d or d.get("Adapter") != self.adapter or not d.get("Address"):
                continue
            mac = str(d["Address"]).upper()
            devs[mac] = BtDevice(address=mac, name=str(d.get("Alias") or d.get("Name") or ""),
                                 icon=str(d.get("Icon") or ""), paired=bool(d.get("Paired")),
                                 trusted=bool(d.get("Trusted")), connected=bool(d.get("Connected")),
                                 rssi=d.get("RSSI") if isinstance(d.get("RSSI"), int) else None,
                                 audio=is_audio(d.get("UUIDs"), d.get("Icon")))
        self.devices = devs

    def _on_message(self, msg: Any) -> bool:
        try:
            self.on_signal(msg.path, msg.interface, msg.member, list(msg.body or []))
        except Exception:
            log.exception("bluetooth signal")
        return False  # не «съедаем» сообщение

    def on_signal(self, path: str, iface: str, member: str, body: list) -> None:
        if iface == OM_IF and member == "InterfacesAdded":
            p, ifs = body[0], _unvariant(body[1])
            self.objects.setdefault(p, {}).update(ifs)
        elif iface == OM_IF and member == "InterfacesRemoved":
            p, names = body[0], body[1]
            for n in names:
                self.objects.get(p, {}).pop(n, None)
            if p in self.objects and not self.objects[p]:
                del self.objects[p]
        elif iface == PROPS_IF and member == "PropertiesChanged":
            name, changed, invalid = body[0], _unvariant(body[1]), body[2] if len(body) > 2 else []
            props = self.objects.setdefault(path, {}).setdefault(name, {})
            props.update(changed)
            for k in invalid:
                props.pop(k, None)
        elif member == "NameOwnerChanged" and body and body[0] == BLUEZ:
            if not body[2]:  # bluetoothd остановился
                self.objects, self.available, self.error = {}, False, "bluetoothd is not running"
                self.devices = {}
            else:
                asyncio.get_running_loop().create_task(self._open(), name="bt-reopen")
            self._emit_output()
            return
        else:
            return
        self._rebuild()
        self._emit_output()

    # ── операции ──
    def _path(self, mac: str) -> str:
        if not self.adapter:
            raise BluetoothError(503, "No Bluetooth adapter")
        return _dev_path(self.adapter, mac)

    async def _power(self, on: bool) -> None:
        if not self.adapter:
            raise BluetoothError(503, "No Bluetooth adapter")
        await self._call(BLUEZ, self.adapter, PROPS_IF, "Set", "ssv", [ADAPTER_IF, "Powered", _variant("b", on)])

    async def _start_discovery(self) -> None:
        if not self.adapter:
            raise BluetoothError(503, "No Bluetooth adapter")
        # колонки — «классический» Bluetooth; без фильтра список забивают BLE-браслеты и метки
        with contextlib.suppress(BluetoothError):
            await self._call(BLUEZ, self.adapter, ADAPTER_IF, "SetDiscoveryFilter", "a{sv}",
                             [{"Transport": _variant("s", "bredr")}])
        try:
            await self._call(BLUEZ, self.adapter, ADAPTER_IF, "StartDiscovery")
        except BluetoothError as e:
            if "InProgress" not in e.detail:
                raise BluetoothError(502, f"Scan failed: {e.detail}") from e

    async def _stop_discovery(self) -> None:
        if self.adapter:
            await self._call(BLUEZ, self.adapter, ADAPTER_IF, "StopDiscovery")

    async def _pair(self, mac: str) -> None:
        path = self._path(mac)
        if self.agent is not None:
            self.agent.allowed.add(path)
        try:
            await self._call(BLUEZ, path, DEVICE_IF, "Pair", timeout=PAIR_TIMEOUT_S)
        except BluetoothError as e:
            if "AlreadyExists" not in e.detail:
                raise BluetoothError(502, f"Pairing failed: {e.detail}") from e
        finally:
            if self.agent is not None:
                self.agent.allowed.discard(path)

    async def _trust(self, mac: str) -> None:
        await self._call(BLUEZ, self._path(mac), PROPS_IF, "Set", "ssv", [DEVICE_IF, "Trusted", _variant("b", True)])

    async def _connect(self, mac: str) -> None:
        try:
            await self._call(BLUEZ, self._path(mac), DEVICE_IF, "Connect", timeout=CONNECT_TIMEOUT_S)
        except BluetoothError as e:
            hint = " (is bluealsa running?)" if "profile-unavailable" in e.detail else ""
            raise BluetoothError(502, f"Connection failed: {e.detail}{hint}") from e

    async def _disconnect(self, mac: str) -> None:
        await self._call(BLUEZ, self._path(mac), DEVICE_IF, "Disconnect", timeout=20.0)

    async def _remove(self, mac: str) -> None:
        if not self.adapter:
            raise BluetoothError(503, "No Bluetooth adapter")
        await self._call(BLUEZ, self.adapter, ADAPTER_IF, "RemoveDevice", "o", [self._path(mac)])


# ───────────────────────── --mock ─────────────────────────


class FakeBluetooth(BluetoothBase):
    """Вымышленный адаптер для разработки админки и экрана: колонка уже подключена, поиск находит ещё."""

    def __init__(self, state_path: Path, get_settings: Callable[[], BluetoothSettings],
                 delay_s: float = 1.0) -> None:
        super().__init__(state_path, get_settings)
        self.delay_s = delay_s
        self._hidden = [
            BtDevice(address="11:22:33:44:55:02", name="Pip-Boy Boombox", icon="audio-card", audio=True, rssi=-58),
            BtDevice(address="11:22:33:44:55:04", name="Mi Smart Band 8", icon="", rssi=-80),
        ]

    async def _open(self) -> None:
        self.available, self.powered = True, False
        self.devices = {d.address: d for d in (
            BtDevice(address="11:22:33:44:55:01", name="JBL Go 3", icon="audio-card", paired=True, trusted=True,
                     connected=True, audio=True, rssi=-52),
            BtDevice(address="11:22:33:44:55:03", name="Galaxy S24", icon="phone", paired=True, trusted=True),
        )}
        if not self.audio_device:
            self.audio_device, self.audio_name = "11:22:33:44:55:01", "JBL Go 3"

    async def _power(self, on: bool) -> None:
        await asyncio.sleep(self.delay_s / 4)
        if not on:
            for d in self.devices.values():
                d.connected = False

    async def _start_discovery(self) -> None:
        async def appear() -> None:
            for d in self._hidden:
                await asyncio.sleep(self.delay_s * 2)
                if self.discovering and d.address not in self.devices:
                    self.devices[d.address] = d.model_copy(update={"rssi": d.rssi + random.randint(-6, 6)})

        asyncio.get_running_loop().create_task(appear(), name="fake-bt-scan")

    async def _pair(self, mac: str) -> None:
        await asyncio.sleep(self.delay_s * 1.5)
        self.devices[mac].paired = True

    async def _trust(self, mac: str) -> None:
        self.devices[mac].trusted = True

    async def _connect(self, mac: str) -> None:
        await asyncio.sleep(self.delay_s)
        if not self.powered:
            raise BluetoothError(502, "Connection failed: adapter is off")
        self.devices[mac].connected = True

    async def _disconnect(self, mac: str) -> None:
        await asyncio.sleep(self.delay_s / 2)
        self.devices[mac].connected = False

    async def _remove(self, mac: str) -> None:
        self.devices.pop(mac, None)


def create_bluetooth(mock: bool, data_dir: Path, get_settings: Callable[[], BluetoothSettings]) -> BluetoothBase:
    path = data_dir / "bluetooth_state.json"
    if mock:
        return FakeBluetooth(path, get_settings)
    bt = BlueZBluetooth(path, get_settings)
    bt.reset_controller = reset_bt_chip
    return bt


async def reset_bt_chip() -> None:
    """Перезапустить встроенный Bluetooth-чип (root: pi/systemd/fnav-bt-reset.service)."""
    from fnav.core.system import run

    rc, _out, err = await run("sudo", "-n", "/usr/bin/systemctl", "start", "fnav-bt-reset", timeout=30)
    if rc != 0:
        raise BluetoothError(502, (err or "").strip() or f"systemctl exit code {rc}")
