"""Датчик воздуха Qingping: разбор вещания, история по минутам, корзины для графиков (docs/contracts/air.md)."""

from __future__ import annotations

import json

from fnav.core import air
from fnav.core.advscan import FakeAdvScanner
from fnav.models import AirSettings

T0 = 1_794_770_000.0 - 1_794_770_000.0 % 3600  # ровный час


def test_real_packet_is_parsed():
    # пакет с устройства (2026-11-15): 22.8 °C, 38.4 %, PM2.5 7, PM10 9, CO2 902
    r = air.parse(bytes.fromhex("88244bae7d3e4dec0104e400800112040700090013028603"))
    assert (r.temp_c, r.humidity_pct, r.pm25, r.pm10, r.co2_ppm) == (22.8, 38.4, 7, 9, 902)
    assert air.parse(bytes.fromhex("88244bae7d3e4dec")) is None
    assert air.parse(air.frame(-3.5, 61.0, 12, 30, 1510)).temp_c == -3.5


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


async def test_history_keeps_a_row_per_minute_and_is_served_in_buckets(tmp_path):
    clock = Clock()
    sc = FakeAdvScanner()
    s = AirSettings()
    svc = air.AirService(sc, lambda: s, tmp_path / "air.json", now=clock)
    await svc.start()
    try:
        assert svc.telemetry() is not None and svc.telemetry().co2_ppm is None, "configured, no data yet"
        cb = next(iter(sc.watched.values()))
        for minute, co2 in enumerate([800, 900, 1000, 1600]):
            clock.t = T0 + minute * 60 + 5
            cb({}, {air.SERVICE_UUID: air.frame(21.0, 40.0, 5, 8, co2 - 50)})  # то же в ту же минуту — заменяется
            clock.t += 20
            cb({}, {air.SERVICE_UUID: air.frame(21.0 + minute, 40.0, 5, 8, co2)})
        assert len(svc.rows) == 4
        t = svc.telemetry()
        assert (t.co2_ppm, t.temp_c, t.age_s) == (1600, 24.0, 0)

        h = svc.history(1)
        assert h.step_s == 60 and h.n == 60
        vals = [v for v in h.co2_ppm.avg if v is not None]
        assert vals == [800, 900, 1000, 1600]

        week = svc.history(168)
        assert week.step_s == 900 and week.n == 672
        k = next(i for i, v in enumerate(week.co2_ppm.avg) if v is not None)
        assert (week.co2_ppm.min[k], week.co2_ppm.max[k], week.co2_ppm.avg[k]) == (800, 1600, 1075.0)

        clock.t += air.STALE_S + 1
        assert svc.telemetry().co2_ppm is None, "stale"
        clock.t = T0 + air.KEEP_S + 600
        cb({}, {air.SERVICE_UUID: air.frame(20.0, 40.0, 5, 8, 700)})
        assert len(svc.rows) == 1, "older than 7 days is dropped"
    finally:
        await svc.stop()
    assert json.loads((tmp_path / "air.json").read_text())["rows"][0][3] == 700

    again = air.AirService(sc, lambda: s, tmp_path / "air.json", now=clock)
    await again.start()
    await again.stop()
    assert len(again.rows) == 1, "the history survives a restart"

    off = AirSettings(enabled=False)
    svc.apply_settings(off)
    assert svc.telemetry() is None and not sc.watched
