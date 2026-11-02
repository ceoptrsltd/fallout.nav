"""Часовой пояс устройства следует за машиной: Open-Meteo называет пояс точки, два прогноза подряд — смена;
имя пояса и tzset в процессах (fnav/tz.py); пояс в API для админки."""

from __future__ import annotations

import os

import httpx
import pytest
from core_helpers import login, make_opts, running_app
from test_core_weather import NOW, Clock, forecast

from fnav import tz
from fnav.core import timezone as tzmod
from fnav.core import weather as wmod
from fnav.models import GpsFix


class Follower:
    """TimeZoneFollower с подменённой системой: текущий пояс и timedatectl."""

    def __init__(self, zone: str = "Europe/Warsaw", *, enabled: bool = True, mock: bool = False,
                 error: str | None = None) -> None:
        self.zone = zone
        self.enabled = enabled
        self.error = error
        self.calls: list[str] = []
        self.f = tzmod.TimeZoneFollower(lambda: self.enabled, mock=mock, current=lambda: self.zone,
                                        setter=self._set)

    async def _set(self, zone: str) -> str | None:
        self.calls.append(zone)
        if self.error:
            return self.error
        self.zone = zone
        return None


@pytest.fixture
def tzsets(monkeypatch):
    n = []
    monkeypatch.setattr(tz, "tzset", lambda: n.append(1))
    return n


async def test_two_forecasts_in_a_row_switch_the_zone(tzsets) -> None:
    s = Follower()
    assert await s.f.observe("Europe/Warsaw") is False  # тот же пояс
    assert await s.f.observe("Europe/Berlin") is False and s.f.candidate == "Europe/Berlin"  # первый — ждём
    assert await s.f.observe("Europe/Berlin") is True  # второй подряд — смена
    assert s.calls == ["Europe/Berlin"] and s.zone == "Europe/Berlin" and tzsets == [1]
    assert s.f.candidate is None and await s.f.observe("Europe/Berlin") is False and s.calls == ["Europe/Berlin"]


async def test_no_flapping_near_a_border(tzsets) -> None:
    s = Follower("Europe/Lisbon")
    for z in ("Europe/Madrid", "Europe/Lisbon", "Europe/Madrid", "Europe/Lisbon"):  # едем вдоль границы
        assert await s.f.observe(z) is False
    assert await s.f.observe("Europe/Madrid") is False  # A, B, A — счёт заново
    assert await s.f.observe("Europe/Paris") is False and s.f.candidate == "Europe/Paris"
    assert s.calls == [] and tzsets == []


async def test_ignored_zones_and_switches(tzsets) -> None:
    s = Follower(enabled=False)
    for _ in range(3):
        assert await s.f.observe("Europe/Prague") is False  # выключено настройкой
    s.enabled = True
    for bad in (None, "", "Etc/GMT+1", "GMT", "UTC", "../etc/shadow", "Europe/Prague; reboot"):  # море, мусор
        assert await s.f.observe(bad) is False
    assert s.calls == []
    assert await s.f.observe("Europe/Prague") is False and await s.f.observe("Europe/Prague") is True
    # --mock: пояс системы не трогаем
    m = Follower(mock=True)
    assert [await m.f.observe("Europe/Rome") for _ in range(2)] == [False, False] and m.calls == []


async def test_failed_switch_is_logged_and_retried(tzsets, caplog) -> None:
    s = Follower(error="Access denied")
    assert [await s.f.observe("Europe/Vienna") for _ in range(2)] == [False, False]
    assert s.calls == ["Europe/Vienna"] and s.zone == "Europe/Warsaw" and tzsets == []
    assert "cannot switch Europe/Warsaw -> Europe/Vienna: Access denied" in caplog.text
    s.error = None
    assert [await s.f.observe("Europe/Vienna") for _ in range(2)] == [False, True]


async def test_set_system_zone_runs_timedatectl(monkeypatch) -> None:
    calls = []

    async def fake_run(*args, timeout=5.0):
        calls.append(args)
        return (0, "", "") if args[-1] == "Europe/Oslo" else (1, "", "Failed to set time zone: Access denied\n")

    monkeypatch.setattr(tzmod, "run", fake_run)
    assert await tzmod.set_system_zone("Europe/Oslo") is None
    assert calls[-1] == ("timedatectl", "--no-ask-password", "set-timezone", "Europe/Oslo")
    assert await tzmod.set_system_zone("Europe/Kyiv") == "Failed to set time zone: Access denied"


