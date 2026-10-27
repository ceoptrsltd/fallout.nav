"""Аварийка через реле (docs/contracts/alarm.md, «Аварийка»): hold / pulse, продление, подтверждения, API."""

from __future__ import annotations

import asyncio
import time

import pytest
from core_helpers import login, running_app

from fnav.core.alarm import AlarmService, Detection, HazardLights
from fnav.core.imu import ImuService
from fnav.core.tracking import RelayConfig
from fnav.models import AlarmSettings, GpsFix, HazardSettings
from fnav.sensors.hazard import FakeHazard, GpioHazard, servo_duty


class Outputs:
    """Фабрика выводов: запоминает открытые FakeHazard; fail — открыть не получится."""

    def __init__(self, fail: str | None = None) -> None:
        self.made: list[FakeHazard] = []
        self.fail = fail

    def __call__(self, cfg: HazardSettings) -> FakeHazard:
        if self.fail:
            raise RuntimeError(self.fail)
        self.made.append(FakeHazard(cfg))
        return self.made[-1]

    @property
    def last(self) -> FakeHazard:
        return self.made[-1]


async def until(cond, timeout: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timeout"
        await asyncio.sleep(0.01)


def test_servo_duty_and_busy_pins():
    assert servo_duty(1000) == pytest.approx(0.05) and servo_duty(2000) == pytest.approx(0.10)
    with pytest.raises(RuntimeError, match="GPIO12 is taken"):
        GpioHazard(HazardSettings(gpio=12))


async def test_hold_flash_is_extended_then_released():
    cfg = HazardSettings(enabled=True, mode="hold")
    outs = Outputs()
    hz = HazardLights(lambda: cfg, outs)
    hz.apply(cfg)  # включена — вывод открыт сразу (у RC-реле пошли импульсы «выкл»)
    assert len(outs.made) == 1 and outs.last.on is False
    hz.flash(0.3)
    await until(lambda: outs.last.on)
    assert hz.active and hz.status().active
    await asyncio.sleep(0.2)
    hz.flash(0.3)  # новое событие продлевает
    await asyncio.sleep(0.2)
    assert outs.last.on  # 0.4 с с начала: без продления уже погасла бы
    await until(lambda: not hz.active)
    assert outs.last.history == [True, False] and not outs.last.closed


async def test_pulse_mode_presses_on_and_off():
    cfg = HazardSettings(enabled=True, mode="pulse", press_ms=100)
    outs = Outputs()
    hz = HazardLights(lambda: cfg, outs)
    hz.flash(0.3)
    await until(lambda: hz.lit)
    await until(lambda: not hz.active)
    # «нажали» (вкл) и «нажали» ещё раз (выкл): реле каждый раз замыкается на press_ms и отпускается
    assert outs.last.history == [True, False, True, False] and not hz.lit and not outs.last.on


async def test_stop_mid_press_releases_the_button():
    cfg = HazardSettings(enabled=True, mode="pulse", press_ms=1000)
    outs = Outputs()
    hz = HazardLights(lambda: cfg, outs)
    hz.flash(30)
    await until(lambda: outs.made and outs.last.on)  # кнопка «нажата»
    await asyncio.wait_for(hz.stop(), 5)
    assert outs.last.on is False and not hz.active and not hz.lit


async def test_test_works_disabled_and_reports_errors():
    cfg = HazardSettings(enabled=False)
    outs = Outputs()
    hz = HazardLights(lambda: cfg, outs)
    hz.apply(cfg)
    assert outs.made == []  # выключена — вывод не держим
    assert await hz.test() is None
    await until(lambda: outs.last.on)
    await hz.stop()
    assert outs.last.closed  # после теста выключенную — отпустили
    bad = HazardLights(lambda: cfg, Outputs(fail="GPIO19 is busy"))
    assert await bad.test() == "GPIO19 is busy" and bad.status().error == "GPIO19 is busy"


def make_alarm(tmp_path, outs: Outputs, **hazard):
    cfg = AlarmSettings(siren=False, arm_delay_s=0, hazard=HazardSettings(enabled=True, **hazard))
    imu = ImuService(lambda: cfg, open_source=lambda b, a: None)
    return AlarmService(tmp_path, lambda: cfg, imu, RelayConfig(), get_fix=GpsFix, open_hazard=outs)


async def test_alarm_event_flashes_and_silence_stops(tmp_path):
    outs = Outputs()
    svc = make_alarm(tmp_path, outs, seconds=30)
    await svc.start(background=False)
    await svc.test()  # проверка push — без аварийки
    await asyncio.sleep(0.05)
    assert not svc.hazard.active
    await svc.arm()
    await svc._fire(Detection("impact", {"peak_g": 0.5}))
    await until(lambda: outs.last.on)
    assert svc.status().hazard.active and svc.telemetry().hazard
    st = await svc.silence()
    assert outs.last.on is False and not st.hazard.active and st.state == "triggered"
    await svc._fire(Detection("tilt", {"tilt_deg": 4.0}))
    await until(lambda: outs.last.on)
    await svc.disarm()
    assert outs.last.on is False
    await svc.stop()
    assert outs.last.closed


async def test_confirm_blinks_on_arm_and_disarm(tmp_path):
    outs = Outputs()
    svc = make_alarm(tmp_path, outs, confirm=True)
    await svc.start(background=False)
    await svc.arm()
    await until(lambda: not svc.hazard.active and outs.last.history)
    assert outs.last.history == [True, False]
    await svc.disarm()
    await asyncio.sleep(0.05)
    await until(lambda: not svc.hazard.active, timeout=5)
    assert outs.last.history == [True, False, True, False, True, False]
    await svc.stop()


async def test_hazard_test_api(data_dir):
    async with running_app() as c:
        await login(c)
        r = await c.post("/api/alarm/hazard/test")
        assert r.status_code == 200 and r.json()["hazard"]["active"] is True
        assert r.json()["hazard"]["enabled"] is False
        core = c.app.state.core
        core.alarm.hazard.open_output = Outputs(fail="GPIO19 is busy")
        await core.alarm.hazard.stop()
        core.alarm.hazard._close_output()
        r = await c.post("/api/alarm/hazard/test")
        assert r.status_code == 503 and r.json()["detail"] == "Hazard relay: GPIO19 is busy"
