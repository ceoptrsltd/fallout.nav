"""Настройки из окружения (systemd: EnvironmentFile=/etc/fnav-relay/relay.env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def parse_listen(value: str) -> tuple[str, int]:
    """`127.0.0.1:8095`, `[::1]:8095`, `:8095` → (хост, порт)."""
    host, sep, port = value.strip().rpartition(":")
    if not sep:
        raise ValueError(f"FNAV_RELAY_LISTEN must be host:port, got {value!r}")
    return host.strip("[]"), int(port)


@dataclass(frozen=True)
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8095
    db_path: str = "/var/lib/fnav-relay/track.db"
    device_token: str = ""
    radius_m: float = 50.0
    stop_min_s: float = 300.0
    cookie_path: str = "/fnadmin/"
    prefix: str = "/fnadmin"  # отрезается от пути запроса

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        host, port = parse_listen(e.get("FNAV_RELAY_LISTEN") or "127.0.0.1:8095")
        cfg = cls(
            listen_host=host,
            listen_port=port,
            db_path=e.get("FNAV_RELAY_DB") or cls.db_path,
            device_token=(e.get("FNAV_RELAY_DEVICE_TOKEN") or "").strip(),
            radius_m=float(e.get("FNAV_RELAY_RADIUS_M") or cls.radius_m),
            stop_min_s=float(e.get("FNAV_RELAY_STOP_MIN_S") or cls.stop_min_s),
            cookie_path=e.get("FNAV_RELAY_COOKIE_PATH") or cls.cookie_path,
            prefix=(e.get("FNAV_RELAY_PREFIX") or cls.prefix).rstrip("/"),
        )
        if cfg.radius_m <= 0 or cfg.stop_min_s < 0:
            raise ValueError("FNAV_RELAY_RADIUS_M must be > 0 and FNAV_RELAY_STOP_MIN_S >= 0")
        return cfg
