"""Электростанции по Bluetooth LE — только чтение (docs/contracts/power.md).

Главная — Fossibot (fnav/core/fossibot.py): заряд, % — на экране крупно. Вторая — Daly BMS (fnav/core/daly.py):
напряжение — верхняя строка. Третья — пока заглушка (нижняя строка «--.-V»).

Каждая станция — свой StationReader: держит постоянное подключение (fnav/core/ble.py) и раз в POLL_S читает
показания. Подключение LE у встроенного чипа Pi удаётся не с первого раза (ble.py), поэтому неудачи сначала
повторяются часто (CONNECT_RETRY_S), а если станции так и нет (не нашлась в эфире или долго не подключается) —
всё реже (AWAY_BACKOFF_S): попытки занимают радио, общее с Wi-Fi. Пока на экране задний вид (quiet) — не
подключаемся вовсе (как колонка, fnav/core/bluetooth.py); уже подключённые станции читаются.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fnav.core import daly, fossibot, leadbatt
from fnav.core.ble import BleError, BleLink
from fnav.core.util import cancel_tasks
from fnav.models import PowerSettings, PowerSlot, PowerStationStatus, PowerStatus, PowerTelemetry

log = logging.getLogger("fnav.core.power")

POLL_S = 10.0
READ_TIMEOUT_S = 3.0  # ответ приходит за 0.1–0.3 с
READ_TRIES = 3
READ_FAILS_DROP = 3  # столько опросов подряд без ответа — переподключиться
STALE_S = 60.0  # показания старше — на экране прочерк
LOW_SOC_PCT = 20.0
CONNECT_RETRY_S = 2.0  # «le-connection-abort-by-local» — обычное дело: повторить сразу
FAST_TRIES = 30  # столько быстрых попыток подряд (~1–1.5 мин), дальше — реже
AWAY_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
QUIET_POLL_S = 2.0


@dataclass(frozen=True)
class Protocol:
    kind: Literal["fossibot", "daly", "lead"]
    subscribe: tuple[str, ...]
    reply_uuid: str
    write_uuid: str
    write_response: bool
    request: bytes
    frames: Callable[[], Any]
    parse: Callable[[bytes], Any]


FOSSIBOT = Protocol("fossibot", (fossibot.NOTIFY_UUID, fossibot.INDICATE_UUID), fossibot.NOTIFY_UUID,
                    fossibot.WRITE_UUID, True, fossibot.REQUEST, fossibot.Frames, fossibot.parse)
DALY = Protocol("daly", (daly.NOTIFY_UUID,), daly.NOTIFY_UUID, daly.WRITE_UUID, False, daly.REQUEST, daly.Frames,
                daly.parse)
LEAD = Protocol("lead", (leadbatt.STATUS_UUID,), leadbatt.STATUS_UUID, leadbatt.CMD_UUID, False, leadbatt.REQUEST,
                leadbatt.Frames, leadbatt.parse)

LinkFactory = Callable[[str, Callable[[str, bytes], None]], Any]


class StationReader:
    def __init__(self, slot: Literal["main", "second", "third"], proto: Protocol, quiet: Callable[[], bool] = lambda: False,
                 link_factory: LinkFactory = BleLink, mono: Callable[[], float] = time.monotonic) -> None:
        self.slot = slot
        self.proto = proto
        self.quiet = quiet
        self.link_factory = link_factory
        self.mono = mono
        self.mac = ""
        self.reading: Any = None
        self.read_at: float | None = None
        self.error: str | None = None
        self.link: Any = None
        self._frames = proto.frames()
        self._reply: asyncio.Future[bytes] | None = None
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return bool(self.link is not None and self.link.connected)

    def fresh(self) -> Any:
        """Показания не старше STALE_S или None."""
        if self.reading is None or self.read_at is None or self.mono() - self.read_at > STALE_S:
            return None
        return self.reading

    async def set_mac(self, mac: str) -> None:
        if mac == self.mac and (self._task is not None or not mac):
            return
        await self.stop()
        self.mac, self.reading, self.read_at, self.error = mac, None, None, None
        if mac:
            self._task = asyncio.get_running_loop().create_task(self._run(), name=f"power-{self.slot}")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        self._task = None
        if self.link is not None:
            with contextlib.suppress(Exception):
                await self.link.close()
        self.link = None

    # ── опрос ──
    def _on_value(self, uuid: str, data: bytes) -> None:
        if uuid != self.proto.reply_uuid:
            return
        frame = self._frames.feed(data)
        if frame is not None and self._reply is not None and not self._reply.done():
            self._reply.set_result(frame)

    def _on_lost(self) -> None:
        if self._reply is not None and not self._reply.done():
            self._reply.set_exception(BleError("disconnected"))

    async def read_once(self) -> Any:
        loop = asyncio.get_running_loop()
        for _ in range(READ_TRIES):
            self._frames.clear()
            self._reply = loop.create_future()
            try:
                await self.link.write(self.proto.write_uuid, self.proto.request, self.proto.write_response)
                frame = await asyncio.wait_for(self._reply, READ_TIMEOUT_S)
            except TimeoutError:
                continue
            finally:
                self._reply = None
            reading = self.proto.parse(frame)
            if reading is not None:
                return reading
        raise BleError("no answer")

    async def _connect(self) -> None:
        if self.link is None:
            self.link = self.link_factory(self.mac, self._on_value)
            self.link.on_lost = self._on_lost
        await self.link.connect(list(self.proto.subscribe), [self.proto.write_uuid])

    async def _run(self) -> None:
        fails = 0
        while True:
            if self.quiet():
                await asyncio.sleep(QUIET_POLL_S)
                continue
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — BleError, нет dbus-fast, шина
                fails += 1
                self.error = str(e) or type(e).__name__
                away = "not found" in self.error or fails >= FAST_TRIES
                if away:
                    delay = AWAY_BACKOFF_S[min(max(fails - FAST_TRIES, 0), len(AWAY_BACKOFF_S) - 1)]
                    log.info("power: %s %s not connected (%s), next try in %.0f s", self.slot, self.mac,
                             self.error, delay)
                else:
                    delay = CONNECT_RETRY_S
                await asyncio.sleep(delay)
                continue
            log.info("power: %s %s (%s) connected after %d failed tries", self.slot, self.mac, self.proto.kind,
                     fails)
            fails = 0
            self.error = None
            read_fails = 0
            while self.connected:
                try:
                    self.reading = await self.read_once()
                    self.read_at = self.mono()
                    read_fails = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    read_fails += 1
                    self.error = f"read: {e}"
                    if read_fails >= READ_FAILS_DROP:
                        log.info("power: %s %s - no answer %d times, reconnecting", self.slot, self.mac, read_fails)
                        with contextlib.suppress(Exception):
                            await self.link.disconnect()
                        break
                await asyncio.sleep(POLL_S)
            if not self.connected:
                log.info("power: %s %s disconnected", self.slot, self.mac)

    # ── наружу ──
    def slot_view(self) -> PowerSlot:
        r = self.fresh()
        if r is None:
            return PowerSlot()
        soc = float(r.soc_pct)
        return PowerSlot(soc_pct=soc, voltage_v=getattr(r, "voltage_v", None), charging=getattr(r, "charging", None),
                         low=soc <= LOW_SOC_PCT, input_w=getattr(r, "input_w", None),
                         output_w=getattr(r, "output_w", None), to_empty_min=getattr(r, "to_empty_min", None))

    def status(self) -> PowerStationStatus:
        age = None if self.read_at is None else round(self.mono() - self.read_at, 1)
        reading = dataclasses.asdict(self.reading) if self.reading is not None else None
        return PowerStationStatus(slot=self.slot, kind=self.proto.kind, mac=self.mac, connected=self.connected,
                                  age_s=age, error=self.error, reading=reading)


class FakeLink:
    """--mock: «станция» отвечает настоящими кадрами протокола с медленно меняющимися значениями."""

    def __init__(self, proto: Protocol, mac: str, on_value: Callable[[str, bytes], None]) -> None:
        self.proto = proto
        self.mac = mac
        self.on_value = on_value
        self.on_lost: Callable[[], None] | None = None
        self.connected = False
        self.t0 = time.monotonic()

    async def connect(self, subscribe: list[str], need: list[str]) -> None:
        await asyncio.sleep(0.2)
        self.connected = True

    async def write(self, uuid: str, data: bytes, response: bool = False) -> None:
        t = time.monotonic() - self.t0
        if self.proto.kind == "fossibot":
            frame = fake_fossibot(80 + 15 * math.sin(t / 300))
        elif self.proto.kind == "daly":
            frame = fake_daly(20.6 + 0.4 * math.sin(t / 200))
        else:
            frame = leadbatt.frame(12.5 + 0.2 * math.sin(t / 250), 0.0, 0, int(t))
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, self.on_value, self.proto.reply_uuid, frame)

    async def disconnect(self) -> None:
        self.connected = False

    async def close(self) -> None:
        self.connected = False


def fake_fossibot(soc: float, input_w: int = 0) -> bytes:
    r = [0] * fossibot.REGS
    r[56], r[6], r[39], r[54], r[18], r[19], r[59] = round(soc * 10), input_w, 150, 778, 2300, 500, 960
    body = fossibot.HEADER + b"".join(v.to_bytes(2, "big") for v in r)
    c = daly.crc16(body)
    return body + bytes([c >> 8, c & 0xFF])


def fake_daly(voltage: float, soc: float = 55.0) -> bytes:
    r = [0] * daly.REGS
    for i in range(6):
        r[i] = round(voltage / 6 * 1000)
    r[32:34] = [66, 65]
    r[40], r[41], r[42], r[49], r[50] = round(voltage * 10), 30000, round(soc * 10), 6, 2
    body = bytes([daly.ADDR, 0x03, 2 * daly.REGS]) + b"".join(v.to_bytes(2, "big") for v in r)
    c = daly.crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


class PowerService:
    def __init__(self, get_settings: Callable[[], PowerSettings], mock: bool = False,
                 quiet: Callable[[], bool] = lambda: False, link_factory: LinkFactory | None = None) -> None:
        self.get_settings = get_settings
        self.mock = mock

        def factory(proto: Protocol) -> LinkFactory:
            if link_factory is not None:
                return link_factory
            if mock:
                return lambda mac, cb: FakeLink(proto, mac, cb)
            return BleLink

        self.main = StationReader("main", FOSSIBOT, quiet, factory(FOSSIBOT))
        self.second = StationReader("second", DALY, quiet, factory(DALY))
        self.third = StationReader("third", LEAD, quiet, factory(LEAD))
        self.running = False
        self._lock = asyncio.Lock()  # настройки меняются подряд — переподключения по очереди

    async def start(self, background: bool = True) -> None:
        self.running = background
        await self.apply_settings(self.get_settings())

    async def apply_settings(self, s: PowerSettings) -> None:
        if not self.running:
            return
        main_mac, second_mac, third_mac = s.main_mac, s.second_mac, s.third_mac
        if self.mock:  # в демо станции «есть» и без настроек
            main_mac, second_mac = main_mac or "70:AF:09:00:00:01", second_mac or "74:C1:4E:00:00:02"
            third_mac = third_mac or "58:8C:81:00:00:03"
        async with self._lock:
            await self.main.set_mac(main_mac)
            await self.second.set_mac(second_mac)
            await self.third.set_mac(third_mac)

    async def stop(self) -> None:
        self.running = False
        await self.main.stop()
        await self.second.stop()
        await self.third.stop()

    def telemetry(self) -> PowerTelemetry:
        """Всегда (веб-интерфейс); плашку на экране прячет сам экран по power.show_on_screen."""
        return PowerTelemetry(main=self.main.slot_view(), second=self.second.slot_view(), third=self.third.slot_view())

    def status(self) -> PowerStatus:
        return PowerStatus(stations=[self.main.status(), self.second.status(), self.third.status()])
