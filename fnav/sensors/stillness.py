"""Фильтр около-нулевой скорости: на стоянке при плохом GPS не показывать, что мы едем.

Гистерезис по времени и скорости: из «стоим» в «едем» — только если скорость выше порога держится
moving_confirm_s подряд; из «едем» в «стоим» — сразу, как скорость ниже stationary_below_mps.
При плохой геометрии спутников (HDOP > 3) пороги удваиваются.
"""

from __future__ import annotations

from fnav.models import GpsFix, GpsSettings

POOR_HDOP = 3.0


class StillnessFilter:
    def __init__(self) -> None:
        self.moving = False
        self._above_since: float | None = None

    def reset(self) -> None:
        self.moving = False
        self._above_since = None

    def apply(self, now: float, fix: GpsFix, cfg: GpsSettings) -> GpsFix:
        """Вернуть копию fix с отфильтрованной скоростью (0 на стоянке) и курсом (None на стоянке)."""
        if fix.fix == "none" or fix.speed_mps is None:
            self.reset()
            return fix
        k = 2.0 if (fix.hdop is not None and fix.hdop > POOR_HDOP) else 1.0
        stop_below = cfg.stationary_below_mps * k
        go_above = max(cfg.moving_above_mps, cfg.stationary_below_mps) * k
        v = fix.speed_mps
        if self.moving:
            if v < stop_below:
                self.moving = False
                self._above_since = None
        else:
            if v > go_above:
                if self._above_since is None:
                    self._above_since = now
                if now - self._above_since >= cfg.moving_confirm_s:
                    self.moving = True
            else:
                self._above_since = None
        if self.moving:
            return fix
        return fix.model_copy(update={"speed_mps": 0.0, "course_deg": None})
