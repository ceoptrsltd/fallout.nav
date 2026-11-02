"""Аварийка через RC-реле (docs/contracts/alarm.md, «Аварийка»): продление, подтверждения, аппаратный ШИМ, API."""

from __future__ import annotations

import asyncio
import time

import pytest
from core_helpers import login, running_app

from fnav.core.alarm import AlarmService, Detection, HazardLights
from fnav.core.imu import ImuService
from fnav.core.tracking import RelayConfig
from fnav.models import AlarmSettings, GpsFix, HazardSettings
from fnav.sensors.hazard import FakeHazard


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


async def test_hold_flash_is_extended_then_released():
    cfg = HazardSettings(enabled=True)
    outs = Outputs()
    hz = HazardLights(lambda: cfg, outs)
    await hz.apply(cfg)  # включена — вывод открыт сразу (у RC-реле пошли импульсы «выкл»)
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


async def test_test_works_disabled_and_reports_errors():
    cfg = HazardSettings(enabled=False)
    outs = Outputs()
    hz = HazardLights(lambda: cfg, outs)
    await hz.apply(cfg)
    assert outs.made == []  # выключена — вывод не держим
    assert await hz.test() is None
    await until(lambda: outs.last.on)
    await hz.stop()
    assert outs.last.closed  # после теста выключенную — отпустили
    bad = HazardLights(lambda: cfg, Outputs(fail="GPIO19 is busy"))
    # выключенная: причина — в ответе проверки, а в статусе она не «сломана» (ею не пользуются)
    assert await bad.test() == "GPIO19 is busy" and bad.status().error is None
    on = HazardSettings(enabled=True)
    bad_on = HazardLights(lambda: on, Outputs(fail="GPIO19 is busy"))
    assert await bad_on.test() == "GPIO19 is busy" and bad_on.status().error == "GPIO19 is busy"


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


async def test_disabled_hazard_lights_leave_the_pwm_alone(tmp_path):
    """Аварийка выключена, dtoverlay=pwm нет: disarm / silence / тревога вывод не открывают — нет вечной ошибки
    «Hazard lights error» на главной (раньше stop() открывал ШИМ и оставлял error навсегда)."""
    calls: list[HazardSettings] = []

    def no_pwm(cfg: HazardSettings):
        calls.append(cfg)
        raise RuntimeError("hardware PWM is off: add dtoverlay=pwm,pin=19,func=2 to config.txt and reboot")

    cfg = AlarmSettings(siren=False, arm_delay_s=0, hazard=HazardSettings(enabled=False))
    imu = ImuService(lambda: cfg, open_source=lambda b, a: None)
    svc = AlarmService(tmp_path, lambda: cfg, imu, RelayConfig(), get_fix=GpsFix, open_hazard=no_pwm)
    await svc.start(background=False)
    await svc.silence()  # выключенная охрана: «Stop» — тоже без ШИМ
    await svc.disarm()
    await svc.arm()
    await svc._fire(Detection("impact", {"peak_g": 0.5}))
    await svc.silence()
    await svc.disarm()
    await svc.stop()
    assert calls == [] and svc.status().hazard.error is None


async def test_hazard_error_clears_after_a_good_switch():
    cfg = HazardSettings(enabled=True)
    outs = Outputs(fail="permission denied")
    hz = HazardLights(lambda: cfg, outs)
    await hz.apply(cfg)
    assert hz.status().error == "permission denied"
    outs.fail = None  # права появились (udev), а настройки не менялись
    hz.flash(0.2)
    await until(lambda: outs.made and outs.last.on)
    assert hz.status().error is None
    outs.last.set = lambda on: (_ for _ in ()).throw(OSError(19, "No such device"))  # sysfs ШИМ пропал
    await until(lambda: not hz.active)
    assert "No such device" in hz.status().error and outs.last.closed
    hz.flash(0.1)  # снова открылся и переключился — ошибки нет
    await until(lambda: len(outs.made) == 2 and outs.last.on)
    assert hz.status().error is None
    await hz.close()


async def test_hazard_output_opens_and_closes_off_the_event_loop():
    """Открытие (ожидание прав от udev) и закрытие (импульсы «выкл» ~0,1 с) ШИМ — в потоке, не в event loop."""
    import threading

    loop_thread = threading.get_ident()
    seen: list[tuple[str, int]] = []

    class SlowPwm(FakeHazard):
        def __init__(self, cfg: HazardSettings) -> None:
            seen.append(("open", threading.get_ident()))
            super().__init__(cfg)

        def close(self) -> None:
            seen.append(("close", threading.get_ident()))
            time.sleep(0.05)
            super().close()

    cfg = HazardSettings(enabled=False)
    hz = HazardLights(lambda: cfg, SlowPwm)
    assert await hz.test() is None
    await hz.stop()  # тест выключенной — вывод закрывается сразу после
    assert [k for k, _ in seen] == ["open", "close"] and all(t != loop_thread for _, t in seen)


async def test_hazard_test_api(data_dir):
    async with running_app() as c:
        await login(c)
        r = await c.post("/api/alarm/hazard/test")
        assert r.status_code == 200 and r.json()["hazard"]["active"] is True
        assert r.json()["hazard"]["enabled"] is False
        core = c.app.state.core
        core.alarm.hazard.open_output = Outputs(fail="GPIO19 is busy")
        await core.alarm.hazard.stop()
        await core.alarm.hazard._close_output()
        r = await c.post("/api/alarm/hazard/test")
        assert r.status_code == 503 and r.json()["detail"] == "Hazard relay: GPIO19 is busy"


def fake_pwmchip(tmp_path, exported: bool = False):
    chip = tmp_path / "pwmchip0"
    chip.mkdir()
    (chip / "npwm").write_text("2\n")
    (chip / "export").write_text("")
    ch = chip / "pwm1"
    ch.mkdir()  # в sysfs его создаёт запись в export; здесь — заранее
    for f in ("enable", "duty_cycle", "period"):
        (ch / f).write_text("0")
    return chip, ch


def test_hardware_pwm_steady_servo_pulses(tmp_path):
    from fnav.sensors.hazard import HardwarePwmHazard, find_pwmchip

    chip, ch = fake_pwmchip(tmp_path)
    assert find_pwmchip(tmp_path) == chip
    out = HardwarePwmHazard(HazardSettings(on_us=2000, off_us=1000), root=tmp_path, sleep=lambda s: None)
    assert (chip / "export").read_text() == ""  # канал уже был
    assert ch.joinpath("period").read_text() == "20000000" and ch.joinpath("duty_cycle").read_text() == "1000000"
    assert ch.joinpath("enable").read_text() == "1"
    out.set(True)
    assert ch.joinpath("duty_cycle").read_text() == "2000000"
    out.close()
    assert ch.joinpath("duty_cycle").read_text() == "1000000" and ch.joinpath("enable").read_text() == "0"


def test_hardware_pwm_needs_the_overlay(tmp_path):
    from fnav.sensors.hazard import HardwarePwmHazard

    with pytest.raises(RuntimeError, match="dtoverlay=pwm,pin=19,func=2"):
        HardwarePwmHazard(HazardSettings(), root=tmp_path)
