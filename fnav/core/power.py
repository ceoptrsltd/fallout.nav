"""Электростанции по Bluetooth LE — только чтение (docs/contracts/power.md).

Главная — Fossibot (fnav/core/fossibot.py): заряд, % — на экране крупно. Вторая — Daly BMS (fnav/core/daly.py):
напряжение — верхняя строка. Третья — пока заглушка (нижняя строка «--.-V»).

Каждая станция — свой StationReader: держит постоянное подключение (fnav/core/ble.py) и раз в POLL_S (30 с) читает
показания. Подключаются станции по очереди (общий connect_lock). Подключение LE у встроенного чипа Pi удаётся не с первого
раза (ble.py), поэтому неудачи сначала
повторяются часто (CONNECT_RETRY_S), а если станции так и нет (не нашлась в эфире или долго не подключается) —
всё реже (AWAY_BACKOFF_S): попытки занимают радио, общее с Wi-Fi. Пока на экране задний вид (quiet) — не
подключаемся вовсе (как колонка, fnav/core/bluetooth.py); уже подключённые станции читаются.

Команды (control): только по слайдеру в веб-интерфейсе (POST /api/power/{slot}/control) и мигание светом Fossibot при
срабатывании охраны (flash_light, power.light_on_alarm). Каждый протокол сам собирает единственно допустимые кадры
(fossibot.output_request, daly.mosfet_request, leadbatt.mode_request) — другое записать нельзя. Сами по себе (при
запуске, переподключении, по таймеру) ничего не пишем. После команды — внеочередное чтение (видно новое состояние).
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
from fnav.models import (
    PowerControl,
    PowerSettings,
    PowerSlot,
    PowerStationStatus,
    PowerStatus,
    PowerTelemetry,
)

log = logging.getLogger("fnav.core.power")

POLL_S = 30.0  # чаще не нужно: радио общее с Wi-Fi и колонкой (A2DP)
READ_TIMEOUT_S = 3.0  # ответ приходит за 0.1–0.3 с
READ_TRIES = 3
READ_FAILS_DROP = 3  # столько опросов подряд без ответа — переподключиться
STALE_S = 100.0  # показания старше (три опроса подряд без ответа) — на экране прочерк
LOW_SOC_PCT = 20.0
CELL_DELTA_BAD_MV = 100  # Daly: разбаланс ячеек больше — опасно (на экране и в веб-интерфейсе мигает)
# Станция в эфире, а подключение сорвалось («le-connection-abort-by-local» — частая случайность, бывает и 30 раз
# подряд) — повторяем сериями: BURST_TRIES попыток через CONNECT_RETRY_S, пауза BURST_PAUSE_S, снова — пока не
# получится. Станции нет в эфире («not found nearby»: поиск занимает радио) — всё реже, AWAY_BACKOFF_S.
CONNECT_RETRY_S = 2.0
BURST_TRIES = 20
BURST_PAUSE_S = 30.0
AWAY_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
QUIET_POLL_S = 2.0
# Подключаемся строго по одной станции: чип Pi устанавливает одно LE-подключение за раз, а одновременные попытки
# мешают друг другу (2026-11-15: три станции сразу после перезапуска — по 30 «abort-by-local» у каждой, ни одной удачи).
# Замок общий у станций одной PowerService (StationReader.connect_lock).


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
AFTER_COMMAND_S = 1.5  # после команды прочитать через столько (станция успевает переключить)
LIGHT_BLINK_S = 1.0  # свет при тревоге: столько горит, столько не горит


class PowerError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


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
        self.connect_lock = asyncio.Lock()  # PowerService ставит один на все станции
        self._io = asyncio.Lock()  # чтение и команда не перемежаются
        self._wake = asyncio.Event()  # после команды — прочитать сразу

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
        async with self._io:
            return await self._read_once()

    async def command(self, frame: bytes) -> None:
        """Записать готовый кадр команды (собран протоколом) и прочитать новое состояние вскоре."""
        if not self.connected:
            raise PowerError(409, "The station is not connected")
        async with self._io:
            await self.link.write(self.proto.write_uuid, frame, self.proto.write_response)
        self._wake.set()

    async def _read_once(self) -> Any:
        loop = asyncio.get_running_loop()
        for _ in range(READ_TRIES):
            self._frames.clear()
            self._reply = loop.create_future()
            try:
                await self.link.write(self.proto.write_uuid, self.proto.request, self.proto.write_response)
                async with asyncio.timeout(READ_TIMEOUT_S):  # не wait_for: тот (3.11) может «съесть» отмену
                    frame = await self._reply
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
        async with self.connect_lock:
            await self.link.connect(list(self.proto.subscribe), [self.proto.write_uuid])

    async def _run(self) -> None:
        fails = 0
        away = 0  # подряд «нет в эфире»
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
                if "not found" in self.error:
                    away += 1
                    delay = AWAY_BACKOFF_S[min(away - 1, len(AWAY_BACKOFF_S) - 1)]
                elif fails % BURST_TRIES == 0:
                    away = 0
                    delay = BURST_PAUSE_S
                else:
                    away = 0
                    delay = CONNECT_RETRY_S
                if delay > CONNECT_RETRY_S:
                    log.info("power: %s %s not connected after %d tries (%s), next try in %.0f s", self.slot,
                             self.mac, fails, self.error, delay)
                await asyncio.sleep(delay)
                continue
            log.info("power: %s %s (%s) connected after %d failed tries", self.slot, self.mac, self.proto.kind,
                     fails)
            fails = away = 0
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
                self._wake.clear()
                try:
                    async with asyncio.timeout(POLL_S):
                        await self._wake.wait()
                except TimeoutError:
                    continue
                await asyncio.sleep(AFTER_COMMAND_S)  # после команды — новое состояние
            if not self.connected:
                log.info("power: %s %s disconnected", self.slot, self.mac)

    # ── наружу ──
    def slot_view(self) -> PowerSlot:
        r = self.fresh()
        if r is None:
            return PowerSlot()
        soc = float(r.soc_pct)
        delta = None
        if self.proto.kind == "daly":
            outputs = {"charge": bool(r.charge_on), "discharge": bool(r.discharge_on)}
            delta = r.delta_mv if r.delta_mv is not None else (
                r.cell_max_mv - r.cell_min_mv if r.cell_max_mv and r.cell_min_mv else None)
        elif self.proto.kind == "lead":
            outputs = {"charger": bool(r.charging)}
        else:
            outputs = dict(r.outputs)
        return PowerSlot(soc_pct=soc, voltage_v=getattr(r, "voltage_v", None), charging=getattr(r, "charging", None),
                         low=soc <= LOW_SOC_PCT, input_w=getattr(r, "input_w", None),
                         output_w=getattr(r, "output_w", None), to_empty_min=getattr(r, "to_empty_min", None),
                         outputs=outputs, charger_mode=getattr(r, "mode", None), cell_delta_mv=delta,
                         cell_delta_bad=delta is not None and delta > CELL_DELTA_BAD_MV)

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
        lock = asyncio.Lock()
        for r in (self.main, self.second, self.third):
            r.connect_lock = lock
        self._light_task: asyncio.Task | None = None
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

    # ── команды ──
    def _reader(self, slot: str) -> StationReader:
        r = {"main": self.main, "second": self.second, "third": self.third}.get(slot)
        if r is None:
            raise PowerError(404, "No such station")
        return r

    async def control(self, slot: str, cmd: PowerControl, source: str = "web") -> PowerStatus:
        """Команда станции: собирает кадр её протокол; неподходящая команда — 422, станция не подключена — 409."""
        r = self._reader(slot)
        try:
            if r.proto.kind == "fossibot" and cmd.output in fossibot.OUTPUT_REGS and cmd.on is not None:
                frame, what = fossibot.output_request(cmd.output, cmd.on), f"{cmd.output} {'on' if cmd.on else 'off'}"
            elif r.proto.kind == "daly" and cmd.output in daly.MOSFET_REGS and cmd.on is not None:
                frame, what = daly.mosfet_request(cmd.output, cmd.on), f"{cmd.output} {'on' if cmd.on else 'off'}"
            elif r.proto.kind == "lead" and cmd.output == "charger" and cmd.mode is not None:
                frame, what = leadbatt.mode_request(cmd.mode), f"charger {cmd.mode}"
            else:
                raise PowerError(422, f"{cmd.output} cannot be switched on this station")
        except KeyError as e:
            raise PowerError(422, "Unknown output") from e
        if slot == "main" and cmd.output == "light":
            await self._stop_light_task()  # рука хозяина важнее мигания
        log.warning("power: %s %s - %s (%s)", slot, r.mac, what, source)
        await r.command(frame)
        return self.status()

    def flash_light(self, seconds: float) -> None:
        """Охрана сработала: мигать светом Fossibot seconds секунд (новое событие продлевает), потом — как было."""
        if not self.get_settings().light_on_alarm or not self.main.connected:
            return
        until = time.monotonic() + seconds
        if self._light_task is not None and not self._light_task.done():
            self._light_until = until
            return
        self._light_until = until
        r = self.main.fresh()
        was_on = bool(r is not None and r.outputs.get("light"))
        self._light_task = asyncio.get_running_loop().create_task(self._blink(was_on), name="power-light")

    async def _blink(self, was_on: bool) -> None:
        on = not was_on
        try:
            while time.monotonic() < self._light_until and self.main.connected:
                await self.main.command(fossibot.output_request("light", on))
                on = not on
                await asyncio.sleep(LIGHT_BLINK_S)
        except Exception as e:  # noqa: BLE001 — станция отключилась и т.п.
            log.info("power: light flash stopped: %s", e)
        finally:
            with contextlib.suppress(Exception):
                if self.main.connected:
                    await self.main.command(fossibot.output_request("light", was_on))

    async def _stop_light_task(self) -> None:
        if self._light_task is not None and not self._light_task.done():
            self._light_until = 0.0  # _blink сам вернёт свет, как был
            with contextlib.suppress(Exception):
                async with asyncio.timeout(LIGHT_BLINK_S + READ_TIMEOUT_S):
                    await asyncio.shield(self._light_task)
        self._light_task = None

    async def stop_light(self) -> None:
        """Охрану сняли / заглушили — перестать мигать."""
        await self._stop_light_task()

    async def stop(self) -> None:
        self.running = False
        await cancel_tasks(self._light_task)
        await self.main.stop()
        await self.second.stop()
        await self.third.stop()

    def telemetry(self) -> PowerTelemetry:
        """Всегда (веб-интерфейс); плашку на экране прячет сам экран по power.show_on_screen."""
        return PowerTelemetry(main=self.main.slot_view(), second=self.second.slot_view(), third=self.third.slot_view())

    def status(self) -> PowerStatus:
        return PowerStatus(stations=[self.main.status(), self.second.status(), self.third.status()])
