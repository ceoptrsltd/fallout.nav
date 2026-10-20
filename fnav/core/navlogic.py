"""Логика экрана: «скорость → карта/камера и зум» (единый источник правды для экрана и превью).

Чистый класс без ввода-вывода: время передаётся явно (`now`, монотонные секунды).

Правила (docs/contracts/api.md, «Экран»):
  * ступень зума — строка `nav.zoom_table` с наибольшим `min_mps` ≤ скорости;
    новая ступень применяется, если держится `zoom_delay_s` (первая — сразу);
  * карта → камера, если скорость < `camera_below_mps` держится `switch_delay_s`;
    камера → карта, если скорость > `map_above_mps` держится `switch_delay_s`;
  * нет фикса → `nav.no_fix_mode` сразу (`reason="no_fix"`; до первого фикса в первые
    `startup_grace_s` секунд — `reason="startup"`); после появления фикса режим выбирается
    по скорости сразу, дальше — с гистерезисом;
  * `display.force_mode` ≠ auto → этот режим, `reason="forced"` (авто-логика продолжает
    считаться в фоне, чтобы при возврате в auto не было скачка).
"""

from __future__ import annotations

from fnav.models import DisplaySettings, DisplayState, GpsFix, NavSettings

STALE_FIX_S = 5.0  # GPS-данные старше — считаем, что фикса нет
DEFAULT_ZOOM = 17.0


def has_fix(gps: GpsFix) -> bool:
    if gps.fix not in ("2d", "3d") or gps.lat is None or gps.lon is None:
        return False
    return gps.age_s is None or gps.age_s <= STALE_FIX_S


def zoom_for_speed(nav: NavSettings, speed: float) -> float:
    table = sorted(nav.zoom_table, key=lambda s: s.min_mps)
    if not table:
        return DEFAULT_ZOOM
    zoom = table[0].zoom  # скорость ниже всех порогов → самая «медленная» ступень
    for step in table:
        if step.min_mps <= speed:
            zoom = step.zoom
        else:
            break
    return float(zoom)


class NavLogic:
    def __init__(
        self,
        nav: NavSettings,
        display: DisplaySettings,
        *,
        start_time: float = 0.0,
        startup_grace_s: float = 5.0,
    ):
        self.nav = nav
        self.display = display
        self.startup_grace_s = startup_grace_s
        self._t0 = start_time
        self._mode: str = nav.no_fix_mode
        self._reason: str = "startup"
        self._had_fix = False
        self._in_speed_regime = False  # режим уже выбирается по скорости (фикс есть)
        self._switch_since: float | None = None  # когда началось условие переключения
        self._zoom: float | None = None
        self._zoom_cand: float | None = None
        self._zoom_since: float = 0.0
        self._last_now: float | None = None

    def apply_settings(self, nav: NavSettings, display: DisplaySettings) -> None:
        self.nav = nav
        self.display = display

    @property
    def target_zoom(self) -> float:
        return self._zoom if self._zoom is not None else zoom_for_speed(self.nav, 0.0)

    def update(self, now: float, gps: GpsFix) -> DisplayState:
        if self._last_now is not None and now < self._last_now:
            # время пошло назад — сбрасываем выдержки
            self._switch_since = None
            self._zoom_cand = None
        self._last_now = now

        if has_fix(gps):
            speed = max(0.0, gps.speed_mps or 0.0)
            self._had_fix = True
            self._update_zoom(now, speed)
            self._update_mode(now, speed)
        else:
            self._in_speed_regime = False
            self._switch_since = None
            self._zoom_cand = None
            self._mode = self.nav.no_fix_mode
            in_grace = not self._had_fix and (now - self._t0) < self.startup_grace_s
            self._reason = "startup" if in_grace else "no_fix"

        mode, reason = self._mode, self._reason
        if self.display.force_mode != "auto":
            mode, reason = self.display.force_mode, "forced"
        return DisplayState(
            mode=mode,
            reason=reason,
            target_zoom=self.target_zoom,
            orientation=self.display.orientation,
        )

    # ── внутреннее ──
    def _update_zoom(self, now: float, speed: float) -> None:
        z = zoom_for_speed(self.nav, speed)
        if self._zoom is None:
            self._zoom = z
            self._zoom_cand = None
            return
        if z == self._zoom:
            self._zoom_cand = None
            return
        if self._zoom_cand != z:
            self._zoom_cand = z
            self._zoom_since = now
        if now - self._zoom_since >= self.nav.zoom_delay_s:
            self._zoom = z
            self._zoom_cand = None

    def _update_mode(self, now: float, speed: float) -> None:
        nav = self.nav
        self._reason = "speed"
        if not self._in_speed_regime:
            # только что появился фикс: выбираем режим по скорости сразу
            self._in_speed_regime = True
            self._switch_since = None
            if speed > nav.map_above_mps:
                self._mode = "map"
            elif speed < nav.camera_below_mps:
                self._mode = "camera"
            # между порогами — оставляем текущий
            return
        if self._mode == "map":
            want = speed < nav.camera_below_mps
            target = "camera"
        else:
            want = speed > nav.map_above_mps
            target = "map"
        if not want:
            self._switch_since = None
            return
        if self._switch_since is None:
            self._switch_since = now
        if now - self._switch_since >= nav.switch_delay_s:
            self._mode = target
            self._switch_since = None
