"""Реле аварийки на GPIO (gpiozero + lgpio на Pi, импорт ленивый): RC-реле (импульсы 50 Гц) или обычный вход.

docs/contracts/alarm.md, «Аварийка». Здесь только вывод «вкл/выкл»; когда и сколько мигать — fnav/core/alarm.py.
"""

from __future__ import annotations

import contextlib
import time
from typing import Protocol

from fnav.models import HazardSettings

SERVO_HZ = 50
BUSY_PINS = {2: "I2C SDA (compass, MPU-6050)", 3: "I2C SCL (compass, MPU-6050)", 12: "GPS UART", 13: "GPS UART"}


def servo_duty(us: int, hz: int = SERVO_HZ) -> float:
    """Ширина импульса, мкс → доля периода (1000 мкс при 50 Гц → 0.05)."""
    return us * hz / 1_000_000


def output_key(cfg: HazardSettings) -> tuple:
    """Параметры, при смене которых вывод надо открыть заново."""
    return (cfg.gpio, cfg.signal, cfg.on_us, cfg.off_us, cfg.active_low)


class HazardOutput(Protocol):
    def set(self, on: bool) -> None: ...
    def close(self) -> None: ...


class GpioHazard:
    """Настоящий вывод. servo: импульсы идут всё время (вкл/выкл), level: постоянный уровень."""

    def __init__(self, cfg: HazardSettings) -> None:
        if cfg.gpio in BUSY_PINS:
            raise RuntimeError(f"GPIO{cfg.gpio} is taken: {BUSY_PINS[cfg.gpio]}")
        try:
            from gpiozero import DigitalOutputDevice, PWMOutputDevice
        except ImportError:
            raise RuntimeError("python module gpiozero is not installed") from None
        self.cfg = cfg
        if cfg.signal == "servo":
            self.dev = PWMOutputDevice(cfg.gpio, frequency=SERVO_HZ, initial_value=servo_duty(cfg.off_us))
        else:
            self.dev = DigitalOutputDevice(cfg.gpio, active_high=not cfg.active_low, initial_value=False)

    def set(self, on: bool) -> None:
        if self.cfg.signal == "servo":
            self.dev.value = servo_duty(self.cfg.on_us if on else self.cfg.off_us)
        else:
            self.dev.value = 1 if on else 0

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.set(False)
            if self.cfg.signal == "servo":
                time.sleep(0.1)  # несколько импульсов «выкл», прежде чем сигнал пропадёт
        with contextlib.suppress(Exception):
            self.dev.close()


class FakeHazard:
    """--mock и тесты: только состояние и история переключений."""

    def __init__(self, cfg: HazardSettings) -> None:
        self.cfg = cfg
        self.on = False
        self.history: list[bool] = []
        self.closed = False

    def set(self, on: bool) -> None:
        self.on = on
        self.history.append(on)

    def close(self) -> None:
        self.on = False
        self.closed = True


def open_gpio_hazard(cfg: HazardSettings) -> HazardOutput:
    return GpioHazard(cfg)
