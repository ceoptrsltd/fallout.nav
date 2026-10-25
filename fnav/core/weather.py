"""Погода по координатам GPS: Open-Meteo (open-meteo.com — бесплатно, без ключа, лучшие модели для места:
ICON, ECMWF, GFS…; данные CC BY 4.0, подпись — в веб-интерфейсе).

Запрос — раз в 30 мин, и сразу, если машина отъехала от точки прогноза дальше 25 км (не чаще раза в 5 мин).
Нет интернета — повтор через 5 мин. Ответ (текущая погода, почасовой и дневной прогноз на 7 дней) лежит в
data_dir/weather.json: без связи экран показывает почасовой прогноз из кэша на текущий час. Экран получает
компактное состояние (WeatherStatus) сообщением {"type": "weather", …} по WebSocket — при подключении и раз
в минуту. В --mock — вымышленный прогноз без сети.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from fnav.models import GpsFix, WeatherDay, WeatherStatus

log = logging.getLogger("fnav.core.weather")

URL = "https://api.open-meteo.com/v1/forecast"
FIELDS = "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
DAILY = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
REFRESH_S = 1800.0
MOVED_KM = 25.0
MOVED_MIN_S = 300.0
RETRY_S = 300.0
CURRENT_FRESH_S = 3600.0  # дольше — «сейчас» берём из почасового прогноза
STALE_S = 3 * 3600.0
MAX_AGE_S = 6 * 86400.0  # старше — прогноз уже не на что показывать
TICK_S = 60.0
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def _at(block: dict, key: str, i: int) -> Any:
    arr = block.get(key)
    return arr[i] if isinstance(arr, list) and 0 <= i < len(arr) else None


def valid_forecast(d: Any) -> bool:
    return (isinstance(d, dict) and isinstance(d.get("hourly"), dict) and isinstance(d["hourly"].get("time"), list)
            and isinstance(d.get("daily"), dict) and isinstance(d["daily"].get("time"), list))


def weather_status(cache: dict, now: float) -> WeatherStatus | None:
    """Кэш (ответ Open-Meteo + fetched_at) → состояние для экрана на момент now."""
    fc = cache.get("forecast")
    fetched = _num(cache.get("fetched_at"))
    if not valid_forecast(fc) or fetched is None or now - fetched > MAX_AGE_S:
        return None
    offset = _num(fc.get("utc_offset_seconds")) or 0.0
    cur = fc.get("current") if isinstance(fc.get("current"), dict) else {}
    hourly = fc["hourly"]

    if cur and now - fetched < CURRENT_FRESH_S:
        src, i = {k: [v] for k, v in cur.items()}, 0
    else:  # без связи: прогноз на текущий час
        times = hourly["time"]
        i = max((k for k, t in enumerate(times) if isinstance(t, int | float) and t <= now), default=-1)
        if i < 0 or now - times[i] > 3600:
            return None
        src = hourly
    temp = _num(_at(src, "temperature_2m", i))
    if temp is None:
        return None
    code = _at(src, "weather_code", i)

    daily = fc["daily"]
    today = datetime.fromtimestamp(now + offset, UTC).date()
    days: list[WeatherDay] = []
    for k, t in enumerate(daily["time"]):
        if not isinstance(t, int | float):
            continue
        d = datetime.fromtimestamp(t + offset, UTC).date()
        if d <= today:
            continue
        prob = _num(_at(daily, "precipitation_probability_max", k))
        dcode = _at(daily, "weather_code", k)
        days.append(WeatherDay(date=d.isoformat(), weekday=DAYS[d.weekday()],
                               code=dcode if isinstance(dcode, int) else None,
                               t_max=_num(_at(daily, "temperature_2m_max", k)),
                               t_min=_num(_at(daily, "temperature_2m_min", k)),
                               precip_prob=round(prob) if prob is not None else None))
        if len(days) == 2:
            break
    return WeatherStatus(
        temp_c=temp, feels_c=_num(_at(src, "apparent_temperature", i)),
        code=code if isinstance(code, int) else None, is_day=_at(src, "is_day", i) != 0,
        wind_ms=_num(_at(src, "wind_speed_10m", i)), gust_ms=_num(_at(src, "wind_gusts_10m", i)),
        wind_from_deg=_num(_at(src, "wind_direction_10m", i)), days=days, updated=fetched,
        stale=now - fetched > STALE_S, lat=_num(cache.get("lat")), lon=_num(cache.get("lon")),
    )


def fake_forecast(lat: float, lon: float, now: float) -> dict:
    """--mock: правдоподобный прогноз в формате Open-Meteo (без сети)."""
    start = int(now // 86400 * 86400)
    hours = [start + 3600 * h for h in range(7 * 24)]
    temp = [round(9 + 5 * math.sin((h % 24 - 9) / 24 * 2 * math.pi), 1) for h in range(len(hours))]
    wind_dir = [(200 + 3 * h) % 360 for h in range(len(hours))]
    codes = [3, 61, 2, 0, 80, 71, 45]
    return {
        "utc_offset_seconds": 0,
        "current": {"time": int(now), "temperature_2m": 11.4, "apparent_temperature": 8.6, "weather_code": 2,
                    "is_day": 1, "wind_speed_10m": 6.2, "wind_direction_10m": 235, "wind_gusts_10m": 11.0},
        "hourly": {"time": hours, "temperature_2m": temp, "apparent_temperature": [t - 3 for t in temp],
                   "weather_code": [codes[(h // 24) % 7] for h in range(len(hours))],
                   "is_day": [1 if 7 <= h % 24 < 18 else 0 for h in range(len(hours))],
                   "wind_speed_10m": [4.0 + (h % 7) for h in range(len(hours))], "wind_direction_10m": wind_dir,
                   "wind_gusts_10m": [8.0 + (h % 5) for h in range(len(hours))]},
        "daily": {"time": [start + 86400 * d for d in range(7)], "weather_code": codes,
                  "temperature_2m_max": [14.0, 12.5, 16.0, 17.5, 11.0, 3.0, 8.0],
                  "temperature_2m_min": [6.0, 7.5, 5.0, 8.0, 4.0, -4.0, 1.0],
                  "precipitation_probability_max": [10, 85, 5, 0, 60, 70, 20]},
        "latitude": lat, "longitude": lon,
    }


class WeatherService:
    def __init__(self, cache_path: Path, get_gps: Callable[[], GpsFix],
                 on_update: Callable[[WeatherStatus], None] | None = None, *, mock: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cache_path = cache_path
        self.get_gps = get_gps
        self.on_update = on_update
        self.mock = mock
        self.clock = clock
        self.cache: dict = {}
        self.error: str | None = None
        self._retry_at = 0.0
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), transport=transport,
                                         headers={"User-Agent": "fallout.nav/1.0 (car computer)"})

    # ── жизненный цикл ──
    def start(self) -> None:
        self._load()
        self._task = asyncio.create_task(self._loop(), name="weather")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._client.aclose()

    def _load(self) -> None:
        try:
            d = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(d, dict) and valid_forecast(d.get("forecast")):
            self.cache = d

    def _save(self) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.cache), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning("weather: cannot save %s: %s", self.cache_path, e)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("weather")
            st = self.status()
            if st is not None and self.on_update:
                self.on_update(st)
            await asyncio.sleep(TICK_S)

    # ── состояние ──
    def status(self) -> WeatherStatus | None:
        return weather_status(self.cache, self.clock()) if self.cache else None

    def _position(self) -> tuple[float, float] | None:
        g = self.get_gps()
        if g.fix != "none" and g.lat is not None and g.lon is not None:
            return g.lat, g.lon
        lat, lon = _num(self.cache.get("lat")), _num(self.cache.get("lon"))
        return (lat, lon) if lat is not None and lon is not None else None

    def due(self, pos: tuple[float, float], now: float) -> bool:
        fetched = _num(self.cache.get("fetched_at"))
        if fetched is None:
            return True
        age = now - fetched
        if age >= REFRESH_S:
            return True
        lat, lon = _num(self.cache.get("lat")), _num(self.cache.get("lon"))
        far = lat is None or lon is None or distance_km(lat, lon, *pos) > MOVED_KM
        return far and age >= MOVED_MIN_S

    async def tick(self) -> bool:
        """Запросить прогноз, если пора. True — получен новый."""
        now = self.clock()
        pos = self._position()
        if pos is None or now < self._retry_at or not self.due(pos, now):
            return False
        try:
            fc = fake_forecast(*pos, now) if self.mock else await self._fetch(*pos)
        except (httpx.HTTPError, ValueError) as e:
            if self.error is None:
                log.warning("weather: %s (keeping the cached forecast, retry every %.0f min)", e, RETRY_S / 60)
            self.error = str(e) or type(e).__name__
            self._retry_at = now + RETRY_S
            return False
        self.cache = {"fetched_at": now, "lat": round(pos[0], 4), "lon": round(pos[1], 4), "forecast": fc}
        self.error = None
        await asyncio.to_thread(self._save)
        log.info("weather: forecast for %.2f, %.2f updated", *pos)
        st = self.status()
        if st is not None and self.on_update:
            self.on_update(st)
        return True

    async def _fetch(self, lat: float, lon: float) -> dict:
        q = {"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "current": FIELDS, "hourly": FIELDS,
             "daily": DAILY, "timezone": "auto", "forecast_days": "7", "wind_speed_unit": "ms",
             "timeformat": "unixtime"}
        r = await self._client.get(URL, params=q)
        r.raise_for_status()
        d = r.json()
        if not valid_forecast(d):
            raise ValueError("unexpected answer from Open-Meteo")
        return d
