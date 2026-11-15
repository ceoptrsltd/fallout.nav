"""Датчик воздуха Qingping Air Monitor Lite (CGDN1): показания из вещания Bluetooth LE (fnav/core/advscan.py), без
подключения и без шифрования. docs/contracts/air.md.

Service data 0000fdcd-…: [служебный][тип устройства][MAC, 6 байт, задом наперёд], дальше записи «тип, длина,
значение» (little-endian): 01/4 — температура ÷10 (s16) и влажность ÷10 (u16); 12/4 — PM2.5 и PM10 (u16, мкг/м³);
13/2 — CO₂ (u16, ppm); 02/1 — батарея, %.

История: по одной записи в минуту (последнее показание минуты), 7 дней, в памяти и в файле air_history.json
(пишется раз в SAVE_S и при остановке). Для графиков отдаётся корзинами: мин / среднее / макс.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from fnav.core.advscan import AdvScanner
from fnav.core.util import cancel_tasks
from fnav.models import AirHistory, AirSeries, AirSettings, AirTelemetry

log = logging.getLogger("fnav.core.air")

SERVICE_UUID = "0000fdcd-0000-1000-8000-00805f9b34fb"
METRICS = ("temp_c", "humidity_pct", "co2_ppm", "pm25", "pm10")
KEEP_S = 7 * 86400.0
STALE_S = 900.0  # показание старше — на плитке прочерки (окно прослушивания раз в 1–3 мин, пакет ловится не в каждом)
SAVE_S = 600.0
MAX_POINTS = 720  # столько корзин в ответе самое большее


@dataclass(frozen=True)
class AirReading:
    temp_c: float | None = None
    humidity_pct: float | None = None
    co2_ppm: int | None = None
    pm25: int | None = None
    pm10: int | None = None
    battery_pct: int | None = None


def parse(data: bytes) -> AirReading | None:
    """Service data fdcd → показания; None — в пакете нет ни одного значения."""
    out: dict = {}
    i = 8
    while i + 2 <= len(data):
        t, n = data[i], data[i + 1]
        v = data[i + 2:i + 2 + n]
        if len(v) < n:
            break
        if t == 0x01 and n == 4:
            temp, hum = struct.unpack("<hH", v)
            out["temp_c"], out["humidity_pct"] = temp / 10, hum / 10
        elif t == 0x02 and n == 1:
            out["battery_pct"] = v[0]
        elif t == 0x12 and n == 4:
            out["pm25"], out["pm10"] = struct.unpack("<HH", v)
        elif t == 0x13 and n == 2:
            out["co2_ppm"] = struct.unpack("<H", v)[0]
        i += 2 + n
    return AirReading(**out) if out else None


def frame(temp_c: float, humidity_pct: float, pm25: int, pm10: int, co2_ppm: int) -> bytes:
    """Собрать service data (тесты и --mock)."""
    return (bytes.fromhex("88244bae7d3e4dec") + b"\x01\x04" + struct.pack("<hH", round(temp_c * 10), round(humidity_pct * 10))
            + b"\x12\x04" + struct.pack("<HH", pm25, pm10) + b"\x13\x02" + struct.pack("<H", co2_ppm))


def bucket(rows: list[list], t0: float, t1: float, step: float) -> AirHistory:
    """Записи [t, temp, hum, co2, pm25, pm10] → корзины по step секунд от t0 до t1: мин / среднее / макс."""
    n = max(1, int((t1 - t0) // step))
    acc: list[list[list[float]]] = [[[] for _ in METRICS] for _ in range(n)]
    for r in rows:
        k = int((r[0] - t0) // step)
        if 0 <= k < n:
            for m, v in enumerate(r[1:1 + len(METRICS)]):
                if v is not None:
                    acc[k][m].append(v)
    series = {}
    for m, name in enumerate(METRICS):
        cols = [acc[k][m] for k in range(n)]
        series[name] = AirSeries(
            min=[min(c) if c else None for c in cols],
            avg=[round(sum(c) / len(c), 1) if c else None for c in cols],
            max=[max(c) if c else None for c in cols])
    return AirHistory(t0=t0, step_s=step, n=n, **series)


class AirService:
    def __init__(self, scanner: AdvScanner, get_settings: Callable[[], AirSettings], path: Path,
                 now: Callable[[], float] = time.time) -> None:
        self.scanner = scanner
        self.get_settings = get_settings
        self.path = path
        self.now = now
        self.mac = ""
        self.reading: AirReading | None = None
        self.read_at: float | None = None
        self.rows: list[list] = []  # [t (начало минуты), temp, hum, co2, pm25, pm10]
        self._task: asyncio.Task | None = None
        self._dirty = False

    # ── жизнь ──
    async def start(self) -> None:
        self._load()
        self.apply_settings(self.get_settings())
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._saver(), name="air-save")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        self._task = None
        self._save()

    def apply_settings(self, s: AirSettings) -> None:
        mac = s.mac if s.enabled else ""
        if mac == self.mac:
            return
        if self.mac:
            self.scanner.unwatch(self.mac)
        self.mac = mac
        self.reading = self.read_at = None
        if mac:
            self.scanner.watch(mac, self._on_adv)

    # ── вещание ──
    def _on_adv(self, _mfr: dict[int, bytes], svc: dict[str, bytes]) -> None:
        data = svc.get(SERVICE_UUID)
        r = parse(data) if data else None
        if r is None:
            return
        # пакеты бывают неполные (только батарея) — недостающее берём из прошлого показания
        prev = asdict(self.reading) if self.reading is not None else {}
        merged = {**prev, **{k: v for k, v in asdict(r).items() if v is not None}}
        self.reading, self.read_at = AirReading(**merged), self.now()
        self._record()

    def _record(self) -> None:
        r, t = self.reading, self.read_at
        if r is None or t is None:
            return
        row = [t - t % 60, r.temp_c, r.humidity_pct, r.co2_ppm, r.pm25, r.pm10]
        if self.rows and self.rows[-1][0] == row[0]:
            self.rows[-1] = row
        else:
            self.rows.append(row)
            self._prune()
        self._dirty = True

    def _prune(self) -> None:
        edge = self.now() - KEEP_S
        if self.rows and self.rows[0][0] < edge:
            self.rows = [r for r in self.rows if r[0] >= edge]

    # ── файл ──
    def _load(self) -> None:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))["rows"]
            self.rows = [r for r in rows if isinstance(r, list) and len(r) == 1 + len(METRICS)]
            self._prune()
        except (OSError, ValueError, KeyError, TypeError):
            self.rows = []

    def _save(self) -> None:
        if not self._dirty:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"rows": self.rows}, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False
        except OSError as e:
            log.warning("air: cannot save the history: %s", e)

    async def _saver(self) -> None:
        while True:
            await asyncio.sleep(SAVE_S)
            self._save()

    # ── наружу ──
    def telemetry(self) -> AirTelemetry | None:
        """None — датчик не задан (плитки нет); без свежих данных — пустые значения."""
        if not self.mac:
            return None
        if self.reading is None or self.read_at is None or self.now() - self.read_at > STALE_S:
            return AirTelemetry()
        return AirTelemetry(**asdict(self.reading), age_s=round(self.now() - self.read_at))

    def history(self, hours: float) -> AirHistory:
        """Последние hours часов корзинами (не больше MAX_POINTS): час — по минуте, 7 дней — по 15 минут."""
        hours = max(1.0, min(KEEP_S / 3600, hours))
        step = max(60.0, 60.0 * round(hours * 3600 / MAX_POINTS / 60 + 0.4999))
        if hours >= 24 * 6:
            step = 900.0
        t1 = self.now()
        t1 = t1 - t1 % step + step
        return bucket(self.rows, t1 - hours * 3600, t1, step)
