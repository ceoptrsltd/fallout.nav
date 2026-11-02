"""Часовой пояс устройства следует за машиной.

Имена записей, время в push («Hit 0.42 g at 14:03») и часы на экране — по местному времени системы. Пояс ставится
при установке (FNAV_TIMEZONE), а машина ездит по Европе: после границы всё это расходилось бы с местными часами.
Open-Meteo (fnav/core/weather.py, timezone=auto) в каждом прогнозе называет пояс точки (IANA, «Europe/Lisbon»).
Если свежий прогноз для живой позиции GPS два раза подряд называет пояс, отличный от системного (у границы пояс
не «дребезжит»), fnav-core ставит его: `timedatectl set-timezone` (polkit: pi/polkit/50-fnav-timedate.rules) и
time.tzset() у себя. Камеры замечают смену сами (fnav/tz.py ZoneWatch в обслуживании), часы экрана — по
SystemStatus.utc_offset_min. Выключается настройкой settings.system.auto_timezone. В --mock пояс не трогается.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fnav import tz
from fnav.core.system import run

log = logging.getLogger("fnav.core.timezone")

CONFIRM = 2  # столько прогнозов подряд с новым поясом — и только тогда смена


def located_zone(name: object) -> bool:
    """Пояс по месту, а не «Etc/GMT+1», «GMT», «UTC» — такие Open-Meteo отдаёт для точек в море (паром)."""
    return tz.valid_zone(name) and not str(name).startswith("Etc/") and name not in ("GMT", "UTC", "UCT")


async def set_system_zone(zone: str) -> str | None:
    """`timedatectl set-timezone` (polkit разрешает fnav только это). None — получилось, иначе текст ошибки."""
    rc, out, err = await run("timedatectl", "--no-ask-password", "set-timezone", zone, timeout=15)
    return None if rc == 0 else ((err or out).strip() or f"exit code {rc}")


class TimeZoneFollower:
    def __init__(self, enabled: Callable[[], bool], *, mock: bool = False,
                 current: Callable[[], str | None] = tz.zone_name,
                 setter: Callable[[str], Awaitable[str | None]] = set_system_zone) -> None:
        self.enabled = enabled
        self.mock = mock
        self.current = current
        self.setter = setter
        self.candidate: str | None = None  # пояс, который прогнозы называют, но ещё не поставлен
        self.seen = 0  # сколько прогнозов подряд его назвали

    async def observe(self, zone: str | None) -> bool:
        """Пояс из свежего прогноза для живой позиции GPS (WeatherService.on_zone). True — пояс системы сменён."""
        if not located_zone(zone):
            return False
        cur = self.current()
        if zone == cur or not self.enabled():
            self.candidate, self.seen = None, 0
            return False
        if zone != self.candidate:
            self.candidate, self.seen = zone, 0
        self.seen += 1
        if self.seen < CONFIRM:
            log.info("time zone: the forecast says %s here (system: %s), waiting for the next one", zone, cur)
            return False
        self.candidate, self.seen = None, 0
        if self.mock:
            log.info("time zone: would switch %s -> %s (mock: the system zone is left alone)", cur, zone)
            return False
        err = await self.setter(zone)
        if err:
            log.warning("time zone: cannot switch %s -> %s: %s", cur, zone, err)
            return False
        tz.tzset()
        log.info("time zone: %s -> %s (the van's position)", cur, zone)
        return True
