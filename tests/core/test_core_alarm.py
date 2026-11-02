"""Охрана: драйвер MPU-6050 (фальшивая шина I2C), обнаружение удара/наклона/сдвига, состояния и их сохранение,
пауза между push, отправка на fnav-relay, сирена через радио, API и клавиша «Alarm on/off»."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import struct
import time

import httpx
import pytest
from core_helpers import login, make_opts, running_app

from fnav.core import alarm as alarm_mod
from fnav.core import keypad as kp
from fnav.core.alarm import (
    IMPACT_GAP_S,
    AlarmOutbox,
    AlarmService,
    Detection,
    Detector,
    MoveWatch,
    VideoWatch,
    make_event,
    siren_wav,
)
from fnav.core.app import create_app
from fnav.core.imu import ImuService
from fnav.core.level import LevelService
from fnav.core.marks import MarkLog
from fnav.core.radio import RadioDirectory, RadioService, bluealsa_device
from fnav.core.tracking import TOKEN_HEADER, RelayConfig
from fnav.core.vision import VisionClient
from fnav.models import AlarmSettings, GpsFix, HazardSettings, RadioOutput, RadioSettings, RadioStation
from fnav.sensors import mpu6050 as mpu

DT = 1.0 / 200


# ── драйвер ──


class FakeBus:
    """Шина I2C с MPU-6050: регистры, FIFO (байты), флаг переполнения; запоминает записи и блочные чтения."""

    def __init__(self, who: int = 0x68, present: bool = True) -> None:
        self.who = who
        self.present = present
        self.writes: list[tuple[int, int]] = []
        self.block_reads: list[tuple[int, int]] = []
        self.fifo = bytearray()
        self.int_status = 0
        self.count_override: int | None = None
        self.closed = False

    def _check(self, addr: int) -> None:
        if not self.present or addr != 0x68:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, addr: int, reg: int) -> int:
        self._check(addr)
        if reg == mpu.WHO_AM_I:
            return self.who
        if reg == mpu.INT_STATUS:
            v, self.int_status = self.int_status, 0
            return v
        return 0

    def write_byte_data(self, addr: int, reg: int, val: int) -> None:
        self._check(addr)
        self.writes.append((reg, val))
        if reg == mpu.USER_CTRL and val & 0x04:
            self.fifo.clear()

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        self._check(addr)
        self.block_reads.append((reg, length))
        if reg == mpu.FIFO_COUNTH:
            n = len(self.fifo) if self.count_override is None else self.count_override
            return [n >> 8, n & 0xFF]
        if reg == mpu.FIFO_R_W:
            out = list(self.fifo[:length])
            del self.fifo[:length]
            return out + [0] * (length - len(out))
        return [0] * length

    def close(self) -> None:
        self.closed = True

    def push(self, *samples: tuple[float, float, float]) -> None:
        for s in samples:
            self.fifo += struct.pack(">hhh", *(round(v * mpu.LSB_PER_G) for v in s))


def _open(bus: FakeBus, address: int = 0x68) -> mpu.Mpu6050:
    return mpu.open_mpu6050(1, address, bus_opener=lambda n: bus, sleep=lambda s: None)


def test_mpu_init_and_fifo_read():
    bus = FakeBus()
    dev = _open(bus)
    assert dev.model == "MPU-6050" and dev.who_am_i == 0x68
    assert bus.writes == [(0x6B, 0x80), (0x6B, 0x00), (0x6C, 0x07), (0x1A, 0x02), (0x19, 4), (0x1C, 0x00),
                          (0x23, 0x08), (0x6A, 0x04), (0x6A, 0x40)]  # без ACCEL_CONFIG2 у оригинала
    samples = [(0.01 * i, -0.02, 1.0) for i in range(12)]
    bus.push(*samples)
    bus.fifo += b"\x01\x02\x03"  # половина следующего отсчёта — остаётся в FIFO
    got = dev.read()
    assert len(got) == 12 and got[3] == pytest.approx((0.03, -0.02, 1.0), abs=1e-4)
    fifo_reads = [n for reg, n in bus.block_reads if reg == mpu.FIFO_R_W]
    assert fifo_reads == [30, 30, 12] and all(n <= 32 and n % 6 == 0 for n in fifo_reads)
    assert len(bus.fifo) == 3
    assert mpu.parse_fifo(struct.pack(">hhh", -16384, 8192, 16384) + b"\x00") == [(-1.0, 0.5, 1.0)]


def test_mpu_overflow_resets_fifo():
    bus = FakeBus()
    dev = _open(bus)
    bus.push((0.0, 0.0, 1.0))
    bus.int_status = mpu.FIFO_OFLOW
    n = len(bus.writes)
    assert dev.read() == [] and dev.overflows == 1 and bus.writes[n:] == [(0x6A, 0x04), (0x6A, 0x40)]
    bus.count_override = 1024  # FIFO полон (без флага)
    assert dev.read() == [] and dev.overflows == 2


def test_mpu_clone_who_am_i():
    bus = FakeBus(who=0x70)
    dev = _open(bus)
    assert dev.model == "MPU-6500 (WHO_AM_I 0x70)" and (0x1D, 0x02) in bus.writes  # фильтр акселерометра 6500
    assert mpu.model_name(0x98) == "MPU-6050 clone (WHO_AM_I 0x98)" and mpu.model_name(0x71).startswith("MPU-9250")


def test_mpu_missing_sensor():
    bus = FakeBus(present=False)
    with pytest.raises(OSError, match="not found at 0x68 on I2C-1"):
        _open(bus)
    assert bus.closed
    with pytest.raises(OSError, match="no sensor at 0x68 on I2C-1 .WHO_AM_I 0xFF"):
        _open(FakeBus(who=0xFF))
    with pytest.raises(OSError, match="not found at 0x69"):
        _open(FakeBus(), 0x69)


def test_mpu_implausible_data():
    bus = FakeBus()
    dev = _open(bus)
    for _ in range(mpu.BAD_BATCHES_MAX - 1):
        bus.push(*[(0.0, 0.0, 0.0)] * 5)  # FIFO «съехал»: тяжести не видно
        assert dev.read() == [] and len(bus.fifo) == 0
    bus.push((0.0, 0.0, 1.0))
    assert len(dev.read()) == 1  # нормальная пачка — счётчик сброшен
    for _ in range(mpu.BAD_BATCHES_MAX - 1):
        bus.push((0.0, 0.0, 0.0))
        dev.read()
    bus.push((0.0, 0.0, 0.0))
    with pytest.raises(OSError, match="implausible"):
        dev.read()


def _wait(cond, timeout: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timeout"
        time.sleep(0.01)


def test_reader_thread_retries_and_delivers():
    states: list[tuple[bool, str | None]] = []
    batches: list[int] = []
    attempts = {"n": 0}
    bus = FakeBus()

    def opener() -> mpu.AccelSource:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FileNotFoundError(2, "No such file or directory", "/dev/i2c-1")
        return _open(bus)

    r = mpu.AccelReader(opener, lambda s: batches.append(len(s)), lambda src, err: states.append((src is not None, err)),
                        period_s=0.01, retry_s=0.01)
    r.start()
    try:
        _wait(lambda: any(ok for ok, _ in states))
        assert states[0][0] is False and states[0][1].startswith("MPU-6050: [Errno 2]")
        bus.push(*[(0.0, 0.0, 1.0)] * 7)
        _wait(lambda: sum(batches) == 7)
        bus.present = False  # датчик отвалился: после трёх неудач — ошибка в статус
        _wait(lambda: states[-1][0] is False)
        assert "not" in states[-1][1]
    finally:
        r.stop()
        r.join(2)


def test_fake_accel_impact_and_tilt():
    clk = {"t": 0.0}
    fake = mpu.FakeAccel(noise_g=0.0, clock=lambda: clk["t"])
    clk["t"] = 1.0
    first = fake.read()
    assert len(first) == 20  # первый раз — 0,1 с
    fake.inject_impact(0.5)
    clk["t"] += 0.1
    got = fake.read()
    gx = fake.gravity()[0]
    assert len(got) == 20 and got[0][0] == pytest.approx(gx + 0.5)
    roll = fake.roll_deg
    fake.inject_tilt(4.0)
    assert fake.roll_deg == roll + 4.0


# ── обнаружение ──


def tilted(deg: float) -> tuple[float, float, float]:
    r = math.radians(deg)
    return (0.0, -math.sin(r), math.cos(r))


def run(det: Detector, samples, chunk: int = 1) -> list[tuple[float, Detection]]:
    out = []
    for i in range(0, len(samples), chunk):
        for d in det.feed(samples[i:i + chunk], DT):
            out.append((det.t, d))
    return out


def quiet(seconds: float, g=(0.0, 0.0, 1.0)) -> list:
    return [g] * round(seconds / DT)


def test_impact_threshold_merge_and_gap():
    det = Detector()
    det.configure(AlarmSettings(impact_g=0.15))
    assert run(det, quiet(1.0)) == []
    assert det.take_level("telemetry") == pytest.approx(0.0, abs=1e-9)
    # 0.1 g — ниже порога
    assert run(det, [(0.1, 0.0, 1.0)] + quiet(1.0)) == []
    # первое превышение 0.2 g, через 0.3 с — 0.5 g: одно событие с пиком 0.5 через 1 с
    t_hit = det.t + DT
    ev = run(det, [(0.2, 0.0, 1.0)] + quiet(0.3) + [(0.0, 0.5, 1.0)] + quiet(1.0))
    assert len(ev) == 1 and ev[0][1].kind == "impact" and ev[0][1].data["peak_g"] == pytest.approx(0.5, abs=0.01)
    assert ev[0][0] == pytest.approx(t_hit + 1.0, abs=2 * DT)
    assert det.take_level("telemetry") == pytest.approx(0.5, abs=0.01)
    assert det.take_level("telemetry") is None  # окно уже забрали
    assert det.take_level("status") == pytest.approx(0.5, abs=0.01)  # у статуса — своё окно
    # через 1.5 с после первого удара — ещё не прошло 2 с: не считается
    assert run(det, [(0.3, 0.0, 1.0)] + quiet(0.2)) == []
    # после паузы — новое событие
    gap = t_hit + IMPACT_GAP_S - det.t
    ev = run(det, quiet(gap + 0.05) + [(0.3, 0.0, 1.0)] + quiet(1.1))
    assert [d.kind for _, d in ev] == ["impact"] and ev[0][1].data["peak_g"] == pytest.approx(0.3, abs=0.01)


def ramp(a: float, b: float, seconds: float) -> list:
    n = round(seconds / DT)
    return [tilted(a + (b - a) * (i + 1) / n) for i in range(n)]


def test_tilt_hold_and_rebase():
    det = Detector()
    det.configure(AlarmSettings(impact_g=2.0, tilt_deg=3.0))
    run(det, quiet(1.0))
    assert det.angle is None
    det.rebase()  # на охране: база — следующие 2 с
    run(det, quiet(2.0))
    assert det.base is not None and det.angle == pytest.approx(0.0, abs=1e-6)
    t0 = det.t
    ev = run(det, ramp(0, 5, 1.0) + [tilted(5)] * round(25 / DT))
    assert len(ev) == 1 and ev[0][1].kind == "tilt" and ev[0][1].data["tilt_deg"] >= 3.0
    assert 6.0 < ev[0][0] - t0 < 12.0  # угол g_slow дошёл до 3° и держался дольше 5 с
    assert det.angle < 1.0  # новая база — текущая тяжесть, повторного события нет


def test_tilt_short_bump_disabled_and_not_armed():
    det = Detector()
    det.configure(AlarmSettings(impact_g=2.0, tilt_deg=3.0))
    det.rebase()
    run(det, quiet(2.0))
    # 10° на 3 с и обратно — угол g_slow выше порога меньше 5 с
    assert run(det, ramp(0, 10, 0.5) + [tilted(10)] * round(2.5 / DT) + ramp(10, 0, 0.5) + quiet(15)) == []
    det.configure(AlarmSettings(impact_g=2.0, tilt_deg=3.0, tilt_enabled=False))
    assert run(det, ramp(0, 6, 1.0) + [tilted(6)] * round(20 / DT)) == []
    assert det.angle > 3.0  # угол всё равно считается (для статуса)
    det.clear_base()  # охрана выключена — наклона нет
    det.configure(AlarmSettings(impact_g=2.0, tilt_deg=3.0))
    assert run(det, ramp(6, 0, 1.0) + quiet(20)) == [] and det.angle is None


M_LAT = 1 / 111_195.0  # градусов широты на метр


def fx(dlat_m: float = 0.0, hdop: float | None = 1.0, fix: str = "3d", age: float = 0.3) -> GpsFix:
    return GpsFix(fix=fix, lat=52.2 + dlat_m * M_LAT, lon=21.0, hdop=hdop, age_s=age)


def feed(m: MoveWatch, dlats, t0: float, step: float = 1.0, move_m: float = 100, **kw) -> list[tuple[float, Detection]]:
    """Точки по одной раз в step с, начиная с t0 → [(время, событие)]."""
    out = []
    for i, d in enumerate(dlats):
        t = t0 + i * step
        det = m.check(fx(d, **kw), move_m, t)
        if det is not None:
            out.append((t, det))
    return out


def test_move_watch_reference_filters_and_repeat_rule():
    m = MoveWatch()
    assert m.check(fx(0, hdop=7.0), 100, 1000.0) is None  # плохой HDOP — не в точку отсчёта
    assert m.check(fx(0, fix="none"), 100, 1000.0) is None
    assert m.check(fx(0, fix="2d"), 100, 1000.0) is None  # 2D — тоже (между домами скачет сильнее всего)
    assert m.check(fx(0, age=30.0), 100, 1000.0) is None  # устаревшая
    assert m._seed == [] and m.ref is None
    assert feed(m, [0, 3, -2, 1], 1001.0) == [] and m.ref is None  # точка отсчёта — по пяти хорошим точкам
    assert m.check(fx(-1), 100, 1005.0) is None
    assert m.ref == pytest.approx((52.2, 21.0))  # медиана
    assert feed(m, [900] * 20, 1010.0, hdop=9.0) == []  # скачок при плохом HDOP — не сдвиг
    assert feed(m, [500] * 20, 1040.0, age=30.0) == []  # устаревшие точки
    assert feed(m, [500] * 20, 1070.0, fix="2d") == []  # 2D
    # 150 м подряд: событие на 11-й точке — пять подряд и 10 с от первой
    [(t, d)] = feed(m, [150] * 15, 1100.0)
    assert t == 1110.0 and d.kind == "move" and d.data["distance_m"] == pytest.approx(150, abs=1)
    assert d.data["lat"] == pytest.approx(52.2 + 150 * M_LAT) and d.data["lon"] == 21.0
    assert feed(m, [400] * 20, 1200.0) == []  # 250 м от сообщённого, но не прошло 300 с
    assert feed(m, [200] * 20, 1420.0) == []  # прошло, но от сообщённого места только 50 м
    [(t, d)] = feed(m, [400] * 12, 1500.0)
    assert t == 1510.0 and d.data["distance_m"] == pytest.approx(400, abs=1)  # от места стоянки
    m2 = MoveWatch.from_dict(json.loads(json.dumps(m.to_dict())))
    assert (m2.ref, m2.last, m2.last_ts) == (m.ref, m.last, m.last_ts)


def test_move_watch_ignores_outliers():
    """Двор между домами, паркинг: точки скачут на 100–200 м — без подряд идущих далёких точек сдвига нет."""
    m = MoveWatch()
    feed(m, [0] * 5, 0.0)
    assert feed(m, [0, 180, 0, 0, 170, 175, 3, 0], 10.0) == []  # одиночные выбросы, точка ближе — счёт заново
    assert feed(m, [200] * 9 + [0], 30.0) == []  # выброс держится 8 с — меньше 10 с
    # редкие выбросы через паузы без хороших точек (паркинг: 2D, HDOP > 5) — не копятся
    for k in range(10):
        assert m.check(fx(200), 100, 100.0 + 40.0 * k) is None
        assert m.check(fx(0, fix="2d"), 100, 120.0 + 40.0 * k) is None
    assert m.last is None


def test_move_watch_first_fix_outlier_does_not_shift_the_reference():
    m = MoveWatch()
    assert feed(m, [190, 0, 2, -1, 1], 0.0) == []
    assert m.ref == pytest.approx((52.2 + 1 * M_LAT, 21.0))  # медиана: выброс первой точкой не в счёт
    assert feed(m, [0, 1, -2] * 10, 10.0) == []  # верные точки — не «далеко»


def test_move_watch_tow_away():
    m = MoveWatch()
    feed(m, [0] * 5, 0.0)
    # эвакуатор, 7 м/с: дальше 100 м — с 15-й точки (t = 25), событие — через 10 с
    [(t, d)] = feed(m, [7 * i for i in range(60)], 10.0)
    assert t == 35.0 and d.data["distance_m"] == 175


def test_move_watch_restored_reference_needs_confirmation_too():
    m = MoveWatch()
    feed(m, [0] * 5, 0.0)
    m2 = MoveWatch.from_dict(json.loads(json.dumps(m.to_dict())))  # после перезапуска
    assert m2.ref == m.ref
    assert feed(m2, [0, 300, 0], 100.0) == []  # выброс — не сдвиг и после перезапуска
    [(t, _)] = feed(m2, [300] * 12, 200.0)
    assert t == 210.0


def test_event_texts():
    ts = time.mktime((2026, 10, 27, 14, 3, 0, 0, 0, -1))
    assert make_event("impact", {"peak_g": 0.4234}, ts).body == "Hit 0.42 g at 14:03"
    e = make_event("tilt", {"tilt_deg": 4.24}, ts)
    assert (e.title, e.body, e.url) == ("Van tilted", "Tilted 4.2° at 14:03", "#/alarm")
    e = make_event("move", {"distance_m": 230}, ts)
    assert (e.title, e.body, e.url) == ("Van is moving", "230 m from the parking spot at 14:03", "#/tracking")
    e = make_event("test", {}, ts)
    assert (e.title, e.body) == ("Test alarm", "Alarm notifications work (14:03)") and len(e.id) == 32


# ── сервис ──


class Clock:
    def __init__(self) -> None:
        self.wall = 1_792_000_000.0
        self.m = 100.0

    def time(self) -> float:
        return self.wall

    def mono(self) -> float:
        return self.m

    def tick(self, s: float) -> None:
        self.wall += s
        self.m += s


class DummySource:
    model = "MPU-6050"
    address = 0x68
    rate_hz = 200.0

    def read(self):
        return []

    def close(self) -> None: ...


class FakeRelay:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.up = True

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=req)
        assert req.url.path == "/device/alarm" and req.headers[TOKEN_HEADER] == "tok"
        body = json.loads(req.content)
        self.bodies.append(body)
        return httpx.Response(200, json={"ok": True, "accepted": len(body["events"])})


def make_alarm(tmp_path, clk: Clock | None = None, relay: RelayConfig | None = None, transport=None, radio=None,
               protect=None, fix: GpsFix | None = None, **settings):
    cfg = {"s": AlarmSettings(**{"siren": False, **settings})}
    clk = clk or Clock()
    imu = ImuService(lambda: cfg["s"], open_source=lambda b, a: DummySource())
    svc = AlarmService(tmp_path, lambda: cfg["s"], imu, relay or RelayConfig(), get_fix=lambda: fix or GpsFix(),
                       protect=protect, radio=radio, transport=transport, clock=clk.time, mono=clk.mono)
    return svc, clk, cfg


async def settle(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def until(cond, timeout: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timeout"
        await asyncio.sleep(0.01)


async def test_state_machine_and_arm_delay(tmp_path):
    svc, clk, _ = make_alarm(tmp_path, arm_delay_s=60)
    await svc.start(background=False)
    st = await svc.arm()
    assert st.state == "arming" and st.arm_in_s == 60 and st.armed_at is None
    await svc._fire(Detection("impact", {"peak_g": 1.0}))  # в arming событий нет — только запись о постановке
    assert [e.kind for e in svc.events] == ["armed"] and svc.events[0].data == {"source": "web", "delay_s": 60}
    clk.tick(30.2)
    await svc.poll()
    assert svc.status().arm_in_s == 30 and svc.state == "arming"
    assert (await svc.arm()).arm_in_s == 30  # повторное «включить» не сбрасывает отсчёт
    clk.tick(30)
    await svc.poll()
    st = svc.status()
    assert st.state == "armed" and st.armed_at == clk.wall and st.arm_in_s is None
    await svc._fire(Detection("impact", {"peak_g": 0.42}))
    st = svc.status()
    assert st.state == "triggered" and st.events[0].kind == "impact" and st.events[0].title == "Impact detected"
    tel = svc.telemetry()
    assert tel.seq == 2 and tel.last.kind == "impact" and tel.state == "triggered"
    clk.tick(29)
    await svc.poll()
    assert svc.state == "triggered"  # 30 с после последнего события
    clk.tick(1.5)
    await svc.poll()
    assert svc.state == "armed"
    assert (await svc.disarm()).state == "off"
    await svc._fire(Detection("tilt", {"tilt_deg": 5.0}))  # выключена — не событие
    assert [e.kind for e in svc.events] == ["disarmed", "impact", "armed"]
    assert svc.events[0].data == {"source": "web", "was": "armed"}
    await svc.stop()


async def test_events_from_sensor_samples_only_when_armed(tmp_path):
    svc, _clk, _ = make_alarm(tmp_path, arm_delay_s=0)
    await svc.start(background=False)
    svc.imu._on_state(DummySource(), None)
    spike = [(0.0, 0.0, 1.0)] * 40 + [(0.6, 0.0, 1.0)] + [(0.0, 0.0, 1.0)] * 220
    svc.imu.feed(spike)
    await settle()
    assert svc.events == [] and svc.status().level_g == pytest.approx(0.6, abs=0.01)
    await svc.arm()
    svc.imu.feed([(0.0, 0.0, 1.0)] * 400 + spike)
    await until(lambda: svc.events[0].kind == "impact")
    assert [e.kind for e in svc.events] == ["impact", "armed"]
    assert svc.events[0].data["peak_g"] == pytest.approx(0.6, abs=0.01)
    assert svc.state == "triggered" and svc.status().tilt_deg == pytest.approx(0.0, abs=0.2)
    await svc.stop()


async def test_state_and_journal_survive_restart(tmp_path):
    async def protect():
        return {"ok": True, "segment": "2026-10-27/2026-10-27_14-00-00.ts"}

    svc, clk, _ = make_alarm(tmp_path, arm_delay_s=0, protect=protect, fix=fx(0))
    await svc.start(background=False)
    await svc.arm()
    for _ in range(5):  # точка отсчёта для сдвига — по пяти точкам
        await svc.check_gps()
        clk.tick(1.0)
    await svc._fire(Detection("impact", {"peak_g": 0.3}))
    assert svc.events[0].data["segment"] == "2026-10-27/2026-10-27_14-00-00.ts"
    await svc.stop()
    saved = json.loads((tmp_path / "alarm_state.json").read_text())
    assert saved["on"] is True and saved["seq"] == 2 and saved["move"]["ref"] is not None

    svc2, _, _ = make_alarm(tmp_path, clk=clk)
    await svc2.start(background=False)
    assert svc2.state == "armed" and svc2.armed_at == saved["armed_at"] and svc2.seq == 2
    assert svc2.events[0].data["segment"].endswith(".ts") and svc2.move.ref is not None
    assert svc2.detector._base_acc is not None  # база наклона — заново, по первым 2 с
    await svc2.disarm()
    await svc2.stop()

    svc3, _, _ = make_alarm(tmp_path, arm_delay_s=60)
    await svc3.start(background=False)
    assert svc3.state == "off"
    await svc3.arm()  # перезапуск во время arming — сразу armed
    await svc3.stop()
    svc4, _, _ = make_alarm(tmp_path)
    await svc4.start(background=False)
    assert svc4.state == "armed" and svc4.armed_at is not None
    await svc4.stop()
    (tmp_path / "alarm_state.json").write_text("{broken")
    (tmp_path / "alarm_events.json").write_text('[{"id": 1}, "junk"]')
    svc5, _, _ = make_alarm(tmp_path)
    await svc5.start(background=False)
    assert svc5.state == "off" and svc5.events == []
    await svc5.stop()


async def test_journal_is_capped(tmp_path):
    svc, _, _ = make_alarm(tmp_path)
    await svc.start(background=False)
    for _ in range(205):
        svc._record(make_event("test", {}, 1.8e9))
    await svc._save_events()
    assert len(svc.events) == 200 and len(svc.status().events) == 100 and svc.seq == 205
    assert len(json.loads((tmp_path / "alarm_events.json").read_text())) == 200
    assert (await svc.clear_events()).events == [] and svc.telemetry().last is None
    await svc.stop()


async def test_gps_move_event(tmp_path):
    cur = {"fix": fx(0)}
    svc, clk, _ = make_alarm(tmp_path, arm_delay_s=0, move_m=100)
    svc.get_fix = lambda: cur["fix"]
    await svc.start(background=False)
    await svc.check_gps()
    assert svc.events == [] and svc.move._seed == []  # выключена
    await svc.arm()
    for _ in range(5):
        await svc.check_gps()
        clk.tick(1.0)
    assert svc.move.ref is not None
    cur["fix"] = fx(230)
    await svc.check_gps()
    assert svc.state == "armed"  # одна далёкая точка — ещё не сдвиг
    for _ in range(10):
        clk.tick(1.0)
        await svc.check_gps()
    ev = svc.events[0]
    assert ev.kind == "move" and ev.url == "#/tracking" and ev.data["distance_m"] == 230
    assert ev.body.startswith("230 m from the parking spot at ") and svc.state == "triggered"
    await svc.disarm()
    await svc.arm()  # новая постановка — новая точка отсчёта
    for _ in range(5):
        await svc.check_gps()
        clk.tick(1.0)
    assert svc.move.ref == pytest.approx((52.2 + 230 * M_LAT, 21.0))
    await svc.stop()


async def test_push_cooldown_and_relay_payload(tmp_path):
    relay = FakeRelay()
    svc, clk, cfg = make_alarm(tmp_path, arm_delay_s=0, push_cooldown_s=60, relay=RelayConfig("http://relay", "tok"),
                               transport=httpx.MockTransport(relay))
    await svc.start(background=False)
    await svc.arm()
    await svc._fire(Detection("impact", {"peak_g": 0.3}))
    clk.tick(10)
    await svc._fire(Detection("impact", {"peak_g": 0.5}))  # в журнал — да, push — нет (пауза 60 с на вид)
    await svc._fire(Detection("tilt", {"tilt_deg": 4.0}))  # другой вид — push
    clk.tick(51)
    await svc._fire(Detection("impact", {"peak_g": 0.7}))
    await svc.test()
    await svc.test()  # проверка уведомлений — без паузы
    assert len(svc.events) == 7 and svc.outbox.count() == 5 and svc.status().relay.pending == 5  # armed — без push
    relay.up = False
    with pytest.raises(httpx.HTTPError):
        await svc.send_once()
    assert svc.outbox.count() == 5  # не доставлено — лежит в outbox
    relay.up = True
    assert await svc.send_once() == 5
    body = relay.bodies[-1]
    assert body["state"] == {"armed": True, "notify_offline": False}
    assert [e["kind"] for e in body["events"]] == ["impact", "tilt", "impact", "test", "test"]
    e = body["events"][0]
    assert set(e) == {"id", "ts", "kind", "title", "body", "url", "data"} and e["data"]["peak_g"] == 0.3
    assert svc.outbox.count() == 0 and svc.status().relay.pending == 0
    cfg["s"] = cfg["s"].model_copy(update={"notify_offline": True})
    await svc.disarm()  # посреди тревоги — push, даже из админки
    await svc.send_once()
    body = relay.bodies[-1]
    assert body["state"] == {"armed": False, "notify_offline": True}
    assert [(e["kind"], e["data"]) for e in body["events"]] == [("disarmed", {"source": "web", "was": "triggered"})]
    await svc.send_once()  # пульс: без событий — только состояние
    assert relay.bodies[-1] == {"state": {"armed": False, "notify_offline": True}, "events": []}
    await svc.stop()


async def test_outbox_survives_restart_and_not_configured(tmp_path):
    svc, _, _ = make_alarm(tmp_path, relay=RelayConfig("http://relay", "tok"), transport=FakeRelayDown())
    await svc.start(background=False)
    await svc.test()
    await svc.stop()
    relay = FakeRelay()
    svc2, _, _ = make_alarm(tmp_path, relay=RelayConfig("http://relay", "tok"), transport=httpx.MockTransport(relay))
    await svc2.start(background=True)  # сразу после старта — отправка того, что осталось
    for _ in range(100):
        if relay.bodies:
            break
        await asyncio.sleep(0.01)
    assert [e["kind"] for e in relay.bodies[0]["events"]] == ["test"]
    await svc2.arm()  # смена состояния — отправка сразу
    for _ in range(100):
        if len(relay.bodies) > 1:
            break
        await asyncio.sleep(0.01)
    assert relay.bodies[-1]["state"]["armed"] is True
    await svc2.stop()
    svc3, _, _ = make_alarm(tmp_path / "x")  # сервер не настроен: события только в журнал
    await svc3.start(background=False)
    st = await svc3.test()
    assert st.relay.configured is False and st.relay.pending == 0 and len(st.events) == 1
    await svc3.stop()


def FakeRelayDown() -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    return httpx.MockTransport(handler)


# ── сирена ──


class CtlPlayer:
    def __init__(self) -> None:
        self.cb = None
        self.loads: list[tuple[str, str]] = []
        self.halts = 0
        self.volume: int | None = None
        self.volumes: list[int] = []

    def on_event(self, cb) -> None:
        self.cb = cb

    async def start(self) -> None:
        self.cb("ready", True)

    async def close(self) -> None: ...

    async def load(self, url: str, device: str) -> None:
        self.loads.append((url, device))

    async def halt(self) -> None:
        self.halts += 1

    async def set_volume(self, v: int) -> None:
        self.volume = v
        self.volumes.append(v)


async def make_radio(tmp_path, connected: bool = True) -> tuple[RadioService, CtlPlayer]:
    p = CtlPlayer()
    d = RadioDirectory(transport=httpx.MockTransport(lambda r: httpx.Response(404)), retry_pause_s=0)
    radio = RadioService(d, p, tmp_path / "radio_state.json", lambda: RadioSettings(volume=40))
    await radio.start()
    radio.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", name="JBL", connected=connected))
    await settle()
    return radio, p


ST = RadioStation(uuid="u1", name="Jazz", url="http://jazz.example/stream")
DEV = bluealsa_device("AA:BB:CC:DD:EE:FF")


async def test_siren_over_the_radio_and_restore(tmp_path):
    radio, p = await make_radio(tmp_path)
    await radio.play(ST)
    p.cb("playing", True)
    svc, _, _ = make_alarm(tmp_path, radio=radio)
    svc.state = "armed"
    wav = tmp_path / "alarm_siren.wav"
    assert await svc.siren.start(0.15)
    assert svc.siren.sounding and radio.alert_active and p.loads[-1] == (str(wav), DEV) and p.volume == 100
    assert wav.read_bytes()[:4] == b"RIFF" and wav.stat().st_size == len(siren_wav())
    # пока звучит: громкость и станция радио меняются только в состоянии, сирена не прерывается
    await radio.set_volume(70)
    p.cb("title", ({"icy-title": "Song"}, None))
    assert p.volume == 100 and radio.volume == 70
    n = len(p.loads)
    p.cb("ended", "eof")  # файл доиграл — по кругу
    await settle()
    assert len(p.loads) == n + 1 and p.loads[-1][0] == str(wav)
    p.cb("ended", "Failed to open")  # ошибка (колонка пропала) — не повторяем
    await settle()
    assert len(p.loads) == n + 1
    await asyncio.sleep(0.3)
    assert not svc.siren.sounding and not radio.alert_active
    assert p.volume == 70 and p.loads[-1] == (ST.url, DEV)  # снова станция со своей громкостью
    await radio.stop()


async def test_siren_silenced_returns_to_silence_and_needs_a_speaker(tmp_path):
    radio, p = await make_radio(tmp_path)
    svc, _, _ = make_alarm(tmp_path, radio=radio)
    svc.state = "armed"
    halts = p.halts
    assert await svc.siren.start(30)
    await svc.silence()  # сразу
    assert not svc.siren.sounding and not radio.alert_active and p.halts == halts + 1 and p.volume == 40
    radio.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", name="JBL", connected=False))
    await settle()
    assert await svc.siren.start(30) is False and not radio.alert_active  # нет колонки — пропуск
    svc.state = "off"
    radio.set_output(RadioOutput(address="AA:BB:CC:DD:EE:FF", name="JBL", connected=True))
    await settle()
    assert await svc.siren.start(30) is False  # охрану выключили, пока сирена собиралась зазвучать
    svc_none, _, _ = make_alarm(tmp_path / "n")
    svc_none.state = "armed"
    assert await svc_none.siren.start(5) is False
    await radio.stop()


async def test_event_starts_siren_and_disarm_stops_it(tmp_path):
    radio, _p = await make_radio(tmp_path)
    svc, _, cfg = make_alarm(tmp_path, radio=radio, arm_delay_s=0)
    cfg["s"] = cfg["s"].model_copy(update={"siren": True})
    await svc.start(background=False)
    await svc.test()  # проверка — без сирены
    await settle()
    assert not svc.siren.sounding
    await svc.arm()
    await svc._fire(Detection("impact", {"peak_g": 0.5}))
    await until(lambda: svc.siren.sounding)
    assert svc.status().siren and svc.telemetry().siren
    await svc.disarm()
    assert not svc.siren.sounding and not radio.alert_active
    await svc.stop()
    await radio.stop()


# ── API и клавиша ──


async def test_alarm_api_mock(data_dir):
    async with running_app() as c:
        assert (await c.get("/api/alarm")).status_code == 401
        await login(c)
        st = (await c.get("/api/alarm")).json()
        assert st["state"] == "off" and st["sensor"]["ok"] is False and st["sensor"]["address"] == 104
        assert st["relay"] == {"configured": False, "pending": 0} and st["events"] == [] and st["siren"] is False
        st = (await c.post("/api/alarm/arm")).json()
        assert st["state"] == "arming" and st["arm_in_s"] == 60
        assert (await c.post("/api/alarm/disarm")).json()["state"] == "off"
        r = await c.post("/api/alarm/simulate", json={"kind": "impact", "g": 0.7})
        assert r.status_code == 200  # не на охране — не событие; в журнале — только поставили / сняли
        assert [(e["kind"], e["data"]["source"]) for e in r.json()["events"]] == [("disarmed", "web"), ("armed", "web")]
        assert (await c.patch("/api/settings", json={"alarm": {"arm_delay_s": 0, "siren": False}})).status_code == 200
        assert (await c.post("/api/alarm/arm")).json()["state"] == "armed"
        st = (await c.post("/api/alarm/simulate", json={"kind": "impact", "g": 0.7})).json()
        assert st["state"] == "triggered" and st["events"][0]["kind"] == "impact"
        assert st["events"][0]["data"]["peak_g"] == 0.7 and st["events"][0]["body"].startswith("Hit 0.70 g at ")
        st = (await c.post("/api/alarm/simulate", json={"kind": "move"})).json()
        assert st["events"][0]["kind"] == "move" and st["events"][0]["url"] == "#/tracking"
        st = (await c.post("/api/alarm/test")).json()
        assert st["events"][0]["title"] == "Test alarm" and len(st["events"]) == 6
        assert [e["kind"] for e in st["events"][:4]] == ["test", "move", "impact", "armed"]
        short = (await c.get("/api/alarm", params={"events": 1})).json()  # дашборд: только последние
        assert [e["kind"] for e in short["events"]] == ["test"] and short["state"] == st["state"]
        tel = (await c.get("/api/telemetry")).json()["alarm"]
        assert tel["seq"] == 6 and tel["last"]["kind"] == "test" and tel["state"] == "triggered"
        assert (await c.post("/api/alarm/silence")).json()["state"] == "triggered"
        assert (await c.delete("/api/alarm/events")).json()["events"] == []
        assert (await c.post("/api/alarm/simulate", json={"kind": "boom"})).status_code == 422
        assert (await c.patch("/api/settings", json={"alarm": {"impact_g": 5}})).status_code == 422
        assert (await c.patch("/api/settings", json={"alarm": {"sensor_address": 0x70}})).status_code == 422
        assert (await c.post("/api/alarm/disarm")).json()["state"] == "off"
        assert json.loads((data_dir / "alarm_state.json").read_text())["on"] is False


def test_simulate_only_in_mock(data_dir):
    paths = {getattr(r, "path", "") for r in create_app(make_opts(mock=False)).routes}
    assert "/api/alarm" in paths and "/api/alarm/simulate" not in paths  # в --mock он есть: test_alarm_api_mock


async def test_keypad_alarm_toggle(data_dir):
    assert next(a for a in kp.ACTIONS if a["id"] == "alarm")["title"] == "Alarm on/off"
    async with running_app() as c:
        core = c.app.state.core
        await core.keypad.dispatch("alarm")
        assert core.alarm.state == "arming"
        await core.keypad.dispatch("alarm")
        assert core.alarm.state == "off"
        await core.store.patch({"alarm": {"arm_delay_s": 0}, "keypad": {"key5": "alarm"}})
        assert core.keypad.decode(kp.EVENT.pack(0, 0, kp.EV_KEY, kp.KEY_F13 + 4, 1)) == ["alarm"]
        await core.keypad.dispatch("alarm")
        assert core.alarm.state == "armed"
        await core.alarm._fire(Detection("impact", {"peak_g": 0.4}))
        await core.keypad.dispatch("alarm")  # из triggered — выключить
        assert core.alarm.state == "off"
        ev = core.alarm.events[0]  # в журнале: сняли клавишей посреди тревоги
        assert ev.kind == "disarmed" and ev.data == {"source": "keypad", "was": "triggered"}
        assert [(e.kind, e.data["source"]) for e in core.alarm.events if e.kind != "impact"] == [
            ("disarmed", "keypad"), ("armed", "keypad"), ("disarmed", "keypad"), ("armed", "keypad")]
        await login(c)
        acts = (await c.get("/api/controls")).json()["keypad"]["actions"]
        assert {"id": "alarm", "title": "Alarm on/off"}.items() <= next(a for a in acts if a["id"] == "alarm").items()


async def test_imu_consumer_error_does_not_stop_others():
    got: list[int] = []
    imu = ImuService(lambda: AlarmSettings(), open_source=lambda b, a: DummySource())

    def boom(samples, dt):
        raise RuntimeError("x")

    imu.subscribe(boom)
    imu.subscribe(lambda s, dt: got.append(len(s)))
    imu.feed([(0.0, 0.0, 1.0)] * 3)
    assert got == [3]
    imu.start()
    try:
        await until(lambda: imu.ok)
        assert imu.model == "MPU-6050" and imu.running
    finally:
        await imu.stop()
    assert not imu.running and not imu.ok
    imu.apply_settings(AlarmSettings(sensor_bus=3, sensor_address=105))
    assert imu.address == 105 and imu._cfg == (3, 105)


async def test_imu_finds_the_sensor_on_its_own_bus_or_the_compass_bus():
    """По умолчанию I2C-3 (pins 7/29); нет там — I2C-1 (pins 3/5, вместе с компасом); нет нигде — обе причины."""
    wired = {"bus": 1}

    def opener(bus: int, address: int):
        if bus != wired["bus"]:
            raise OSError(f"MPU-6050: not found at 0x{address:02X} on I2C-{bus}")
        return DummySource()

    imu = ImuService(lambda: AlarmSettings(), open_source=opener)
    assert imu._open_any() is not None and imu.bus == 1
    wired["bus"] = 3
    imu._open_any()
    assert imu.bus == 3
    wired["bus"] = 7
    with pytest.raises(OSError) as e:
        imu._open_any()
    assert "on I2C-3" in str(e.value) and "I2C-1" in str(e.value) and imu.bus is None


# ── пропало видео камеры ──


def test_video_watch_rules():
    w = VideoWatch()
    g = 20.0
    assert w.check({"front": False, "rear": True}, g, 0.0, 1000.0) == []  # у передней видео с постановки не было
    assert w.check({"front": False, "rear": False}, g, 1.0, 1001.0) == []  # задняя пропала — ждём grace
    assert w.check({"front": False, "rear": True}, g, 15.0, 1015.0) == []  # вернулась раньше — не событие
    assert w.check({"front": False, "rear": False}, g, 16.0, 1016.0) == []
    assert w.check({"front": False, "rear": False}, g, 35.0, 1035.0) == []
    [d] = w.check({"front": False, "rear": False}, g, 36.0, 1036.0)
    assert d.kind == "video_lost" and d.data == {"camera": "rear", "since": 1016.0} and w.is_lost("rear")
    assert w.check({"front": False, "rear": False}, g, 100.0, 1100.0) == []  # одно событие на пропажу
    assert w.check({"front": False, "rear": True}, g, 101.0, 1101.0) == []  # видео снова — ждём 10 с подряд
    assert w.check({"front": False, "rear": False}, g, 105.0, 1105.0) == []  # мигает — ещё не вернулась
    assert w.check({"front": False, "rear": True}, g, 106.0, 1106.0) == []
    [d] = w.check({"front": False, "rear": True}, g, 116.0, 1116.0)
    assert d.kind == "video_back" and d.data == {"camera": "rear", "lost_s": 90} and not w.is_lost("rear")
    # обе: передняя показалась, потом пропали обе в одну проверку — одно событие «both»
    assert w.check({"front": True, "rear": True}, g, 120.0, 1120.0) == []
    assert w.check({"front": False, "rear": False}, g, 121.0, 1121.0) == []
    [d] = w.check({"front": False, "rear": False}, g, 141.0, 1141.0)
    assert d.data == {"camera": "both", "since": 1121.0}
    # задняя пропала раньше, передняя — потом: второе событие тоже «both» (теперь нет обеих)
    w.reset()
    w.check({"front": True, "rear": True}, g, 0.0, 0.0)
    w.check({"front": True, "rear": False}, g, 1.0, 1.0)
    [d] = w.check({"front": True, "rear": False}, g, 21.0, 21.0)
    assert d.data["camera"] == "rear"
    w.check({"front": False, "rear": False}, g, 30.0, 30.0)
    [d] = w.check({"front": False, "rear": False}, g, 50.0, 50.0)
    assert d.data == {"camera": "both", "since": 30.0}
    # заднюю выключили в настройках (её нет в опросе) — забыта, без video_back
    assert w.check({"front": False}, g, 60.0, 60.0) == [] and not w.is_lost("rear")
    assert [c.model_dump() for c in w.status({"front": False, "rear": True})] == [
        {"camera": "front", "video": False, "lost": True}, {"camera": "rear", "video": True, "lost": False}]


def test_video_event_texts():
    ts = time.mktime((2026, 10, 27, 14, 3, 0, 0, 0, -1))
    e = make_event("video_lost", {"camera": "rear", "since": ts - 60}, ts)
    assert (e.title, e.body, e.url) == ("Rear camera lost", "No video from the rear camera since 14:02", "#/alarm")
    e = make_event("video_lost", {"camera": "both", "since": ts}, ts)
    assert (e.title, e.body) == ("Both cameras lost", "No video from either camera since 14:03")
    e = make_event("video_back", {"camera": "front", "lost_s": 200}, ts)
    assert (e.title, e.body) == ("Front camera is back", "Video from the front camera again at 14:03, after 3 min without it")
    assert make_event("video_back", {"camera": "rear", "lost_s": 40}, ts).body.endswith("after 40 s without it")
    assert make_event("video_back", {"camera": "rear", "lost_s": 4500}, ts).body.endswith("after 1 h 15 min without it")


async def test_video_lost_raises_the_alarm_without_siren_and_back_is_a_note(tmp_path):
    relay = FakeRelay()

    async def protect():
        return {"ok": True, "segment": "2026-10-27/2026-10-27_14-00-00.ts"}

    asked: list = []

    async def protect_rear(recent: bool = False):
        asked.append(recent)  # камера пропала: текущей записи нет — последняя закрытая
        return {"ok": True, "segment": "rear/2026-10-27/2026-10-27_13-59-00.ts"}

    svc, clk, cfg = make_alarm(tmp_path, arm_delay_s=0, siren=True, video_loss_s=20, protect=protect,
                               relay=RelayConfig("http://relay", "tok"), transport=httpx.MockTransport(relay))
    svc.protect_rear = protect_rear
    sirens: list[float] = []

    async def siren_start(seconds: float) -> bool:
        sirens.append(seconds)
        return True

    svc.siren.start = siren_start
    video = {"front": True, "rear": True}
    svc.get_video = lambda: dict(video)
    await svc.start(background=False)
    await svc.check_video()
    assert svc.video.cams == {}  # не на охране — не следим
    await svc.arm()
    await svc.check_video()
    assert [c.model_dump() for c in svc.status().cameras] == [
        {"camera": "front", "video": True, "lost": False}, {"camera": "rear", "video": True, "lost": False}]
    video["rear"] = False
    for _ in range(20):
        clk.tick(1.0)
        await svc.check_video()
    assert [e.kind for e in svc.events] == ["armed"] and svc.state == "armed"  # 19 с — ещё нет
    clk.tick(1.0)
    await svc.check_video()
    await settle()
    ev = svc.events[0]
    assert ev.kind == "video_lost" and ev.title == "Rear camera lost" and svc.state == "triggered"
    assert ev.data["camera"] == "rear" and ev.data["since"] == pytest.approx(clk.wall - 20)
    assert ev.data["segment"].endswith(".ts") and ev.data["segment_rear"].startswith("rear/") and asked == [True]
    assert sirens == []  # без сирены: бывает и сбой Wi-Fi
    assert svc.status().cameras[1].model_dump() == {"camera": "rear", "video": False, "lost": True}
    clk.tick(31.0)
    await svc.poll()
    assert svc.state == "armed"
    video["rear"] = True
    for _ in range(11):
        clk.tick(1.0)
        await svc.check_video()
    assert [e.kind for e in svc.events] == ["video_back", "video_lost", "armed"] and svc.state == "armed"  # не тревога
    assert svc.events[0].data == {"camera": "rear", "lost_s": 52}
    assert svc.outbox.count() == 2
    # выключили в настройках — не следим (и после включения камера должна снова показаться)
    cfg["s"] = cfg["s"].model_copy(update={"video_loss": False})
    video["front"] = False
    for _ in range(30):
        clk.tick(1.0)
        await svc.check_video()
    assert len(svc.events) == 3 and svc.video.cams == {}
    await svc.disarm()
    assert svc.status().cameras[0].lost is False
    await svc.stop()


async def test_video_lost_api_mock(data_dir):
    async with running_app() as c:
        await login(c)
        st = (await c.get("/api/alarm")).json()
        assert st["cameras"] == [{"camera": "front", "video": True, "lost": False}]  # задняя выключена — её нет
        r = await c.post("/api/alarm/simulate", json={"kind": "video_lost", "camera": "rear"})
        assert r.status_code == 409
        st = (await c.post("/api/alarm/simulate", json={"kind": "video_lost", "seconds": 30})).json()
        assert st["cameras"] == [{"camera": "front", "video": False, "lost": False}] and st["events"] == []
        cam = (await c.get("/api/camera/status")).json()
        assert cam["state"] == "error" and cam["video_age_s"] is None


# ── карта памяти не пишется, outbox битый ──


def read_only(*_a, **_kw):
    raise OSError(30, "Read-only file system")


async def test_event_goes_out_when_the_card_cannot_be_written(tmp_path, monkeypatch):
    """Карта полна / перемонтирована «только чтение»: файлы охраны, снимки и outbox не пишутся — сирена, аварийка и
    push всё равно (раньше ошибка записи до сирены отменяла всё)."""
    relay = FakeRelay()
    svc, _clk, _ = make_alarm(tmp_path, arm_delay_s=0, siren=True, hazard=HazardSettings(enabled=True, seconds=5),
                              relay=RelayConfig("http://relay", "tok"), transport=httpx.MockTransport(relay))
    sirens: list[float] = []

    async def siren_start(seconds: float) -> bool:
        sirens.append(seconds)
        return True

    async def snapshot(before_s: float = 0.0) -> bytes:
        return b"\xff\xd8 jpeg"

    svc.siren.start = siren_start
    svc.snapshot = snapshot
    await svc.start(background=False)
    await svc.arm()
    assert svc.status().storage_error is None
    monkeypatch.setattr(alarm_mod, "atomic_write_text", read_only)
    monkeypatch.setattr(svc, "_write_snapshot", read_only)

    def db_read_only(*_a, **_kw):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    assert svc.outbox.db is not None
    monkeypatch.setattr(svc.outbox.db, "add", db_read_only)
    await svc._fire(Detection("impact", {"peak_g": 0.5}))
    await settle()
    assert svc.state == "triggered" and sirens == [30] and svc.hazard.active and svc.hazard.lit
    assert svc.events[0].kind == "impact" and "snapshot" not in svc.events[0].data
    st = svc.status()
    assert st.relay.pending == 1 and svc.outbox.db is None  # outbox — в памяти
    assert "Read-only file system" in st.storage_error and "readonly database" in st.storage_error
    assert await svc.send_once() == 1
    assert [e["kind"] for e in relay.bodies[-1]["events"]] == ["impact"] and relay.bodies[-1]["state"]["armed"]
    await svc._fire(Detection("tilt", {"tilt_deg": 4.0}))  # и дальше — через память
    assert await svc.send_once() == 1 and svc.outbox.count() == 0
    monkeypatch.undo()  # карта снова пишется — ошибка файлов уходит из статуса (outbox — в памяти до перезапуска)
    await svc.silence()
    await svc.test()
    assert svc.status().storage_error.startswith("Notifications wait in memory only")
    assert json.loads((tmp_path / "alarm_events.json").read_text())[0]["kind"] == "test"
    await svc.stop()


async def test_broken_outbox_is_moved_aside(tmp_path):
    """Пропало питание — файл outbox испорчен: при старте он уходит в сторону (*.bad-<время>), база новая."""
    (tmp_path / "alarm_outbox.db").write_bytes(b"not a database " * 500)
    (tmp_path / "alarm_outbox.db-wal").write_bytes(b"junk")
    relay = FakeRelay()
    svc, _, _ = make_alarm(tmp_path, relay=RelayConfig("http://relay", "tok"), transport=httpx.MockTransport(relay))
    await svc.start(background=False)
    bad = svc.outbox.moved
    assert bad is not None and bad.name.startswith("alarm_outbox.db.bad-") and bad.read_bytes().startswith(b"not a")
    wal = tmp_path / "alarm_outbox.db-wal"  # журнал битой базы к новой не пристаёт (уходит с ней или его убрал SQLite)
    assert not wal.exists() or wal.read_bytes() != b"junk"
    assert svc.outbox.error is None and svc.status().storage_error is None
    await svc.test()
    assert svc.outbox.count() == 1 and await svc.send_once() == 1
    await svc.stop()
    svc2, _, _ = make_alarm(tmp_path)  # новая база открывается как обычно
    await svc2.start(background=False)
    assert svc2.outbox.moved is None and svc2.outbox.db is not None
    await svc2.stop()


async def test_outbox_that_cannot_be_opened_keeps_events_in_memory(tmp_path, monkeypatch):
    relay = FakeRelay()
    (tmp_path / "alarm_outbox.db").mkdir()  # не открывается (как карта «только чтение»): файл не трогаем
    svc, _, _ = make_alarm(tmp_path, arm_delay_s=0, relay=RelayConfig("http://relay", "tok"),
                           transport=httpx.MockTransport(relay))
    await svc.start(background=True)  # циклы охраны и сервера всё равно идут
    assert len(svc._tasks) == 2 and svc.outbox.db is None and svc.outbox.moved is None
    assert "unable to open database file" in svc.status().storage_error
    await svc.arm()
    await svc._fire(Detection("impact", {"peak_g": 0.4}))
    await until(lambda: any(b["events"] for b in relay.bodies))
    assert [e["kind"] for b in relay.bodies for e in b["events"]] == ["impact"] and svc.outbox.count() == 0
    await svc.stop()
    # битый файл, который и убрать нельзя (карта «только чтение») — тоже память
    d = tmp_path / "ro"
    d.mkdir()
    (d / "alarm_outbox.db").write_bytes(b"garbage" * 100)
    monkeypatch.setattr(AlarmOutbox, "_move_aside", read_only)
    svc2, _, _ = make_alarm(d)
    await svc2.start(background=False)
    assert svc2.outbox.db is None and svc2.outbox.error.startswith("DatabaseError: file is not a database; no new one")
    assert (d / "alarm_outbox.db").read_bytes() == b"garbage" * 100
    await svc2.stop()


def test_outbox_memory_fallback_keeps_unsent_rows(tmp_path):
    ob = AlarmOutbox(tmp_path / "unused.db", max_rows=3)
    ob._fail("OSError: [Errno 30] Read-only file system", salvage=False)
    for i in range(5):
        ob.add({"id": str(i), "ts": 1.0})
    assert ob.count() == 3 and [e["id"] for _, e in ob.peek(2)] == ["2", "3"]  # старые — за пределом max_rows
    rows = ob.peek(2)
    ob.add({"id": "5", "ts": 1.0})
    ob.drop(rows)
    assert [e["id"] for _, e in ob.peek(10)] == ["4", "5"]
    assert not (tmp_path / "unused.db").exists()


async def test_core_starts_the_rest_when_the_alarm_cannot(data_dir, monkeypatch):
    """Сбой старта охраны не оставляет без отметок, fnav-watch и уровня (раньше — один try на всех)."""
    started: list[str] = []

    async def boom(self, background: bool = True) -> None:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(AlarmService, "start", boom)
    for cls, name in ((MarkLog, "marks"), (VisionClient, "vision"), (LevelService, "level")):
        async def spy(self, background: bool = True, _orig=cls.start, _name=name) -> None:
            started.append(_name)
            await _orig(self, background=background)

        monkeypatch.setattr(cls, "start", spy)
    async with running_app() as c:
        assert started == ["marks", "vision", "level"] and c.app.state.core.started


async def test_core_with_a_broken_outbox_file(data_dir):
    (data_dir / "alarm_outbox.db").write_bytes(b"garbage" * 1000)
    async with running_app() as c:
        core = c.app.state.core
        assert core.alarm.outbox.error is None and list(data_dir.glob("alarm_outbox.db.bad-*"))
        await login(c)
        st = (await c.post("/api/alarm/arm")).json()
        assert st["state"] == "arming" and st["storage_error"] is None


# ── поставили / сняли с охраны: журнал и push ──


def test_arm_disarm_texts():
    ts = time.mktime((2026, 10, 27, 14, 3, 0, 0, 0, -1))
    e = make_event("armed", {"source": "keypad", "delay_s": 60}, ts)
    assert (e.title, e.body, e.url) == ("Alarm armed", "Armed with the keypad at 14:03, active in 60 s", "#/alarm")
    assert make_event("armed", {"source": "web", "delay_s": 0}, ts).body == "Armed from the app at 14:03"
    e = make_event("disarmed", {"source": "web", "was": "armed"}, ts)
    assert (e.title, e.body) == ("Alarm disarmed", "Disarmed from the app at 14:03")
    e = make_event("disarmed", {"source": "keypad", "was": "triggered"}, ts)
    assert e.body == "Disarmed with the keypad at 14:03 while the alarm was going off"
    assert make_event("disarmed", {}, ts).body == "Disarmed at 14:03"


async def test_arm_disarm_journal_and_push(tmp_path):
    """Клавиша — push всегда (без паузы между push); из админки — только снятие посреди тревоги. Не тревога: без
    сирены, аварийки, защиты записи и снимков."""
    relay = FakeRelay()
    protected: list[int] = []

    async def protect():
        protected.append(1)
        return {"segment": "x.ts"}

    svc, clk, cfg = make_alarm(tmp_path, arm_delay_s=0, push_cooldown_s=600, protect=protect,
                               hazard=HazardSettings(enabled=True),
                               relay=RelayConfig("http://relay", "tok"), transport=httpx.MockTransport(relay))
    await svc.start(background=False)
    await svc.arm()  # из админки
    await svc.disarm()
    assert [(e.kind, e.data["source"]) for e in svc.events] == [("disarmed", "web"), ("armed", "web")]
    assert svc.outbox.count() == 0 and svc.seq == 2  # хозяин сам — без push
    assert await svc.toggle() is True  # клавиша
    assert await svc.toggle() is False
    assert await svc.toggle() is True
    assert svc.state == "armed" and svc.outbox.count() == 3  # пауза push_cooldown_s их не глотает
    assert not svc.hazard.active and not svc.siren.sounding and protected == []
    await svc._fire(Detection("impact", {"peak_g": 0.5}))
    assert protected == [1] and svc.hazard.active
    clk.tick(5)
    st = await svc.disarm()  # из админки, но посреди тревоги — push
    assert st.state == "off" and not svc.hazard.active
    assert svc.events[0].data == {"source": "web", "was": "triggered"}
    assert await svc.send_once() == 5
    kinds = [(e["kind"], e["data"]["source"]) for e in relay.bodies[-1]["events"] if e["kind"] != "impact"]
    assert kinds == [("armed", "keypad"), ("disarmed", "keypad"), ("armed", "keypad"), ("disarmed", "web")]
    assert relay.bodies[-1]["events"][-1]["body"].endswith("while the alarm was going off")
    assert [e.kind for e in svc.events if "snapshot" in e.data or "segment" in e.data] == ["impact"]
    cfg["s"] = cfg["s"].model_copy(update={"arm_delay_s": 30})
    await svc.toggle()  # с задержкой: в журнале — через сколько начнёт следить
    assert svc.state == "arming" and svc.events[0].data == {"source": "keypad", "delay_s": 30}
    await svc.arm()  # уже включена — без записи
    assert svc.events[0].kind == "armed" and svc.seq == 8
    await svc.stop()
