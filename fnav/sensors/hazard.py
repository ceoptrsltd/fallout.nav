"""Реле аварийки: RC-реле на GPIO19 (pin 35) от аппаратного ШИМ — docs/contracts/alarm.md, «Аварийка».

Здесь только вывод «вкл/выкл»; когда и сколько мигать — fnav/core/alarm.py. Импульсы 50 Гц (on_us / off_us) делает
блок ШИМ (dtoverlay=pwm,pin=19,func=2, sysfs /sys/class/pwm): ровные при любой загрузке CPU — программный ШИМ при
загруженном CPU теряет импульсы, и RC-реле «дребезжит».
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
GPIO = 19  # pin 35
PWM_CHANNEL = 1  # GPIO19 = PWM0_1 (ALT5)
PWM_SYSFS = Path("/sys/class/pwm")


def output_key(cfg: HazardSettings) -> tuple:
    """Параметры, при смене которых вывод надо открыть заново."""
    return (cfg.on_us, cfg.off_us)


class HazardOutput(Protocol):
    """Открытие и close() могут ждать (права udev, импульсы «выкл» перед снятием сигнала) — их зовут из потока;
    set() — быстрая запись, её можно из event loop."""

    def set(self, on: bool) -> None: ...
    def close(self) -> None: ...


def find_pwmchip(root: Path = PWM_SYSFS) -> Path | None:
    """Контроллер ШИМ с двумя каналами (BCM2711 fe20c000.pwm — его включает dtoverlay=pwm)."""
    for chip in sorted(root.glob("pwmchip*")):
        with contextlib.suppress(OSError, ValueError):
            if int((chip / "npwm").read_text().strip()) >= 2:
                return chip
    return None


class HardwarePwmHazard:
    """RC-реле от аппаратного ШИМ через sysfs: период 20 мс, импульс on_us («вкл») / off_us («выкл»).
    Права на файлы — группе gpio (udev-правило Raspberry Pi OS 99-com.rules); fnav-core в этой группе."""

    def __init__(self, cfg: HazardSettings, root: Path = PWM_SYSFS,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        chip = find_pwmchip(root)
        if chip is None:
            raise RuntimeError(f"hardware PWM is off: add dtoverlay=pwm,pin={GPIO},func=2 to config.txt and reboot")
        self.cfg = cfg
        self.dir = chip / f"pwm{PWM_CHANNEL}"
        if not self.dir.exists():
            (chip / "export").write_text(str(PWM_CHANNEL))
        for _ in range(100):  # канал появляется сразу, права группе gpio udev выдаёт чуть позже
            if os.access(self.dir / "duty_cycle", os.W_OK):
                break
            sleep(0.02)
        self._write("enable", "0")
        self._write("duty_cycle", "0")  # сначала ширина ≤ периода, иначе ядро не примет период
        self._write("period", str(1_000_000_000 // SERVO_HZ))
        self.set(False)
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
    try:
        return HardwarePwmHazard(cfg)
    except OSError as e:
        raise RuntimeError(f"hardware PWM on GPIO{GPIO}: {e}") from e
