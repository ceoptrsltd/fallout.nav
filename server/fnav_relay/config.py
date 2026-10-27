"""Настройки из окружения (systemd: EnvironmentFile=/etc/fnav-relay/relay.env)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass


def parse_listen(value: str) -> tuple[str, int]:
    """`127.0.0.1:8095`, `[::1]:8095`, `:8095` → (хост, порт)."""
    host, sep, port = value.strip().rpartition(":")
    if not sep:
        raise ValueError(f"FNAV_RELAY_LISTEN must be host:port, got {value!r}")
    return host.strip("[]"), int(port)


# sub в JWT VAPID — контакт владельца сервера. Строго mailto:адрес или https://хост[:порт] с публичным именем:
# Apple отвечает BadJwtToken на прочее (localhost, IP, пробелы)
_SUB_RE = re.compile(r"^(mailto:[^\s@/]+@[^\s@/]+\.[A-Za-z][^\s@/]*"
                     r"|https://(?:[A-Za-z0-9-]+\.)+[A-Za-z][A-Za-z0-9-]*(?::\d{1,5})?)$")


def valid_vapid_sub(value: str) -> bool:
    if not _SUB_RE.match(value):
        return False
    host = value.split("@")[-1] if value.startswith("mailto:") else value[len("https://"):].split(":")[0]
    return not host.lower().endswith(".localhost")


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
    vapid_sub: str = ""  # sub в JWT VAPID; пусто — https://<Host> первой подписки (сохраняется в базе)
    push_allow_http: bool = False  # endpoint подписки по http — только для тестов (браузеры дают https)

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
            vapid_sub=(e.get("FNAV_RELAY_VAPID_SUB") or "").strip(),
        )
        if cfg.radius_m <= 0 or cfg.stop_min_s < 0:
            raise ValueError("FNAV_RELAY_RADIUS_M must be > 0 and FNAV_RELAY_STOP_MIN_S >= 0")
        if cfg.vapid_sub and not valid_vapid_sub(cfg.vapid_sub):
            raise ValueError("FNAV_RELAY_VAPID_SUB must be mailto:<address> or https://<host>")
        return cfg
