"""Удар на ходу (fnav/core/impact.py): ФВЧ + порог на синтетике (яма / удар), едем / стоим, защита записей, метки."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app

from fnav import paths
from fnav.camera.tsindex import SidecarWriter
from fnav.core.impact import (
    MOVING_HOLD_S,
    NO_FIX_HOLD_S,
    PEAK_WINDOW_S,
    DriveState,
    ImpactDetector,
    ImpactService,
)
from fnav.core.mockcam import _demo_ts
from fnav.models import CameraStatus, GpsFix, ImpactSettings

RATE = 200.0
DT = 1.0 / RATE
T0 = 1_792_507_200.0
FULL = 2.0  # предел датчика по оси (±2 g)


# ── синтетика: 200 отсчётов/с, датчик платой вверх (z — вверх), шум 0,01 g, предел ±2 g ──


def base(seconds: float, seed: int = 1) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.gauss(0, 0.01), rng.gauss(0, 0.01), 1.0 + rng.gauss(0, 0.01)] for _ in range(int(seconds * RATE))]


def add(s: list[list[float]], at_s: float, fn, dur_s: float) -> list[list[float]]:
    start = int(at_s * RATE)
    for i in range(int(dur_s * RATE)):
        if start + i < len(s):
            d = fn(i * DT)
            for k in range(3):
                s[start + i][k] += d[k]
    return s


def clip(s: list[list[float]]) -> list[tuple[float, float, float]]:
    return [(max(-FULL, min(FULL, a[0])), max(-FULL, min(FULL, a[1])), max(-FULL, min(FULL, a[2]))) for a in s]


def pothole(t: float):
    """Колесо в яме: затухающее 12 Гц до 0,8 g по вертикали (и немного вдоль/вбок)."""
    e = math.exp(-t / 0.08)
    return (0.25 * e * math.sin(2 * math.pi * 12 * t), 0.1 * e * math.sin(2 * math.pi * 9 * t),
            0.8 * e * math.sin(2 * math.pi * 12 * t + 0.3))


def body_bounce(t: float):
    return (0.0, 0.0, 0.3 * math.sin(2 * math.pi * 1.5 * t))


def speed_bump(t: float):
    if t < 0.1:
        return (0.0, 0.0, 0.8 * math.sin(math.pi * t / 0.1))
    if t < 0.25:
        return (0.0, 0.0, -0.6 * math.sin(math.pi * (t - 0.1) / 0.15))
    return (0.0, 0.0, 0.0)


def washboard(t: float):
    return (0.0, 0.2 * math.sin(2 * math.pi * 15 * t), 0.6 * math.sin(2 * math.pi * 15 * t))


def braking(t: float):
    """Торможение 0,8 g: нарастает за 0,4 с, держится, отпускается за 0,4 с (всего 3 с)."""
    return (-0.8 * min(1.0, t / 0.4, max(0.0, (3.0 - t) / 0.4)), 0.0, 0.0)


def crash(t: float):
    """Удар спереди: 3,5 g за 120 мс вдоль (датчик упирается в 2 g), рывок вверх, потом колебание кузова."""
    x = -3.5 * math.sin(math.pi * t / 0.12) if t < 0.12 else \
        0.3 * math.exp(-(t - 0.12) / 0.1) * math.sin(2 * math.pi * 8 * (t - 0.12))
    z = 1.0 * math.sin(math.pi * t / 0.06) if t < 0.06 else 0.0
    return (x, 0.2 * math.sin(math.pi * min(t, 0.12) / 0.12), z)


def run(det: ImpactDetector, samples, *, active: bool = True, batch: int = 20):
    out = []
    for i in range(0, len(samples), batch):
        chunk = samples[i:i + batch]
        out += det.feed(chunk, DT, active, T0 + (i + len(chunk) - 1) * DT)
    return out


def detector(threshold: float = 1.5, cooldown: float = 30.0) -> ImpactDetector:
    d = ImpactDetector()
    d.configure(ImpactSettings(threshold_g=threshold, cooldown_s=int(cooldown)))
    return d


# ── обнаружение ──


def test_road_is_not_an_impact():
    """Яма с раскачкой кузова, лежачий полицейский (оба моста), стиральная доска, торможение — ниже 1,5 g."""
    s = base(8)
    add(s, 0.5, body_bounce, 1.5)
    add(s, 0.6, pothole, 0.5)
    add(s, 2.5, speed_bump, 0.3)
    add(s, 2.8, speed_bump, 0.3)
    add(s, 3.5, washboard, 1.0)
    add(s, 4.8, braking, 3.0)
    det = detector()
    levels = []
    for i in range(0, len(s), 20):
        assert det.feed(clip(s[i:i + 20]), DT, True, T0) == []
        levels.append(det.level)
    assert 0.4 < max(levels) < 1.0  # ямы видны (0,6–0,7 g), но далеко до порога
    assert run(detector(threshold=1.0), clip(s)) == []  # и порог 1 g ещё с запасом


def test_crash_while_driving_is_an_impact_at_its_first_sample():
    s = base(3)
    add(s, 1.0, crash, 0.5)
    hits = run(detector(), clip(s))
    assert [h.kind for h in hits] == ["hit", "peak"]
    hit, peak = hits
    assert hit.ts == pytest.approx(T0 + 1.0, abs=0.03)  # время — первого превышения, а не конца пачки
    assert peak.ts == hit.ts and peak.g >= hit.g >= 1.5
    assert peak.g == pytest.approx(FULL, abs=0.15)  # датчик в пределе: сила — не меньше предела


def test_clipped_crash_counts_even_when_the_filter_alone_would_miss_it():
    """3,5 g за 120 мс в пределе ±2 g: после ФВЧ 3 Гц — ~1,67 g; порог 1,8 g берёт только «ось в пределе»."""
    s = base(3)
    add(s, 1.0, crash, 0.5)
    assert [h.kind for h in run(detector(threshold=1.8), clip(s))] == ["hit", "peak"]
    # вертикаль в пределе (яма в 1 g: z = 2 g) — не удар: по вертикальной оси предел не считается
    s = base(3)
    add(s, 1.0, lambda t: (0.0, 0.0, 1.2 * math.sin(math.pi * t / 0.1)), 0.1)
    assert run(detector(threshold=1.8), clip(s)) == []
    # порог выше предела: «ось в пределе» ещё не «выше порога»
    s = base(3)
    add(s, 1.0, crash, 0.5)
    assert run(detector(threshold=2.5), clip(s)) == []


def test_strength_counts_from_the_first_sample_over():
    """Как толкает ненастоящий датчик --mock (FakeAccel.inject_impact): 3 g, −1,8 g, … по отсчётам — сила с первого
    отсчёта выше порога (≈ 2,7 g), а не с того, на котором удар подтвердился."""
    s = base(2.5)
    for j, k in enumerate((1.0, -0.6, 0.35, -0.2, 0.1)):
        s[200 + j][0] += 3.0 * k
    hit, peak = run(detector(), s)  # без предела ±2 g (как у ненастоящего датчика)
    assert hit.g == pytest.approx(2.74, abs=0.05) and peak.g == hit.g
    assert hit.ts == pytest.approx(T0 + 200 * DT, abs=1e-6)


def test_parked_crash_is_not_watched():
    s = base(3)
    add(s, 1.0, crash, 0.5)
    assert run(detector(), clip(s), active=False) == []


def test_single_glitch_is_not_an_impact():
    s = base(2)
    s[200] = [2.0, -2.0, -2.0]  # один битый отсчёт
    assert run(detector(), clip(s)) == []


def test_cooldown_between_impacts():
    s = base(12)
    add(s, 1.0, crash, 0.5)
    add(s, 4.0, crash, 0.5)
    add(s, 9.0, crash, 0.5)
    assert [round(h.ts - T0) for h in run(detector(cooldown=5), clip(s)) if h.kind == "hit"] == [1, 9]
    assert [round(h.ts - T0) for h in run(detector(cooldown=30), clip(s)) if h.kind == "hit"] == [1]


def test_drive_state():
    d = DriveState()
    assert d.update(None, 0) is False  # скорость не известна с самого начала — не едем
    assert d.update(0.2, 1) is False
    assert d.update(10.0, 2) is True
    assert d.update(0.0, 30) is True  # остановились (светофор) — ещё «едем»
    assert d.update(0.0, 2 + MOVING_HOLD_S + 1) is False
    assert d.update(12.0, 100) is True
    assert d.update(None, 200) is True  # тоннель: скорости нет — прежнее «едем»
    assert d.update(None, 100 + NO_FIX_HOLD_S + MOVING_HOLD_S - 1) is True
    assert d.update(None, 100 + NO_FIX_HOLD_S + MOVING_HOLD_S + 1) is False
    p = DriveState()
    p.update(0.0, 0)
    assert p.update(None, 10) is False  # стояли — без фикса так и стоим


# ── сервис ──


class FakeImu:
    def __init__(self) -> None:
        self.cb = None

    def subscribe(self, cb) -> None:
        self.cb = cb


def fix(speed: float | None, lat: float = 52.2297, lon: float = 21.0118) -> GpsFix:
    if speed is None:
        return GpsFix(fix="none")
    return GpsFix(fix="3d", lat=lat, lon=lon, speed_mps=speed)


async def make_service(tmp_path, *, speed=15.0, settings=None, protect_fails=False):
    imu = FakeImu()
    state = {"fix": fix(speed), "settings": settings or ImpactSettings()}
    calls, beeps = [], []

    async def protect():
        calls.append(1)
        if protect_fails:
            raise RuntimeError("Camera service (fnav-camera) is not responding")
        return {"ok": True, "segment": "2026-10-20/2026-10-20_16-40-00.ts",
                "segment_rear": "rear/2026-10-20/2026-10-20_16-40-03.ts"}

    clock = {"t": T0}
    svc = ImpactService(tmp_path / "impacts.jsonl", lambda: state["settings"], imu, get_fix=lambda: state["fix"],
                        protect=protect, beep=beeps.append, clock=lambda: clock["t"])
    await svc.start(background=False)
    return svc, imu, state, calls, beeps, clock


async def feed(imu, svc, samples, clock, batch=20):
    """Как поток датчика: пачками по 0,1 с; события — через event loop."""
    for i in range(0, len(samples), batch):
        chunk = samples[i:i + batch]
        clock["t"] = T0 + (i + len(chunk) - 1) * DT
        imu.cb(chunk, DT)
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0.01)


def crash_samples(seconds: float = 1.0 + PEAK_WINDOW_S + 1.0):
    s = base(seconds)
    add(s, 0.5, crash, 0.5)
    return clip(s)


async def test_impact_while_driving_protects_beeps_and_marks(tmp_path):
    svc, imu, _state, calls, beeps, clock = await make_service(tmp_path)
    assert svc.moving is True
    await feed(imu, svc, crash_samples(), clock)
    assert calls == [1] and beeps == ["double"]
    (rec,) = svc.items
    assert rec["ts"] == pytest.approx(T0 + 0.5, abs=0.03)
    assert rec["peak_g"] >= 1.5 and rec["speed_mps"] == 15.0 and rec["lat"] == 52.2297
    assert rec["segment"] == "2026-10-20/2026-10-20_16-40-00.ts"
    assert rec["segment_rear"] == "rear/2026-10-20/2026-10-20_16-40-03.ts"
    # метка — у записей обеих камер
    (m,) = svc.between(T0, T0 + 10, "rear")
    assert m == {"ts": rec["ts"], "kind": "impact", "camera": "rear", "dur_s": 0.0, "peak_g": rec["peak_g"]}
    assert svc.between(T0 + 1, T0 + 10, "front") == []
    # журнал на карте: переживает перезапуск
    lines = (tmp_path / "impacts.jsonl").read_text().splitlines()
    assert [json.loads(x) for x in lines] == [rec]
    again, *_ = await make_service(tmp_path)
    assert again.items == [rec]


async def test_pothole_parked_or_disabled_do_nothing(tmp_path):
    svc, imu, state, calls, beeps, clock = await make_service(tmp_path)
    s = base(3)
    add(s, 0.5, pothole, 0.5)
    await feed(imu, svc, clip(s), clock)
    assert calls == [] and svc.items == []
    state["fix"] = fix(0.0)
    svc.drive = DriveState()  # стоим давно
    svc.update_drive()
    assert svc.moving is False
    await feed(imu, svc, crash_samples(), clock)
    assert calls == [] and svc.items == []
    state["settings"] = ImpactSettings(enabled=False)
    state["fix"] = fix(20.0)
    svc.update_drive()
    await feed(imu, svc, crash_samples(), clock)
    assert calls == [] and beeps == [] and not (tmp_path / "impacts.jsonl").exists()


async def test_no_camera_still_marks_with_an_error_beep(tmp_path):
    svc, imu, _state, calls, beeps, clock = await make_service(tmp_path, protect_fails=True)
    await feed(imu, svc, crash_samples(), clock)
    assert calls == [1] and beeps == ["long"]
    (rec,) = svc.items
    assert rec["segment"] is None and rec["peak_g"] >= 1.5


async def test_torn_last_line_is_dropped_and_the_file_rewritten(tmp_path):
    good = {"ts": T0 - 100, "peak_g": 1.9, "speed_mps": 20.0, "lat": 52.0, "lon": 21.0, "segment": "a/b.ts",
            "segment_rear": None}
    (tmp_path / "impacts.jsonl").write_text(json.dumps(good) + "\n" + '{"ts": 17925', encoding="utf-8")
    svc, imu, _state, _calls, _beeps, clock = await make_service(tmp_path)
    assert svc.items == [good]
    await feed(imu, svc, crash_samples(), clock)
    lines = [json.loads(x) for x in (tmp_path / "impacts.jsonl").read_text().splitlines()]
    assert lines == svc.items and len(lines) == 2


# ── метки в списке записей ──


async def test_impact_marks_in_recordings_of_both_cameras(data_dir):
    root = paths.recordings_dir()
    t0 = T0
    for rel in ("2026-10-20/2026-10-20_16-40-00.ts", "rear/2026-10-20/2026-10-20_16-40-00.ts"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_demo_ts(seconds=20))
        SidecarWriter(p, start_ts=t0).close()
        os.utime(p, (t0 + 60, t0 + 60))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/status":
            return httpx.Response(200, json=CameraStatus(state="recording", recording=True,
                                                         current_segment=None).model_dump(mode="json"))
        return httpx.Response(404)

    cam = FakeUpstream(handler)
    async with running_app(make_opts(local_transport=cam.transport)) as c:
        await login(c)
        core = c.app.state.core
        core.impact.items = [{"ts": t0 + 12.34, "peak_g": 1.87, "speed_mps": 14.0, "lat": 52.0, "lon": 21.0,
                              "segment": None, "segment_rear": None}]
        items = {i["path"]: i for i in (await c.get("/api/recordings")).json()}
        for rel in ("2026-10-20/2026-10-20_16-40-00.ts", "rear/2026-10-20/2026-10-20_16-40-00.ts"):
            assert items[rel]["marks"] == [{"kind": "impact", "ts": t0 + 12.34, "offset_s": 12.3, "dur_s": 0.0,
                                            "peak_g": 1.87}], rel
        one = (await c.get("/api/recordings/item/rear/2026-10-20/2026-10-20_16-40-00.ts")).json()
        assert one["marks"][0]["kind"] == "impact"
