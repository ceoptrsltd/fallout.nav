"""Интерфейсы между частями fallout.nav — КОНТРАКТ (владелец: ведущий, субагенты не правят).

SensorHub реализуют:
  * fnav.sensors.hub.RealSensorHub  — агент F (настоящие gpsd + IST8310 + GPIO);
  * fnav.mock.sensors.MockSensorHub — ведущий (синтетический маршрут для разработки на ПК).

fnav-core (агент A) работает только через этот интерфейс и создаёт хаб так:

    from fnav.interfaces import create_sensor_hub
    hub = create_sensor_hub(settings, mock=args.mock)
"""

from __future__ import annotations

from typing import Callable, Literal, Protocol, runtime_checkable

from fnav.models import CalibrationStatus, CompassCalibration, SensorSnapshot, Settings

ButtonEvent = Literal["press", "long_press"]
LedMode = Literal["off", "on", "blink_slow", "blink_fast"]


@runtime_checkable
class SensorHub(Protocol):
    async def start(self) -> None:
        """Запустить фоновые задачи (чтение gpsd, опрос компаса, GPIO). Не должно падать,
        если устройства нет — ошибки попадают в SensorSnapshot.errors, хаб продолжает пытаться."""

    async def stop(self) -> None: ...

    def snapshot(self) -> SensorSnapshot:
        """Последнее состояние. Дёшево, вызывается 10+ раз в секунду."""

    def apply_settings(self, settings: Settings) -> None:
        """Применить новые настройки на лету (калибровка, поправка установки, пороги курса, GPIO)."""

    # ── GPS ──
    def nmea_tail(self, n: int = 50) -> list[str]:
        """Последние n строк NMEA (для раздела «GPS»)."""

    async def gps_configure(self) -> dict:
        """Перенастроить M8N по UBX согласно settings.gps. Вернуть {"ok": bool, "details": ...}."""

    # ── Калибровка компаса ──
    def calibration_start(self) -> None: ...

    def calibration_status(self) -> CalibrationStatus: ...

    def calibration_finish(self) -> CompassCalibration:
        """Подогнать калибровку по собранным точкам и вернуть её. Сохраняет в настройки fnav-core."""

    def calibration_cancel(self) -> None: ...

    # ── Автокалибровка по GPS-курсу во время езды ──
    def on_calibration_update(self, callback: Callable[[CompassCalibration], None]) -> None:
        """Хаб вызывает callback (в event loop), когда автокалибровка улучшилась и её пора сохранить
        (не чаще settings.compass.auto_save_interval_s). fnav-core сохраняет её в settings.compass.calibration."""

    def auto_calibration_reset(self) -> None:
        """Сбросить накопленные точки автокалибровки и начать сбор заново (сохранённая калибровка не трогается)."""

    # ── Кнопка / LED / пищалка на GPS-модуле ──
    def on_button(self, callback: Callable[[ButtonEvent], None]) -> None:
        """Зарегистрировать обработчик нажатия (вызывается из event loop)."""

    def set_led(self, mode: LedMode) -> None: ...

    def beep(self, pattern: str = "short") -> None:
        """'short' | 'long' | 'double'. Если пищалки нет — ничего не делать."""


def create_sensor_hub(settings: Settings, mock: bool = False) -> SensorHub:
    if mock:
        from fnav.mock.sensors import MockSensorHub

        return MockSensorHub(settings)
    from fnav.sensors.hub import RealSensorHub  # реализует агент F

    return RealSensorHub(settings)
