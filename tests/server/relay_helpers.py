"""Общие помощники тестов fnav-relay (пакет лежит в server/)."""

from __future__ import annotations

import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

# всё из fnav_relay тесты берут отсюда: импорт ниже работает только после строки с sys.path
from fnav_relay import auth  # noqa: F401
from fnav_relay.api import RelayServer  # noqa: F401
from fnav_relay.config import Config
from fnav_relay.store import Store, haversine_m  # noqa: F401

DEVICE_TOKEN = "test-device-token-0123456789"
LAT0, LON0 = 50.06, 19.94


def at(m_north: float = 0.0, m_east: float = 0.0) -> tuple[float, float]:
    """Точка в m_north/m_east метрах от исходной."""
    lat = LAT0 + m_north / 111195.0
    lon = LON0 + m_east / (111195.0 * math.cos(math.radians(LAT0)))
    return lat, lon


def pt(ts: float, m_north: float = 0.0, m_east: float = 0.0, **kw) -> dict:
    lat, lon = at(m_north, m_east)
    return {"ts": ts, "lat": lat, "lon": lon, "speed_mps": 0.0, "course_deg": None, "hdop": 0.9,
            "fix": "3d", **kw}


@dataclass
class Resp:
    status: int
    data: object
    headers: object

    def cookies(self) -> list[str]:
        return self.headers.get_all("Set-Cookie") or []


@dataclass
class Relay:
    base: str
    store: Store
    cfg: Config

    def call(self, method: str, path: str, body: object = None, headers: dict | None = None,
             raw: bytes | None = None) -> Resp:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, text, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, text, hdrs = e.code, e.read(), e.headers
        return Resp(status, json.loads(text) if text else None, hdrs)

    def device(self, method: str, path: str, body: object = None) -> Resp:
        return self.call(method, path, body, {"X-Fnav-Device-Token": DEVICE_TOKEN})
