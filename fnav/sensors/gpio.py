"""Кнопка SAFETY_SWITCH, её LED и пищалка на gpiozero (+ lgpio на Pi). Импорт gpiozero — ленивый.

Полярность кнопки при button_active_low=None определяется по состоянию покоя:
  * читаем вход с подтяжкой вверх и вниз; если уровень не меняется — линию держит модуль, «покой» = этот
    уровень, нажатие — противоположный;
  * если уровень следует за подтяжкой (линия «висит») — считаем как у Pixhawk: кнопка замыкает на 3.3 В,
    включаем подтяжку вниз, активный уровень — высокий.
LED при led_active_low=None — активный низкий (как nSAFETY_LED у Pixhawk: анод на VDD_3V3 модуля).

Колбэки кнопки вызываются в event loop через loop.call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fnav.models import GpioSettings

LED_BLINK = {"blink_slow": (1.0, 1.0), "blink_fast": (0.15, 0.15)}
BEEPS = {"short": (0.08, 0.1, 1), "long": (0.5, 0.1, 1), "double": (0.08, 0.12, 2)}


@dataclass
class IdleProbe:
    driven: bool  # линию держит внешняя схема
    level: int  # уровень покоя (при driven) или с подтяжкой вниз
    active_low: bool  # итоговое решение


def decide_polarity(level_pull_up: int, level_pull_down: int) -> IdleProbe:
    """Чистая логика автоопределения (тестируется без железа)."""
    if level_pull_up == level_pull_down:
        return IdleProbe(driven=True, level=level_pull_up, active_low=bool(level_pull_up))
    return IdleProbe(driven=False, level=level_pull_down, active_low=False)


def probe_idle_level(pin: int, settle_s: float = 0.02) -> IdleProbe:
    """Блокирующее: прочитать уровень покоя с подтяжками вверх/вниз (кнопка в этот момент не нажата)."""
    from gpiozero import InputDevice

    levels = []
    for pull_up in (True, False):
        dev = InputDevice(pin, pull_up=pull_up)
        try:
            time.sleep(settle_s)
            samples = []
            for _ in range(5):
                samples.append(int(dev.pin.state))
                time.sleep(0.004)
            levels.append(1 if sum(samples) >= 3 else 0)
        finally:
            dev.close()
    return decide_polarity(levels[0], levels[1])


class GpioController:
    def __init__(self, loop: asyncio.AbstractEventLoop | None, on_button: Callable[[str], None]):
        self.loop = loop
        self.on_button = on_button
        self.button = None
        self.led = None
        self.buzzer = None
        self.button_active_low: bool | None = None
        self.button_probe: IdleProbe | None = None
        self.led_mode = "off"
        self.clock = time.monotonic
        self._held = False
        self._down = False
        self._press_t = 0.0
        self._last_release = -1e9
        self._last_emit = -1e9
        self._lock = threading.Lock()

    # ── настройка (блокирующая, вызывать из потока) ──
    def setup(self, cfg: GpioSettings) -> list[str]:
        """Вернуть список проблем (пустой = всё хорошо)."""
        self.close()
        errors: list[str] = []
        try:
            from gpiozero import LED, Button, Buzzer
        except ImportError:
            return ["GPIO: python module gpiozero is not installed"]
        with self._lock:
            if cfg.button_gpio is not None:
                try:
                    active_low = cfg.button_active_low
                    if active_low is None:
                        self.button_probe = probe_idle_level(cfg.button_gpio)
                        active_low = self.button_probe.active_low
                        pull = None if self.button_probe.driven else False
                    else:
                        pull = bool(active_low)
                    self.button_active_low = active_low
                    if pull is None:
                        btn = Button(cfg.button_gpio, pull_up=None, active_state=not active_low,
                                     bounce_time=0.08, hold_time=cfg.long_press_s)
                    else:
                        btn = Button(cfg.button_gpio, pull_up=pull, bounce_time=0.08, hold_time=cfg.long_press_s)
                    btn.when_pressed = self._pressed
                    btn.when_held = self._held_cb
                    btn.when_released = self._released
                    self.button = btn
                except Exception as e:  # noqa: BLE001
                    errors.append(f"GPIO: button on GPIO{cfg.button_gpio} unavailable ({e})")
            if cfg.led_gpio is not None:
                try:
                    active_low = True if cfg.led_active_low is None else cfg.led_active_low
                    self.led = LED(cfg.led_gpio, active_high=not active_low)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"GPIO: LED on GPIO{cfg.led_gpio} unavailable ({e})")
            if cfg.buzzer_gpio is not None:
                try:
                    self.buzzer = Buzzer(cfg.buzzer_gpio)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"GPIO: buzzer on GPIO{cfg.buzzer_gpio} unavailable ({e})")
        self.set_led(self.led_mode)
        return errors

    def close(self) -> None:
        with self._lock:
            for dev in (self.button, self.led, self.buzzer):
                if dev is not None:
                    with contextlib.suppress(Exception):
                        dev.close()
            self.button = self.led = self.buzzer = None

    # ── кнопка (вызывается из потока gpiozero) ──
    def _emit(self, ev: str) -> None:
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.on_button, ev)
        else:
            self.on_button(ev)

    # Антидребезг поверх bounce_time gpiozero (на lgpio одно нажатие давало несколько событий):
    #  * фронты, пришедшие раньше RELEASE_SETTLE_S после отпускания, — продолжение того же нажатия;
    #  * нажатие короче MIN_PRESS_S (от первого фронта) — помеха;
    #  * события чаще REPEAT_GUARD_S — повтор;
    #  * после long_press отпускание не даёт press.
    MIN_PRESS_S = 0.03
    RELEASE_SETTLE_S = 0.25
    REPEAT_GUARD_S = 1.0

    def _pressed(self) -> None:
        now = self.clock()
        if self._down:
            return
        self._down = True
        if now - self._last_release >= self.RELEASE_SETTLE_S:  # новое нажатие, а не дребезг
            self._press_t = now
            self._held = False

    def _held_cb(self) -> None:
        if self._held:
            return
        self._held = True
        now = self.clock()
        if now - self._last_emit >= self.REPEAT_GUARD_S:
            self._last_emit = now
            self._emit("long_press")

    def _released(self) -> None:
        now = self.clock()
        was_down, self._down = self._down, False
        self._last_release = now
        if not was_down or self._held:
            return
        if now - self._press_t < self.MIN_PRESS_S or now - self._last_emit < self.REPEAT_GUARD_S:
            return
        self._last_emit = now
        self._emit("press")

    # ── LED / пищалка ──
    def set_led(self, mode: str) -> None:
        self.led_mode = mode
        led = self.led
        if led is None:
            return
        with contextlib.suppress(Exception):
            if mode == "on":
                led.on()
            elif mode in LED_BLINK:
                on_t, off_t = LED_BLINK[mode]
                led.blink(on_time=on_t, off_time=off_t, background=True)
            else:
                led.off()

    def beep(self, pattern: str = "short") -> None:
        bz = self.buzzer
        if bz is None:
            return
        on_t, off_t, n = BEEPS.get(pattern, BEEPS["short"])
        with contextlib.suppress(Exception):
            bz.beep(on_time=on_t, off_time=off_t, n=n, background=True)
