"""Погода: Open-Meteo, расписание запросов, кэш без интернета, состояние для экрана."""

from __future__ import annotations

import json

import httpx
from core_helpers import login, make_opts, running_app

from fnav.core import weather as wmod
from fnav.models import GpsFix

NOW = 1_792_929_600.0  # 2026-10-25 12:00 UTC = 13:00 in Warsaw (Sunday)
DAY0 = 1_792_882_800  # local midnight of 2026-10-25, Europe/Warsaw (UTC+1)


def forecast(now: float = NOW) -> dict:
    hours = [DAY0 + 3600 * h for h in range(7 * 24)]
    return {
        "utc_offset_seconds": 3600,
        "current": {"time": int(now), "temperature_2m": 12.6, "apparent_temperature": 9.0, "weather_code": 3,
                    "is_day": 1, "wind_speed_10m": 5.3, "wind_direction_10m": 174, "wind_gusts_10m": 10.5},
        "hourly": {"time": hours, "temperature_2m": [float(h % 24) for h in range(len(hours))],
                   "apparent_temperature": [h % 24 - 2.0 for h in range(len(hours))],
                   "weather_code": [61] * len(hours), "is_day": [0] * len(hours),
                   "wind_speed_10m": [2.0] * len(hours), "wind_direction_10m": [90] * len(hours),
                   "wind_gusts_10m": [4.0] * len(hours)},
        "daily": {"time": [DAY0 + 86400 * d for d in range(7)], "weather_code": [61, 3, 2, 0, 0, 0, 0],
                  "temperature_2m_max": [12.8, 10.1, 11.3, 9, 9, 9, 9],
                  "temperature_2m_min": [4.4, 7.9, -5.2, 1, 1, 1, 1],
                  "precipitation_probability_max": [100, 93, 1, 0, 0, 0, 0]},
    }


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_status_current_and_next_two_days() -> None:
    st = wmod.weather_status({"fetched_at": NOW - 60, "lat": 52.2, "lon": 21.0, "forecast": forecast()}, NOW)
    assert (st.temp_c, st.feels_c, st.code, st.is_day, st.wind_ms, st.wind_from_deg) == (12.6, 9.0, 3, True, 5.3, 174)
    assert [(d.weekday, d.date, d.code, d.t_max, d.t_min, d.precip_prob) for d in st.days] == [
        ("MON", "2026-10-26", 3, 10.1, 7.9, 93), ("TUE", "2026-10-27", 2, 11.3, -5.2, 1)]
    assert st.stale is False and st.updated == NOW - 60


def test_status_offline_uses_the_hourly_forecast() -> None:
    cache = {"fetched_at": NOW, "lat": 52.2, "lon": 21.0, "forecast": forecast()}
    later = NOW + 26 * 3600 + 600  # next day, 15:10 local
    st = wmod.weather_status(cache, later)
    assert st.temp_c == 15.0 and st.code == 61 and st.wind_from_deg == 90 and st.is_day is False
    assert st.stale is True
    assert [d.weekday for d in st.days] == ["TUE", "WED"]  # the days move on with the clock
    assert wmod.weather_status(cache, NOW + 8 * 86400) is None  # too old to show anything


async def test_service_fetches_caches_and_backs_off(tmp_path) -> None:
    calls: list[httpx.Request] = []
    online = {"v": True}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if not online["v"]:
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, json=forecast())

    clk = Clock()
    gps = {"fix": GpsFix(fix="3d", lat=52.23, lon=21.01)}
    updates = []
    svc = wmod.WeatherService(tmp_path / "weather.json", lambda: gps["fix"], updates.append,
                              transport=httpx.MockTransport(handler), clock=clk)
    assert await svc.tick() is True
    q = calls[0].url.params
    assert q["latitude"] == "52.2300" and q["wind_speed_unit"] == "ms" and q["timeformat"] == "unixtime"
    assert updates and updates[-1].temp_c == 12.6
    assert json.loads((tmp_path / "weather.json").read_text())["lat"] == 52.23
    # not due yet: 10 min later, same place
    clk.t += 600
    assert await svc.tick() is False and len(calls) == 1
    # moved 40 km: refresh early
    gps["fix"] = GpsFix(fix="3d", lat=52.59, lon=21.01)
    assert await svc.tick() is True and len(calls) == 2
    # offline after 30 min: keeps the cache, retries in 5 min, not every minute
    online["v"] = False
    clk.t += 1800
    assert await svc.tick() is False and svc.error
    clk.t += 60
    assert await svc.tick() is False and len(calls) == 3
    assert svc.status().temp_c == 12.6
    # restart without internet and without a GPS fix: the cached forecast is still shown
    gps["fix"] = GpsFix()
    svc2 = wmod.WeatherService(tmp_path / "weather.json", lambda: gps["fix"], transport=httpx.MockTransport(handler),
                               clock=clk)
    svc2._load()
    assert svc2.status() is not None and svc2._position() == (52.59, 21.01)
    await svc.stop()
    await svc2.stop()


async def test_mock_forecast_and_ws_hello(data_dir) -> None:
    async with running_app(make_opts()) as c:
        core = c.app.state.core
        await core.weather.tick()  # --mock: a made-up forecast, no network
        await login(c)
        w = (await c.get("/api/weather")).json()["weather"]
        assert w["type"] == "weather" and len(w["days"]) == 2 and w["wind_ms"] is not None
