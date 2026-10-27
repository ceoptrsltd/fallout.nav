"""Реле аварийки на GPIO: RC-реле (импульсы 50 Гц) или обычный вход.

docs/contracts/alarm.md, «Аварийка». Здесь только вывод «вкл/выкл»; когда и сколько мигать — fnav/core/alarm.py.
RC-реле на GPIO18/19 — от аппаратного ШИМ (sysfs, dtoverlay=pwm,pin=19,func=2): импульсы ровные при любой загрузке
CPU. Программный ШИМ (gpiozero + lgpio) при загруженном CPU теряет импульсы, и RC-реле «дребезжит» — он только для
других выводов. Обычный вход — gpiozero (импорт ленивый).
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from fnav.models import HazardSettings

SERVO_HZ = 50
BUSY_PINS = {2: "I2C SDA (compass)", 3: "I2C SCL (compass)", 4: "I2C-3 SDA (MPU-6050)", 5: "I2C-3 SCL (MPU-6050)",
             12: "GPS UART", 13: "GPS UART"}
PWM_SYSFS = Path("/sys/class/pwm")
HW_PWM_CHANNEL = {18: 0, 19: 1}  # канал ШИМ BCM2711 на этом выводе (GPIO12/13 заняты GPS)


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


def find_pwmchip(root: Path = PWM_SYSFS) -> Path | None:
    """Контроллер ШИМ с двумя каналами (BCM2711 fe20c000.pwm — его включает dtoverlay=pwm)."""
    for chip in sorted(root.glob("pwmchip*")):
        with contextlib.suppress(OSError, ValueError):
            if int((chip / "npwm").read_text().strip()) >= 2:
                return chip
    return None


class HardwarePwmHazard:
    """RC-реле от аппаратного ШИМ через sysfs: период 20 мс, ширина импульса on_us / off_us.
    Права на файлы — группе gpio (udev-правило Raspberry Pi OS 99-com.rules); fnav-core в этой группе."""

    def __init__(self, cfg: HazardSettings, root: Path = PWM_SYSFS,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if cfg.gpio not in HW_PWM_CHANNEL:
            raise RuntimeError(f"GPIO{cfg.gpio} has no hardware PWM (use GPIO19, pin 35)")
        chip = find_pwmchip(root)
        if chip is None:
            raise RuntimeError(f"hardware PWM is off: add dtoverlay=pwm,pin={cfg.gpio},func=2 to config.txt "
                               "and reboot")
        ch = HW_PWM_CHANNEL[cfg.gpio]
        self.cfg = cfg
        self.dir = chip / f"pwm{ch}"
        if not self.dir.exists():
            (chip / "export").write_text(str(ch))
        for _ in range(100):  # канал появляется сразу, права группе gpio udev выдаёт чуть позже
            if os.access(self.dir / "duty_cycle", os.W_OK):
                break
            sleep(0.02)
        self._write("enable", "0")
        self._write("duty_cycle", "0")  # сначала ширина ≤ периода, иначе ядро не примет период
        self._write("period", str(1_000_000_000 // SERVO_HZ))
        self._write("duty_cycle", str(cfg.off_us * 1000))
        self._write("enable", "1")

    def _write(self, name: str, value: str) -> None:
        (self.dir / name).write_text(value)

    def set(self, on: bool) -> None:
        self._write("duty_cycle", str((self.cfg.on_us if on else self.cfg.off_us) * 1000))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.set(False)
            time.sleep(0.1)  # несколько импульсов «выкл», прежде чем сигнал пропадёт
            self._write("enable", "0")


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
    """RC-реле на выводе с аппаратным ШИМ — только аппаратный (программный дребезжит); иначе — gpiozero."""
    if cfg.signal == "servo" and cfg.gpio in HW_PWM_CHANNEL:
        try:
            return HardwarePwmHazard(cfg)
        except OSError as e:
            raise RuntimeError(f"hardware PWM on GPIO{cfg.gpio}: {e}") from e
    return GpioHazard(cfg)