async def test_weather_passes_the_zone_only_for_a_live_position(tmp_path) -> None:
    zones = iter(["Europe/Lisbon", "Europe/Madrid"])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**forecast(), "timezone": next(zones), "utc_offset_seconds": 0})

    seen: list[str | None] = []

    async def on_zone(z):
        seen.append(z)

    clk = Clock()
    gps = {"fix": GpsFix(fix="3d", lat=38.72, lon=-9.14)}
    svc = wmod.WeatherService(tmp_path / "weather.json", lambda: gps["fix"], transport=httpx.MockTransport(handler),
                              clock=clk, on_zone=on_zone)
    assert await svc.tick() is True and seen == ["Europe/Lisbon"]
    # без фикса прогноз берётся для последней точки из кэша — это не позиция машины, пояс не считается
    gps["fix"] = GpsFix()
    clk.t += wmod.REFRESH_S
    assert await svc.tick() is True and seen == ["Europe/Lisbon"]
    await svc.stop()


async def test_core_follows_the_forecast_zone(data_dir, monkeypatch, tzsets) -> None:
    zone = {"v": "Europe/Warsaw"}
    monkeypatch.setattr(tz, "zone_name", lambda path=tz.LOCALTIME: zone["v"])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**forecast(), "timezone": "Europe/Lisbon", "utc_offset_seconds": 0})

    async with running_app(make_opts(weather_transport=httpx.MockTransport(handler))) as c:
        core = c.app.state.core
        assert core.store.get().system.auto_timezone is True  # по умолчанию включено
        set_calls = []

        async def setter(z):
            set_calls.append(z)
            zone["v"] = z

        core.weather.mock = False  # настоящий запрос (через подменённый транспорт)
        core.timezone.mock = False
        core.timezone.setter = setter
        core.timezone.current = lambda: zone["v"]
        core.weather.clock = Clock(NOW)
        core.weather.get_gps = lambda: GpsFix(fix="3d", lat=38.72, lon=-9.14)
        assert await core.weather.tick() is True and set_calls == []
        core.weather.clock.t += wmod.REFRESH_S
        assert await core.weather.tick() is True and set_calls == ["Europe/Lisbon"] and tzsets

        # пояс — в статусе системы и в сессии (только со входом: пояс ≈ страна, где машина)
        assert "timezone" not in (await c.get("/api/session")).json()
        await login(c)
        assert (await c.get("/api/session")).json()["timezone"] == "Europe/Lisbon"
        assert (await c.get("/api/system/status")).json()["system"]["timezone"] == "Europe/Lisbon"
        assert core.build_telemetry().system.timezone == "Europe/Lisbon"

        # выключатель в настройках системы
        r = await c.patch("/api/settings", json={"system": {"auto_timezone": False}})
        assert r.status_code == 200 and r.json()["system"]["auto_timezone"] is False
        core.weather.cache = {}
        assert await core.timezone.observe("Europe/Madrid") is False
        assert await core.timezone.observe("Europe/Madrid") is False and set_calls == ["Europe/Lisbon"]


def test_zone_name_from_localtime_link_or_timezone_file(tmp_path) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    lt = etc / "localtime"
    assert tz.zone_name(lt) is None  # ничего нет (Windows, контейнер)
    (etc / "timezone").write_text("Europe/Warsaw\n")
    assert tz.zone_name(lt) == "Europe/Warsaw"
    (etc / "timezone").write_text("not a zone!\n")
    assert tz.zone_name(lt) is None
    zi = tmp_path / "usr/share/zoneinfo/America/Argentina"
    zi.mkdir(parents=True)
    (zi / "Salta").write_bytes(b"TZif")
    try:
        os.symlink(zi / "Salta", lt)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here (Windows without the privilege)")
    assert tz.zone_name(lt) == "America/Argentina/Salta"


def test_zone_watch_calls_tzset_when_the_zone_file_changes(tmp_path, tzsets) -> None:
    lt = tmp_path / "localtime"
    lt.write_bytes(b"TZif one")
    w = tz.ZoneWatch(lt)
    assert w.check() is False and tzsets == []
    lt.unlink()
    lt.write_bytes(b"TZif second zone")  # timedatectl ставит новую ссылку: другой файл
    assert w.check() is True and tzsets == [1]
    assert w.check() is False and tzsets == [1]
    assert tz.ZoneWatch(tmp_path / "missing").check() is False


def test_valid_zone() -> None:
    for ok in ("Europe/Warsaw", "America/Argentina/Salta", "UTC", "Etc/GMT+1", "America/Port-au-Prince"):
        assert tz.valid_zone(ok), ok
    for bad in (None, "", "/etc/passwd", "../x", "Europe/../../x", "a b", "Europe/Warsaw\n", 5, "x" * 65):
        assert not tz.valid_zone(bad), bad
