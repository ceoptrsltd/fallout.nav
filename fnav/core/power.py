"""Электростанции по Bluetooth LE (docs/contracts/power.md).

Главная — Fossibot (fnav/core/fossibot.py): заряд, % — на экране крупно. Вторая — Daly BMS (fnav/core/daly.py).
Третья — свинцовый аккумулятор по вещанию солнечного контроллера Victron (VictronStation, fnav/core/victron.py):
без подключения, только слушаем. Модуль FNAV-LEAD (fnav/core/leadbatt.py, протокол LEAD) сейчас не используется —
оставлен для будущей четвёртой станции.

Каждая станция — свой StationReader: держит постоянное подключение (fnav/core/ble.py) и читает показания редко —
раз в POLL_S (минута), а пока на колонке играет музыка — раз в POLL_MUSIC_S (5 минут): данные медленные, а радио
у чипа одно на Wi-Fi, колонку и станции (power.md «Радио»). Подключаются станции по очереди (общий connect_lock). Подключение LE у встроенного чипа Pi удаётся не с первого
раза (ble.py), поэтому неудачи сначала
повторяются часто (CONNECT_RETRY_S), а если станции так и нет (не нашлась в эфире или долго не подключается) —
всё реже (AWAY_BACKOFF_S): попытки занимают радио, общее с Wi-Fi. Задний вид на экране подключению не мешает
(колонку при нём не вызываем, fnav/core/bluetooth.py, а станции — подключаем).

Команды (control): только по слайдеру в веб-интерфейсе (POST /api/power/{slot}/control) и мигание светом Fossibot при
срабатывании охраны (flash_light, power.light_on_alarm). Каждый протокол сам собирает единственно допустимые кадры
(fossibot.output_request, daly.mosfet_request, leadbatt.mode_request) — другое записать нельзя. Сами по себе (при
запуске, переподключении, по таймеру) ничего не пишем. После команды — внеочередное чтение (видно новое состояние).

Станция держит одно подключение: пока мы подключены, её приложение на телефоне её не видит. release(slot, минуты) —
отключиться и не подключаться столько времени (кнопка на странице Bluetooth); 0 — подключиться сейчас (кнопка
«Reconnect now» и на Dashboard вместо серых слайдеров: прерывает и отпускание, и паузу между неудачными попытками).
Отпускание живёт до перезапуска fnav-core.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fnav.core import daly, fossibot, leadbatt, victron
from fnav.core.advscan import AdvScanner
from fnav.core.ble import BleError, BleLink
from fnav.core.leadsoc import LeadSoc
from fnav.core.util import cancel_tasks
from fnav.models import (
    PowerControl,
    PowerRelease,
    PowerSettings,
    PowerSlot,
    PowerStationStatus,
    PowerStatus,
    PowerTelemetry,
)

log = logging.getLogger("fnav.core.power")

POLL_S = 60.0  # данные медленные; радио общее с Wi-Fi и колонкой (A2DP)
POLL_MUSIC_S = 300.0  # пока на колонке играет музыка — ещё реже
POLL_FAIL_S = 30.0  # станция не ответила — переспросить через столько (а не через весь период)
READ_TIMEOUT_S = 3.0  # ответ приходит за 0.1–0.3 с
READ_TRIES = 3
READ_FAILS_DROP = 3  # столько опросов подряд без ответа — переподключиться
STALE_S = 100.0  # показания старше периода опроса на столько (три переспроса без ответа) — на экране прочерк
LOW_SOC_PCT = 20.0
CELL_DELTA_BAD_MV = 100  # Daly: разбаланс ячеек больше — опасно (на экране и в веб-интерфейсе мигает)
# Станция в эфире, а подключение сорвалось («le-connection-abort-by-local» — частая случайность, бывает и 30 раз
# подряд) — повторяем сериями: BURST_TRIES попыток через CONNECT_RETRY_S, пауза BURST_PAUSE_S, снова — пока не
# получится. Станции нет в эфире («not found nearby»: поиск занимает радио) — всё реже, AWAY_BACKOFF_S.
CONNECT_RETRY_S = 2.0
BURST_TRIES = 20
BURST_PAUSE_S = 30.0
AWAY_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)
RELEASE_CHECK_S = 5.0
MUSIC_RETRY_S = 30.0  # пока играет музыка — неудачное подключение повторять не чаще (серии попыток слышны)
# Подключаемся строго по одной станции: чип Pi устанавливает одно LE-подключение за раз, а одновременные попытки
# мешают друг другу (2026-11-15: три станции сразу после перезапуска — по 30 «abort-by-local» у каждой, ни одной удачи).
# Замок общий у станций одной PowerService (StationReader.connect_lock).


@dataclass(frozen=True)
class Protocol:
    kind: Literal["fossibot", "daly", "lead", "victron"]
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
AFTER_COMMAND_S = 1.0  # после команды прочитать через столько (станция успевает переключить)
LIGHT_WAIT_S = 0.5  # тревога, а станция не подключена — как часто смотреть, не подключилась ли
LIGHT_BLINK_S = 1.0  # свет при тревоге: столько горит, столько не горит
LIGHT_RESTORE_S = 180.0  # тревога кончилась: столько добиваемся, чтобы свет стал как до неё (связь могла пропасть)
LIGHT_CHECK_S = 0.1


SLOW_TIMEOUT_S = 15.0


async def slow_ble_links(fast: bool = False) -> None:
    """Замедлить LE-подключения к станциям (root: pi/systemd/fnav-ble-slow.service, pi/bin/fnav-ble-slow): BlueZ
    подключается с событием связи каждые 30–50 мс на станцию — три станции отнимали радио у колонки (музыка
    заикалась и щёлкала, 2026-11-15). Опрос раз в минуты — хватает события раз в 400–500 мс (реже — слайдеры на Dashboard отвечали бы вяло). fast — обратно на 30–50 мс
    (fnav-ble-fast.service): пока охрана мигает светом Fossibot, каждая команда должна дойти вовремя."""
    from fnav.core.system import run

    unit = "fnav-ble-fast" if fast else "fnav-ble-slow"
    rc, _out, err = await run("sudo", "-n", "/usr/bin/systemctl", "start", unit, timeout=SLOW_TIMEOUT_S)
    if rc != 0:
        raise BleError((err or "").strip() or f"systemctl exit code {rc}")


class PowerError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class StationReader:
    def __init__(self, slot: Literal["main", "second", "third"], proto: Protocol,
                 link_factory: LinkFactory = BleLink, mono: Callable[[], float] = time.monotonic,
                 busy: Callable[[], bool] = lambda: False) -> None:
        self.slot = slot
        self.proto = proto
        self.busy = busy  # на колонке играет музыка — подключаться реже (MUSIC_RETRY_S)
        self.hold: Callable[[], bool] = lambda: False  # охрана включена — станцию не отпускать (свет при тревоге)
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
        # подключились — замедлить подключение (редкие события связи не мешают музыке на колонке, slow_ble_links)
        self.after_connect: Callable[[], Awaitable[None]] | None = None
        self.released_until: float | None = None  # отпущена для приложения на телефоне — до этого mono()
        self._resume = asyncio.Event()  # отпускание кончилось раньше (кнопка «подключиться сейчас»)

    @property
    def connected(self) -> bool:
        return bool(self.link is not None and self.link.connected)

    def released_s(self) -> int | None:
        """Отпущена — сколько секунд ещё; нет — None."""
        if self.released_until is None or self.hold():
            return None
        left = self.released_until - self.mono()
        return math.ceil(left) if left > 0 else None

    def release(self, seconds: float) -> None:
        """Отключиться и не подключаться seconds секунд (0 — подключиться сейчас: и после отпускания, и вместо
        паузы между неудачными попытками)."""
        if seconds > 0:
            self.released_until = self.mono() + seconds
            self._resume.clear()
            self._wake.set()  # опрос просыпается и отключается
        else:
            self.released_until = None
            self._resume.set()

    async def _nap(self, seconds: float) -> bool:
        """Пауза между попытками; True — её прервали кнопкой «подключиться сейчас» (release(0))."""
        try:
            async with asyncio.timeout(seconds):
                await self._resume.wait()
        except TimeoutError:
            return False
        self._resume.clear()
        return True

    async def _wait_released(self) -> None:
        """Отпущена: отключиться и ждать, пока время не выйдет или не попросят подключиться."""
        if self.connected:
            log.info("power: %s %s released for %d s (the phone app can connect now)", self.slot, self.mac,
                     self.released_s() or 0)
            async with self._io:
                with contextlib.suppress(Exception):
                    await self.link.disconnect()
        self.error = None
        while (left := self.released_s()) is not None:
            try:
                async with asyncio.timeout(min(left, RELEASE_CHECK_S)):  # охрана включилась — вернуться раньше
                    await self._resume.wait()
            except TimeoutError:
                pass
            if self._resume.is_set():
                break
        self.released_until = None
        self._resume.clear()
        log.info("power: %s %s release over, connecting again", self.slot, self.mac)

    def poll_s(self) -> float:
        """Как часто читать станцию сейчас: играет музыка — реже."""
        return POLL_MUSIC_S if self.busy() else POLL_S

    def fresh(self) -> Any:
        """Показания не старше периода опроса + STALE_S или None."""
        if self.reading is None or self.read_at is None or self.mono() - self.read_at > self.poll_s() + STALE_S:
            return None
        return self.reading

    async def set_mac(self, mac: str) -> None:
        if mac == self.mac and (self._task is not None or not mac):
            return
        await self.stop()
        self.mac, self.reading, self.read_at, self.error = mac, None, None, None
        self.released_until = None
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

    async def command(self, frame: bytes, read: bool = True) -> None:
        """Записать готовый кадр команды (собран протоколом) и (read) прочитать новое состояние вскоре."""
        if not self.connected:
            raise PowerError(409, "The station is not connected")
        async with self._io:
            await self.link.write(self.proto.write_uuid, frame, self.proto.write_response)
        if read:
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
            if self.after_connect is not None:
                try:
                    await self.after_connect()
                except Exception as e:  # noqa: BLE001 — станция читается и на быстром интервале
                    log.warning("power: %s - slowing the connection down failed: %s", self.slot, e)

    async def _run(self) -> None:
        fails = 0
        away = 0  # подряд «нет в эфире»
        while True:
            if self.released_s() is not None:
                await self._wait_released()
                fails = away = 0
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
                if self.busy():
                    delay = max(delay, MUSIC_RETRY_S)
                if delay > CONNECT_RETRY_S:
                    log.info("power: %s %s not connected after %d tries (%s), next try in %.0f s", self.slot,
                             self.mac, fails, self.error, delay)
                if await self._nap(delay):  # «подключиться сейчас» — без паузы, счёт заново
                    fails = away = 0
                continue
            log.info("power: %s %s (%s) connected after %d failed tries", self.slot, self.mac, self.proto.kind,
                     fails)
            fails = away = 0
            self.error = None
            read_fails = 0
            while self.connected and self.released_s() is None:
                self._wake.clear()  # до чтения: команда или «отпустить» во время чтения не теряются
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
                try:
                    async with asyncio.timeout(POLL_FAIL_S if read_fails else self.poll_s()):
                        await self._wake.wait()
                except TimeoutError:
                    continue
                if self.released_s() is not None:
                    break  # отпустили — наверху цикла отключимся
                await asyncio.sleep(AFTER_COMMAND_S)  # после команды — новое состояние
            if not self.connected and self.released_s() is None:
                log.info("power: %s %s disconnected", self.slot, self.mac)

    # ── наружу ──
    def slot_view(self) -> PowerSlot:
        if (rel := self.released_s()) is not None:
            return PowerSlot(released_s=rel)  # данных нет, пока станция у приложения на телефоне
        r = self.fresh()
        if r is None:
            return PowerSlot()
        soc = float(r.soc_pct)
        delta = balancing = cells = None
        if self.proto.kind == "daly":
            cells = list(r.cells_mv) or None
            balancing = None if r.balancing is None else bool(r.balancing)
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
                         cell_delta_bad=delta is not None and delta > CELL_DELTA_BAD_MV, balancing=balancing,
                         cells_mv=cells)

    def status(self) -> PowerStationStatus:
        age = None if self.read_at is None else round(self.mono() - self.read_at, 1)
        reading = dataclasses.asdict(self.reading) if self.reading is not None else None
        return PowerStationStatus(slot=self.slot, kind=self.proto.kind, mac=self.mac, connected=self.connected,
                                  age_s=age, error=self.error, reading=reading, released_s=self.released_s())


REST_CURRENT_A = 0.3  # ток заряда от солнца меньше — его не считаем (заряд по напряжению это не искажает)
VICTRON_STALE_S = 420.0  # вещание ловим окнами раз в 1–3 мин (advscan) — дольше нет пакетов: прочерки


class VictronStation:
    """Третья станция: свинцовый аккумулятор по вещанию Victron SmartSolar (fnav/core/advscan.py). Заряд, % и
    зарядку чужим источником (сеть, DC-DC — контроллер их не видит) оценивает по напряжению fnav/core/leadsoc.py:
    процент — только в покое, во время зарядки и после неё держится последнее доверенное значение («примерно»)."""

    slot = "third"
    proto = Protocol("victron", (), "", "", False, b"", lambda: None, lambda _f: None)
    connected = False

    def __init__(self, scanner: AdvScanner, mono: Callable[[], float] = time.monotonic,
                 state_path: Path | None = None, wall: Callable[[], float] = time.time) -> None:
        self.scanner = scanner
        self.mono = mono
        self.mac = ""
        self.key = ""
        self.reading: victron.VictronReading | None = None
        self.read_at: float | None = None
        self.soc = LeadSoc(mono, wall, state_path)  # state_path None — без файла (тесты)
        self.error: str | None = None

    async def configure(self, mac: str, key: str) -> None:
        if (mac, key) == (self.mac, self.key):
            return
        if self.mac:
            self.scanner.unwatch(self.mac)
        self.mac, self.key = mac, key
        self.reading = self.read_at = None
        self.soc.reset_trend()  # батарея та же — доверенный заряд остаётся
        self.error = None if key or not mac else "No key (VictronConnect: Product info, Instant readout)"
        if mac and key:
            self.scanner.watch(mac, self._on_adv)

    async def stop(self) -> None:
        if self.mac:
            self.scanner.unwatch(self.mac)
        self.soc.save()

    def _on_adv(self, mfr: dict[int, bytes], _svc: dict[str, bytes]) -> None:
        raw = mfr.get(victron.COMPANY_ID)
        if raw is None or raw[:1] != b"\x10":
            return
        r = victron.parse(raw, self.key)
        if r is None:
            self.error = "The key does not fit (or this is not a solar charger)"
            return
        self.error = None
        self.reading, self.read_at = r, self.mono()
        if r.voltage_v is not None:
            self.soc.feed(r.voltage_v, r.charging or (r.current_a or 0) >= REST_CURRENT_A, r.state)

    def fresh(self) -> victron.VictronReading | None:
        if self.reading is None or self.read_at is None or self.mono() - self.read_at > VICTRON_STALE_S:
            return None
        return self.reading

    def released_s(self) -> None:
        return None

    def slot_view(self) -> PowerSlot:
        r = self.fresh()
        if r is None or r.voltage_v is None:
            return PowerSlot()
        soc, approx = self.soc.view(r.voltage_v)
        # мощность — только солнечной зарядки; чужой источник — молния без ватт (их не знаем)
        return PowerSlot(soc_pct=soc, soc_approx=approx, voltage_v=r.voltage_v, charging=self.soc.charging,
                         low=soc is not None and soc <= LOW_SOC_PCT,
                         input_w=r.pv_power_w if r.charging else None if self.soc.external else 0)

    def status(self) -> PowerStationStatus:
        age = None if self.read_at is None else round(self.mono() - self.read_at, 1)
        reading = dataclasses.asdict(self.reading) if self.reading is not None else None
        if reading is not None:
            view = self.slot_view()
            reading["soc_pct"], reading["soc_approx"] = view.soc_pct, view.soc_approx
            reading["external_charging"] = self.soc.external
            if self.reading.voltage_v is not None:
                reading["soc_note"] = self.soc.note(self.reading.voltage_v)
        return PowerStationStatus(slot="third", kind="victron", mac=self.mac, connected=self.fresh() is not None,
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
                 link_factory: LinkFactory | None = None,
                 tune: Callable[[bool], Awaitable[None]] | None = None,
                 busy: Callable[[], bool] = lambda: False, guard: Callable[[], bool] = lambda: False,
                 scanner: AdvScanner | None = None, lead_state: Path | None = None) -> None:
        self.get_settings = get_settings
        self.mock = mock

        def factory(proto: Protocol) -> LinkFactory:
            if link_factory is not None:
                return link_factory
            if mock:
                return lambda mac, cb: FakeLink(proto, mac, cb)
            return BleLink

        self.main = StationReader("main", FOSSIBOT, factory(FOSSIBOT))
        self.second = StationReader("second", DALY, factory(DALY))
        lock = asyncio.Lock()
        self.connect_lock = lock  # поиск вещания (advscan) берёт его же
        self.scanner = scanner or AdvScanner(busy, lock)
        self.scanner.lock = lock
        self.third = VictronStation(self.scanner, state_path=lead_state)  # заряд свинцового — в файле
        for r in (self.main, self.second):
            r.connect_lock = lock
            r.after_connect = self._tune if tune is not None else None
            # музыка играет — подключаться реже; но мигает свет (тревога) — без пауз: музыка может играть и при
            # включённой охране, поэтому паузы снимает только сама тревога
            r.busy = lambda: busy() and not self._flashing()
        self.tune = tune  # tune(fast): интервал связи станций — быстрый (мигает свет) или медленный (slow_ble_links)
        self.guard = guard  # охрана включена: главную станцию держим наготове (свет при тревоге)
        self.main.hold = guard
        self._light_task: asyncio.Task | None = None
        self.running = False
        self._lock = asyncio.Lock()  # настройки меняются подряд — переподключения по очереди

    async def start(self, background: bool = True) -> None:
        self.running = background
        await self.apply_settings(self.get_settings())

    async def apply_settings(self, s: PowerSettings) -> None:
        if not self.running:
            return
        main_mac, second_mac = s.main_mac, s.second_mac
        if self.mock:  # в демо станции «есть» и без настроек
            main_mac, second_mac = main_mac or "70:AF:09:00:00:01", second_mac or "74:C1:4E:00:00:02"
        async with self._lock:
            await self.main.set_mac(main_mac)
            await self.second.set_mac(second_mac)
            # в демо ключ «есть» и без настроек (fnav/core/runtime.py mock_adv_scanner)
            await self.third.configure(s.victron_mac, s.victron_key or ("00112233445566778899aabbccddeeff" if self.mock else ""))

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
            self._light_until = 0.0  # рука хозяина важнее мигания: и возвращать «как было» не нужно — решает он
            await cancel_tasks(self._light_task)
            self._light_task = None
        log.warning("power: %s %s - %s (%s)", slot, r.mac, what, source)
        await r.command(frame)
        return self.status()

    def release(self, slot: str, body: PowerRelease) -> PowerStatus:
        """Отпустить станцию для приложения на телефоне на body.minutes (нет — power.release_min); 0 — вернуть сейчас."""
        r = self._reader(slot)
        if isinstance(r, VictronStation):
            raise PowerError(409, "This station is only listened to: its phone app works at the same time")
        if not r.mac:
            raise PowerError(409, "The station has no address")
        if slot == "main" and self.guard() and body.minutes != 0:
            raise PowerError(409, "The alarm is on: the station stays connected to flash its light")
        minutes = self.get_settings().release_min if body.minutes is None else body.minutes
        if minutes and slot == "main":
            self._light_until = 0.0  # мигание при тревоге без станции не нужно
        r.release(minutes * 60.0)
        log.info("power: %s %s - %s", slot, r.mac, f"release for {minutes} min" if minutes else "reconnect now")
        return self.status()

    def _flashing(self) -> bool:
        return self._light_task is not None and not self._light_task.done()

    async def _tune(self) -> None:
        """Интервал связи станций: быстрый, пока мигает свет, иначе медленный (после каждого подключения тоже)."""
        if self.tune is not None:
            await self.tune(self._flashing())

    def flash_light(self, seconds: float) -> None:
        """Охрана сработала: мигать светом Fossibot seconds секунд (новое событие продлевает), потом — как было.
        Станция в этот момент не подключена — подключаемся без пауз и мигаем остаток времени."""
        if not self.get_settings().light_on_alarm or not self.main.mac:
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
        """Только регистр света (fossibot.output_request("light", …)); другие выходы станции не трогаем.
        Мигание кончилось — свет как до тревоги (_restore_light); новое событие в это время — мигаем снова."""
        try:
            while True:
                await self._blink_until(was_on)
                if await self._restore_light(was_on):
                    break
        except asyncio.CancelledError:  # служба останавливается / хозяин сам переключил свет: одна попытка
            with contextlib.suppress(Exception):
                if self.main.connected:
                    await self.main.command(fossibot.output_request("light", was_on), read=False)
            raise
        finally:
            if self._light_task is asyncio.current_task():
                self._light_task = None
            with contextlib.suppress(Exception):
                await self._tune()  # обратно на медленный интервал

    async def _blink_until(self, was_on: bool) -> None:
        on = not was_on
        fast = False
        while time.monotonic() < self._light_until:
            if not self.main.connected:
                fast = False
                self.main.release(0)  # без паузы между попытками
                await asyncio.sleep(LIGHT_WAIT_S)
                continue
            if not fast:  # команды каждую секунду — на быстром интервале связи (музыка сейчас не важна)
                fast = True
                with contextlib.suppress(Exception):
                    await self._tune()
            try:
                await self.main.command(fossibot.output_request("light", on), read=False)
                on = not on
            except (PowerError, BleError) as e:
                log.info("power: light flash: %s", e)
            await asyncio.sleep(LIGHT_BLINK_S)

    async def _restore_light(self, was_on: bool) -> bool:
        """Свет — как был до тревоги, и станция это подтвердила свежим чтением. Связь могла пропасть на последнем
        «включить», запись — не пройти: тогда свет горел бы до утра. Повторяем не дольше LIGHT_RESTORE_S.
        → False: пришло новое событие тревоги — снова мигать; True — готово (или не вышло — в журнал)."""
        deadline = time.monotonic() + LIGHT_RESTORE_S
        while time.monotonic() < deadline:
            if time.monotonic() < self._light_until:
                return False
            if not self.main.connected:
                self.main.release(0)  # без паузы между попытками
                await asyncio.sleep(LIGHT_WAIT_S)
                continue
            sent = self.main.mono()
            try:
                await self.main.command(fossibot.output_request("light", was_on))  # read: прочитать сразу после
            except (PowerError, BleError) as e:
                log.info("power: light restore: %s", e)
                await asyncio.sleep(LIGHT_WAIT_S)
                continue
            t = 0.0
            while t < 2 * READ_TIMEOUT_S and (self.main.read_at is None or self.main.read_at <= sent):
                await asyncio.sleep(LIGHT_CHECK_S)
                t += LIGHT_CHECK_S
            r = self.main.reading if self.main.read_at is not None and self.main.read_at > sent else None
            if r is not None and bool(getattr(r, "outputs", {}).get("light")) == was_on:
                log.info("power: light back %s after the alarm", "on" if was_on else "off")
                return True
        log.warning("power: could not put the station light back %s after the alarm", "on" if was_on else "off")
        return True

    async def _stop_light_task(self) -> None:
        """Перестать мигать; свет вернёт как был сама задача (_restore_light) — ждём её недолго, дальше она в фоне."""
        if self._light_task is not None and not self._light_task.done():
            self._light_until = 0.0
            with contextlib.suppress(Exception):
                async with asyncio.timeout(LIGHT_BLINK_S + 2 * READ_TIMEOUT_S):
                    await asyncio.shield(self._light_task)

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
