"""Удар на ходу (docs/contracts/camera.md «Удар на ходу»): MPU-6050 (fnav/core/imu.py) → ФВЧ ~3 Гц по каждой оси →
|a_hp| ≥ порога хотя бы MIN_SAMPLES отсчётов подряд, пока машина едет (по GPS) → защитить текущие записи обеих камер
(/protect-current: у границы сегмента — и соседний), сигнал зуммера, метка «impact» на шкале записей.

Почему ФВЧ: тяжесть, торможение, повороты, подъёмы и раскачка кузова на подвеске (< 2 Гц) — не удар, а на ходу они дают
десятые доли g постоянно. Ямы и лежачие полицейские в фургоне — короткие всплески 0,3–0,8 g; порог по умолчанию 1,5 g
— с запасом над ними. Одиночный выброс (сбой чтения) после фильтра — один отсчёт, а удар (фильтр датчика 94 Гц,
200 отсчётов/с) — несколько подряд.

Предел датчика: акселерометр настроен на ±2 g по оси (fnav/sensors/mpu6050.py), удар сильнее упирается в него. Резкая
ступенька до 2 g после ФВЧ — ~1,8 g, а импульс с фронтом 20–30 мс — только ~1,6 g (замер на синтетике: 3,5 g за
120 мс → 1,67 g при 3 Гц, 1,41 g при 5 Гц — поэтому срез 3 Гц, а не выше). Чтобы не зависеть от этого, ось, которая
сейчас лежит горизонтально (её доля тяжести < HORIZONTAL_G), упёршаяся в предел, — тоже превышение (если порог не выше
предела): 2 g вбок или вдоль — это уже не торможение и не яма (у ям — вертикаль, по ней не считаем).

«Едем» — скорость GPS ≥ MOVING_MPS сейчас или не дольше MOVING_HOLD_S назад (удар сзади на светофоре, в пробке); нет
скорости (тоннель, нет фикса) — прежнее «едем» держится NO_FIX_HOLD_S. На стоянке удары — дело охраны (alarm.py).

Журнал ударов: data_dir/impacts.jsonl — строка JSON на удар {ts, peak_g, speed_mps, lat, lon, segment, segment_rear},
последние IMPACTS_MAX; оборванная последняя строка при чтении пропускается.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from fnav.core.camera import MOVING_MPS
from fnav.core.config import atomic_write_text
from fnav.core.imu import ImuService
from fnav.core.util import cancel_tasks, spawn
from fnav.models import GpsFix, ImpactSettings
from fnav.sensors.mpu6050 import LSB_PER_G, Vec

log = logging.getLogger("fnav.core.impact")

HP_HZ = 3.0  # частота среза ФВЧ, Гц
MIN_SAMPLES = 2  # столько отсчётов подряд выше порога — удар (одиночный выброс — нет)
PEAK_WINDOW_S = 1.0  # сила удара — максимум |a_hp| за столько от первого превышения
FULL_SCALE_G = 32768 / LSB_PER_G  # предел датчика по оси (±2 g)
SAT_G = 0.98 * FULL_SCALE_G  # ось упёрлась в предел
HORIZONTAL_G = 0.5  # доля тяжести меньше — ось лежит горизонтально (предел по ней — удар)
GRAVITY_TAU_S = 2.0  # тяжесть по осям (для «горизонтально») — EMA
MOVING_HOLD_S = 60.0  # остановились — ещё столько «едем»
NO_FIX_HOLD_S = 300.0  # скорости нет — прежнее «едем» держится столько
DRIVE_EVERY_S = 1.0
IMPACTS_MAX = 500
EPS = 1e-9


@dataclass
class Hit:
    kind: str  # "hit" — первое превышение (защищать сразу), "peak" — через PEAK_WINDOW_S: сила удара
    ts: float  # unix-время первого превышения
    g: float


class ImpactDetector:
    """Удар по отсчётам акселерометра (зовётся из потока чтения). Время — по счёту отсчётов; unix-время отсчёта —
    от t_end (время последнего отсчёта пачки).

    ФВЧ первого порядка по осям: y = α·(y + x − x_prev), α = RC / (RC + dt), RC = 1 / (2π·HP_HZ). Превышение — |y| ≥
    порога или горизонтальная ось в пределе датчика (сила тогда — не меньше предела); удар — превышение MIN_SAMPLES
    отсчётов подряд, пока active (едем): сразу "hit", через PEAK_WINDOW_S — "peak" с максимумом силы. Следующий удар —
    не раньше cooldown_s после прошлого."""

    def __init__(self, hp_hz: float = HP_HZ, min_samples: int = MIN_SAMPLES) -> None:
        self.hp_hz = hp_hz
        self.min_samples = min_samples
        self.threshold_g = 1.5
        self.cooldown_s = 30.0
        self.reset()

    def reset(self) -> None:
        self._prev: tuple[float, float, float] | None = None
        self._y = [0.0, 0.0, 0.0]
        self._g = [0.0, 0.0, 0.0]  # тяжесть по осям (EMA)
        self._n = 0  # отсчётов с начала
        self._over = 0  # подряд выше порога
        self._run_max = 0.0  # максимум силы среди них
        self._next_ok = -math.inf  # раньше этого (с, по счёту отсчётов) удара нет
        self._peak_t0: float | None = None  # окно силы удара: начало (по счёту отсчётов)
        self._peak_ts = 0.0
        self._peak = 0.0
        self.level = 0.0  # максимум силы (|a_hp| или предел) за последнюю пачку

    def configure(self, s: ImpactSettings) -> None:
        self.threshold_g, self.cooldown_s = s.threshold_g, float(s.cooldown_s)

    def feed(self, samples: list[Vec], dt: float, active: bool, t_end: float) -> list[Hit]:
        out: list[Hit] = []
        rc = 1.0 / (2.0 * math.pi * self.hp_hz)
        alpha = rc / (rc + dt)
        k_g = 1.0 - math.exp(-dt / GRAVITY_TAU_S)
        sat_counts = self.threshold_g <= FULL_SCALE_G  # порог выше предела: предел ещё не значит «выше порога»
        n = len(samples)
        level = 0.0
        for i, a in enumerate(samples):
            x = (float(a[0]), float(a[1]), float(a[2]))
            self._n += 1
            t = self._n * dt
            prev, y, g = self._prev, self._y, self._g
            self._prev = x
            if prev is None:
                g[:] = x
                continue
            sat = 0.0
            for k in range(3):
                y[k] = alpha * (y[k] + x[k] - prev[k])
                if abs(x[k]) >= SAT_G and abs(g[k]) < HORIZONTAL_G:
                    sat = FULL_SCALE_G
                g[k] += k_g * (x[k] - g[k])
            d = max(math.sqrt(y[0] * y[0] + y[1] * y[1] + y[2] * y[2]), sat if sat_counts else 0.0)
            level = max(level, d)
            if self._peak_t0 is not None:
                self._peak = max(self._peak, d)
                if t - self._peak_t0 >= PEAK_WINDOW_S - EPS:
                    out.append(Hit("peak", self._peak_ts, round(self._peak, 3)))
                    self._peak_t0 = None
            over = d >= self.threshold_g
            self._over = self._over + 1 if over else 0
            self._run_max = max(self._run_max, d) if over else 0.0  # сила с первого превышения подряд
            if active and self._over >= self.min_samples and t >= self._next_ok - EPS and self._peak_t0 is None:
                ts = t_end - (n - 1 - i) * dt - (self._over - 1) * dt  # первое превышение
                self._next_ok = t + self.cooldown_s
                self._peak_t0, self._peak_ts, self._peak = t, ts, self._run_max
                out.append(Hit("hit", ts, round(self._run_max, 3)))
        self.level = level
        return out


class DriveState:
    """Едем ли: скорость GPS ≥ MOVING_MPS — да; остановились — ещё MOVING_HOLD_S «да»; скорости нет (нет фикса) —
    прежнее решение ещё NO_FIX_HOLD_S (потом — как остановились)."""

    def __init__(self) -> None:
        self._moving_at: float | None = None  # когда последний раз ехали (monotonic)
        self._known_at: float | None = None  # когда последний раз знали скорость
        self._was_moving = False

    def update(self, speed_mps: float | None, now: float) -> bool:
        if speed_mps is not None:
            self._known_at = now
            self._was_moving = speed_mps >= MOVING_MPS
            if self._was_moving:
                self._moving_at = now
        elif self._was_moving and self._known_at is not None:
            self._moving_at = max(self._moving_at or now, min(now, self._known_at + NO_FIX_HOLD_S))
        return self._moving_at is not None and now - self._moving_at <= MOVING_HOLD_S + EPS


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _clean(m: object) -> dict | None:
    if not isinstance(m, dict) or _num(m.get("ts")) is None or _num(m.get("peak_g")) is None:
        return None
    out = {"ts": float(m["ts"]), "peak_g": float(m["peak_g"])}
    for k in ("speed_mps", "lat", "lon"):
        out[k] = _num(m.get(k))
    for k in ("segment", "segment_rear"):
        out[k] = m[k] if isinstance(m.get(k), str) else None
    return out


class ImpactService:
    def __init__(self, path: Path, get_settings: Callable[[], ImpactSettings], imu: ImuService, *,
                 get_fix: Callable[[], GpsFix], protect: Callable[[], Awaitable[dict]],
                 beep: Callable[[str], None], clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self.get_settings = get_settings
        self.get_fix = get_fix
        self.protect = protect
        self.beep = beep
        self.clock = clock
        self.mono = mono
        self.detector = ImpactDetector()
        self.drive = DriveState()
        self.moving = False  # решение «едем» (раз в секунду в event loop; поток датчика только читает)
        self.items: list[dict] = []  # удары по времени
        self._peaks: dict[float, asyncio.Future] = {}  # ts удара → сила (из потока датчика через PEAK_WINDOW_S)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._enabled = False
        self._rewrite_file = False  # в файле оборванная строка: дописывать после неё нельзя — склеится
        self.detector.configure(get_settings())
        imu.subscribe(self._on_samples)

    # ── жизненный цикл ──
    async def start(self, background: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._load)
        self.update_drive()
        if background:
            self._task = asyncio.create_task(self._drive_loop(), name="impact-drive")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        self._task = None

    async def _drive_loop(self) -> None:
        while True:
            await asyncio.sleep(DRIVE_EVERY_S)
            try:
                self.update_drive()
            except Exception:
                log.exception("impact: drive state")

    def update_drive(self) -> bool:
        fix = self.get_fix()
        speed = fix.speed_mps if fix.fix != "none" else None
        moving = self.drive.update(speed, self.mono())
        if moving != self.moving:
            log.info("impact: %s", "driving - watching for impacts" if moving else "stopped - not watching")
        self.moving = moving
        return moving

    # ── поток датчика ──
    def _on_samples(self, samples: list[Vec], dt: float) -> None:
        s = self.get_settings()
        if not s.enabled:
            if self._enabled:
                self.detector.reset()
            self._enabled = False
            return
        self._enabled = True
        self.detector.configure(s)
        hits = self.detector.feed(samples, dt, self.moving, self.clock())
        loop = self._loop
        if not hits or loop is None:
            return
        for hit in hits:
            with contextlib.suppress(RuntimeError):  # loop уже закрыт (остановка)
                loop.call_soon_threadsafe(self._on_hit, hit)

    # ── event loop ──
    def _on_hit(self, hit: Hit) -> None:
        if hit.kind == "peak":
            fut = self._peaks.pop(hit.ts, None)
            if fut is not None and not fut.done():
                fut.set_result(hit.g)
            return
        assert self._loop is not None
        self._peaks[hit.ts] = self._loop.create_future()
        spawn(self.handle(hit), name="impact")

    async def handle(self, hit: Hit) -> dict:
        """Удар на ходу: защитить записи обеих камер, сигнал; через PEAK_WINDOW_S — в журнал с силой удара."""
        fix = self.get_fix()
        speed = fix.speed_mps if fix.fix != "none" else None
        log.warning("impact: %.2f g while driving (%s km/h) - protecting the recordings", hit.g,
                    f"{speed * 3.6:.0f}" if speed is not None else "?")
        res: dict = {}
        try:
            res = await self.protect() or {}
            self.beep("double")
        except Exception as e:  # noqa: BLE001 — камер нет: метка всё равно, сигнал — «ошибка»
            log.warning("impact: cannot protect the recordings: %s", e)
            self.beep("long")
        fut = self._peaks.get(hit.ts)
        peak = hit.g
        if fut is not None:
            try:
                peak = max(peak, await asyncio.wait_for(fut, PEAK_WINDOW_S + 2.0))
            except TimeoutError:
                pass
            self._peaks.pop(hit.ts, None)
        rec = {"ts": round(hit.ts, 3), "peak_g": round(peak, 2),
               "speed_mps": round(speed, 2) if speed is not None else None,
               "lat": fix.lat if fix.fix != "none" else None, "lon": fix.lon if fix.fix != "none" else None,
               "segment": res.get("segment"), "segment_rear": res.get("segment_rear")}
        self.items.append(rec)
        self.items.sort(key=lambda m: m["ts"])
        rewrite = self._rewrite_file or len(self.items) > IMPACTS_MAX
        del self.items[:-IMPACTS_MAX]
        try:
            await asyncio.to_thread(self._rewrite if rewrite else self._append, rec)
            self._rewrite_file = False
        except OSError as e:
            log.warning("impact: cannot write %s: %s", self.path, e)
        return rec

    # ── журнал ──
    def _load(self) -> None:
        items: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    if not ln.endswith("\n"):
                        self._rewrite_file = True
                    try:
                        m = _clean(json.loads(ln))
                    except ValueError:  # оборванная строка (питание пропало при записи)
                        self._rewrite_file = True
                        continue
                    if m is not None:
                        items.append(m)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("impact: cannot read %s: %s", self.path, e)
        items.sort(key=lambda m: m["ts"])
        self.items = items[-IMPACTS_MAX:]

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _rewrite(self, _rec: dict) -> None:
        atomic_write_text(self.path, "".join(json.dumps(m, separators=(",", ":")) + "\n" for m in self.items))

    def between(self, t0: float, t1: float, camera: str | None = None) -> list[dict]:
        """Удары в [t0, t1] — метки «impact» на шкале записей (удар один на машину: у записей обеих камер)."""
        lo = bisect.bisect_left(self.items, t0, key=_ts)
        hi = bisect.bisect_right(self.items, t1, key=_ts)
        return [{"ts": m["ts"], "kind": "impact", "camera": camera or "front", "dur_s": 0.0, "peak_g": m["peak_g"]}
                for m in self.items[lo:hi]]


def _ts(m: dict) -> float:
    return m["ts"]
