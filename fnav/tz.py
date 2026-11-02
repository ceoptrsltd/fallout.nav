"""Системный часовой пояс: имя IANA и смена пояса в уже запущенных процессах.

Пояс устройства меняется в дороге (fnav/core/timezone.py: `timedatectl set-timezone` по позиции машины). Процесс
читает /etc/localtime один раз: без time.tzset() localtime() и datetime.now() в работающих fnav-core и fnav-camera
остались бы в старом поясе (имена записей, время в push, часы экрана). ZoneWatch.check() — дешёвый stat, его зовут
периодически (обслуживание камеры, статус системы ядра): файл сменился — tzset().
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("fnav.tz")

LOCALTIME = Path("/etc/localtime")
_ZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}")


def valid_zone(name: object) -> bool:
    """Похоже на имя IANA («Europe/Warsaw», «America/Argentina/Salta», «UTC»): без путей и мусора."""
    return isinstance(name, str) and len(name) <= 64 and _ZONE_RE.fullmatch(name) is not None


def zone_name(path: Path = LOCALTIME) -> str | None:
    """IANA-имя пояса системы: по ссылке /etc/localtime (…/zoneinfo/Europe/Warsaw — так её ставит timedatectl),
    иначе из /etc/timezone рядом; не известно (Windows, контейнер) — None."""
    try:
        target = os.readlink(path)
    except (OSError, ValueError):
        target = ""
    i = target.find("zoneinfo/")
    if i >= 0:
        name = target[i + len("zoneinfo/"):]
        for pre in ("posix/", "right/"):  # те же пояса в других вариантах базы
            name = name.removeprefix(pre)
        if valid_zone(name):
            return name
    try:
        name = path.with_name("timezone").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return name if valid_zone(name) else None


def tzset() -> None:
    """Перечитать пояс системы в этом процессе (только Unix)."""
    if hasattr(time, "tzset"):
        time.tzset()


class ZoneWatch:
    """Смена системного пояса → time.tzset() в этом процессе. check() — stat файла, звать раз в несколько секунд."""

    def __init__(self, path: Path = LOCALTIME) -> None:
        self.path = path
        self._sig = self._signature()

    def _signature(self) -> tuple | None:
        try:
            st = os.stat(self.path)  # по ссылке: у каждого пояса свой файл
            link = os.readlink(self.path) if os.path.islink(self.path) else ""
        except (OSError, ValueError):
            return None
        return link, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns

    def check(self) -> bool:
        """True — пояс сменился (tzset уже вызван)."""
        sig = self._signature()
        if sig == self._sig:
            return False
        self._sig = sig
        tzset()
        log.info("system time zone changed: %s (UTC%s)", zone_name(self.path) or "?", time.strftime("%z"))
        return True
