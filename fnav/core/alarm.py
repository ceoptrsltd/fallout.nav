"""Охрана (docs/contracts/alarm.md): удар и наклон по MPU-6050, сдвиг по GPS; состояния off → arming → armed →
triggered → armed; журнал событий; действия — защита текущей записи камеры, сирена через колонку Bluetooth (mpv
радио), push через fnav-relay (outbox SQLite, как у журнала позиций).

Отсчёты акселерометра приходят в потоке чтения (fnav/core/imu.py) — обнаружение (Detector) считается там же,
в event loop попадают только готовые события и величины.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import re
import threading
import time
import uuid
import wave
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import numpy as np
from pydantic import ValidationError

from fnav.core.config import atomic_write_text
from fnav.core.imu import ImuService
from fnav.core.tracking import TOKEN_HEADER, Outbox, RelayConfig
from fnav.core.util import cancel_tasks, spawn
from fnav.models import (
    AlarmEvent,
    AlarmHazard,
    AlarmLast,
    AlarmRelay,
    AlarmSensor,
    AlarmSettings,
    AlarmStatus,
    AlarmTelemetry,
    AlarmVision,
    GpsFix,
    HazardSettings,
)
from fnav.sensors.hazard import FakeHazard, HazardOutput, output_key
from fnav.sensors.mpu6050 import Vec

if TYPE_CHECKING:
    from fnav.core.radio import RadioService

log = logging.getLogger("fnav.core.alarm")

GRAVITY_TAU_S = 1.0  # g_lp — тяжесть
SLOW_TAU_S = 5.0  # g_slow — для наклона
IMPACT_WINDOW_S = 1.0  # пик удара — за 1 с от первого превышения
IMPACT_GAP_S = 2.0  # следующий удар — не раньше
BASE_S = 2.0  # база наклона — средний g_lp за 2 с
TILT_HOLD_S = 5.0  # наклон должен держаться дольше
MOVE_MAX_HDOP = 5.0
MOVE_MAX_AGE_S = 5.0
MOVE_REPEAT_S = 300.0
TRIGGERED_HOLD_S = 30.0  # triggered — пока звучит сирена и ещё столько после последнего события
JOURNAL_MAX = 200
STATUS_EVENTS = 100
TICK_S = 0.25
GPS_EVERY_S = 1.0
HEARTBEAT_S = 60.0
RETRY_MIN_S = 5.0
OUTBOX_MAX_ROWS = 1000
BATCH = 50
SIREN_RATE = 16000
SIREN_SECONDS = 8.0  # файл короткий, играет по кругу siren_s секунд
EPS = 1e-9

HAZARD_TEST_S = 3.0
HAZARD_BLINK_ON_S, HAZARD_BLINK_OFF_S = 0.7, 0.5  # подтверждение Arm/Disarm

DETECTED = ("armed", "triggered")  # события и сирена — только в этих состояниях
VISION_KINDS = ("approach", "face", "loiter")  # от камеры (fnav-watch): без сирены — push, аварийка, защита записи
SNAPSHOTS_MAX = 100  # событий со снимками камер (alarm_snaps/<id>.jpg, задняя — <id>-rear.jpg)


_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _snap_name(event_id: str, camera: str) -> str:
    return f"{event_id}-rear.jpg" if camera == "rear" else f"{event_id}.jpg"


# ───────────────────────── обнаружение ─────────────────────────


@dataclass
class Detection:
    kind: str  # "impact" | "tilt" | "move" | "approach" | "face" | "loiter"
    data: dict = field(default_factory=dict)


def _angle_deg(a: Vec | list[float], b: Vec | list[float]) -> float:
    cx, cy, cz = a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]
    return math.degrees(math.atan2(math.sqrt(cx * cx + cy * cy + cz * cz), a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))


class Detector:
    """Удар и наклон по отсчётам акселерометра. Зовётся из потока чтения; время — по счёту отсчётов.

    g_lp — тяжесть (EMA τ = 1 с), d = |a − g_lp| в g. Удар: d ≥ impact_g → пик d за 1 с → событие; следующий —
    не раньше чем через 2 с. Наклон: база — средний g_lp за 2 с после rebase(); g_slow — EMA τ = 5 с; угол между
    g_slow и базой ≥ tilt_deg дольше 5 с → событие, база := текущая тяжесть (g_lp)."""

    def __init__(self) -> None:
        self.impact_g = 0.15
        self.tilt_enabled = True
        self.tilt_deg = 3.0
        self._lock = threading.Lock()
        self._n = 0  # отсчётов с начала
        self._dt = 0.005
        self.g_lp: list[float] | None = None
        self.g_slow: list[float] | None = None
        self.base: Vec | None = None
        self.angle: float | None = None  # угол g_slow от базы, °
        self._levels: dict[str, float | None] = {"telemetry": None, "status": None}
        self._impact_t0: float | None = None
        self._impact_peak = 0.0
        self._impact_next = -math.inf
        self._base_acc: list[float] | None = None
        self._base_n = 0
        self._over_since: float | None = None

    @property
    def t(self) -> float:
        return self._n * self._dt

    def configure(self, s: AlarmSettings) -> None:
        with self._lock:
            self.impact_g, self.tilt_enabled, self.tilt_deg = s.impact_g, s.tilt_enabled, s.tilt_deg

    def rebase(self) -> None:
        """Постановка на охрану (или старт во включённой): база наклона — по следующим 2 с."""
        with self._lock:
            self.base, self.angle, self._over_since = None, None, None
            self._base_acc, self._base_n = [0.0, 0.0, 0.0], 0

    def clear_base(self) -> None:
        with self._lock:
            self.base = self.angle = self._over_since = self._base_acc = None
            self._base_n = 0

    def take_level(self, key: str) -> float | None:
        """Максимум d с прошлого вызова с тем же key (у телеметрии и у GET /api/alarm — свои окна)."""
        with self._lock:
            v = self._levels.get(key)
            self._levels[key] = None
            return v

    def feed(self, samples: list[Vec], dt: float) -> list[Detection]:
        out: list[Detection] = []
        with self._lock:
            self._dt = dt
            k_lp = 1.0 - math.exp(-dt / GRAVITY_TAU_S)
            k_slow = 1.0 - math.exp(-dt / SLOW_TAU_S)
            peak: float | None = None
            for a in samples:
                self._n += 1
                g, s = self.g_lp, self.g_slow
                if g is None or s is None:
                    self.g_lp, self.g_slow = list(a), list(a)
                    continue
                d = math.sqrt((a[0] - g[0]) ** 2 + (a[1] - g[1]) ** 2 + (a[2] - g[2]) ** 2)
                peak = d if peak is None else max(peak, d)
                for i in range(3):
                    g[i] += k_lp * (a[i] - g[i])
                    s[i] += k_slow * (a[i] - s[i])
                self._impact(d, out)
                self._tilt(out)
            if peak is not None:
                for key, v in self._levels.items():
                    self._levels[key] = peak if v is None else max(v, peak)
        return out

    def _impact(self, d: float, out: list[Detection]) -> None:
        t = self.t
        if self._impact_t0 is not None:
            self._impact_peak = max(self._impact_peak, d)
            if t - self._impact_t0 >= IMPACT_WINDOW_S - EPS:
                out.append(Detection("impact", {"peak_g": round(self._impact_peak, 3)}))
                self._impact_t0 = None
        elif d >= self.impact_g and t >= self._impact_next - EPS:
            self._impact_t0, self._impact_peak, self._impact_next = t, d, t + IMPACT_GAP_S

    def _tilt(self, out: list[Detection]) -> None:
        g, s = self.g_lp, self.g_slow
        assert g is not None and s is not None
        if self._base_acc is not None:  # копим базу
            for i in range(3):
                self._base_acc[i] += g[i]
            self._base_n += 1
            if self._base_n * self._dt < BASE_S - EPS:
                return
            n = self._base_n
            self.base = (self._base_acc[0] / n, self._base_acc[1] / n, self._base_acc[2] / n)
            self._base_acc = None
        if self.base is None:
            return
        ang = _angle_deg(s, self.base)
        self.angle = ang
        if ang < self.tilt_deg:
            self._over_since = None
        elif self._over_since is None:
            self._over_since = self.t
        elif self.t - self._over_since > TILT_HOLD_S and self.tilt_enabled:
            out.append(Detection("tilt", {"tilt_deg": round(ang, 1)}))
            self.base = (g[0], g[1], g[2])
            self._over_since = None


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))


def good_fix(fix: GpsFix) -> bool:
    return (fix.fix != "none" and fix.lat is not None and fix.lon is not None and fix.hdop is not None
            and fix.hdop <= MOVE_MAX_HDOP and (fix.age_s is None or fix.age_s <= MOVE_MAX_AGE_S))


class MoveWatch:
    """Сдвиг по GPS: точка отсчёта — первая хорошая точка (fix, HDOP ≤ 5) после постановки на охрану;
    расстояние ≥ move_m → событие; дальше — не чаще раза в 300 с и только если от последнего сообщённого места
    снова ≥ move_m."""

    def __init__(self) -> None:
        self.ref: tuple[float, float] | None = None
        self.last: tuple[float, float] | None = None
        self.last_ts: float | None = None

    def reset(self) -> None:
        self.ref = self.last = self.last_ts = None

    def to_dict(self) -> dict:
        return {"ref": list(self.ref) if self.ref else None, "last": list(self.last) if self.last else None,
                "last_ts": self.last_ts}

    @classmethod
    def from_dict(cls, d: Any) -> MoveWatch:
        m = cls()
        if isinstance(d, dict):
            with contextlib.suppress(TypeError, ValueError, IndexError):
                m.ref = (float(d["ref"][0]), float(d["ref"][1])) if d.get("ref") else None
                m.last = (float(d["last"][0]), float(d["last"][1])) if d.get("last") else None
                m.last_ts = float(d["last_ts"]) if d.get("last_ts") is not None else None
        return m

    def check(self, fix: GpsFix, move_m: float, now: float) -> Detection | None:
        if not good_fix(fix):
            return None
        cur = (float(fix.lat), float(fix.lon))  # type: ignore[arg-type]
        if self.ref is None:
            self.ref = cur
            return None
        if self.last is None:
            if haversine_m(self.ref, cur) < move_m:
                return None
        elif now - (self.last_ts or 0.0) < MOVE_REPEAT_S or haversine_m(self.last, cur) < move_m:
            return None
        self.last, self.last_ts = cur, now
        return Detection("move", {"distance_m": round(haversine_m(self.ref, cur)), "lat": round(cur[0], 7),
                                  "lon": round(cur[1], 7)})


def make_event(kind: str, data: dict, ts: float) -> AlarmEvent:
    """Тексты push и журнала (по-английски); время — местное устройства, HH:MM. События камеры с задней
    (data.camera == "rear") — свои тексты: «за фургоном»."""
    hm = time.strftime("%H:%M", time.localtime(ts))
    url = "#/alarm"
    rear = kind in VISION_KINDS and data.get("camera") == "rear"
    if kind == "impact":
        title, body = "Impact detected", f"Hit {data.get('peak_g', 0):.2f} g at {hm}"
    elif kind == "tilt":
        title, body = "Van tilted", f"Tilted {data.get('tilt_deg', 0):.1f}° at {hm}"
    elif kind == "move":
        title, body = "Van is moving", f"{data.get('distance_m', 0):.0f} m from the parking spot at {hm}"
        url = "#/tracking"
    elif kind == "approach" and rear:
        title, body = "Person approaching behind the van", f"Someone came close to the back of the van at {hm}"
    elif kind == "approach":
        title, body = "Person approaching", f"Someone came close to the van at {hm}"
    elif kind == "face" and rear:
        title, body = "Someone is looking into the rear camera", f"A face close to the rear camera at {hm}"
    elif kind == "face":
        title, body = "Someone is looking in", f"A face close to the windshield at {hm}"
    elif kind == "loiter":
        secs = float(data.get("held_s") or 0)
        if rear:
            title = "Someone is lingering behind the van"
            body = f"A person has stayed close to the back of the van for {secs:.0f} s at {hm}"
        else:
            title = "Someone is lingering at the van"
            body = f"A person has stayed close to the van for {secs:.0f} s at {hm}"
    else:
        title, body = "Test alarm", f"Alarm notifications work ({hm})"
    return AlarmEvent(id=uuid.uuid4().hex, ts=round(ts, 3), kind=kind, title=title, body=body, url=url,  # type: ignore[arg-type]
                      data=data)


# ───────────────────────── сирена ─────────────────────────


def siren_wav(rate: int = SIREN_RATE, seconds: float = SIREN_SECONDS, lo: float = 600.0, hi: float = 1400.0,
              period: float = 2.0) -> bytes:
    """Сирена «вой»: тон плавно поднимается lo → hi и опускается обратно за period, с нечётными обертонами (громче
    на маленькой колонке). WAV 16 бит моно; края смягчены — при повторе по кругу нет щелчка."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    f = lo + (hi - lo) * (1.0 - np.cos(2 * np.pi * t / period)) / 2
    ph = 2 * np.pi * np.cumsum(f) / rate
    x = np.sin(ph) + 0.35 * np.sin(3 * ph) + 0.15 * np.sin(5 * ph)
    x /= np.max(np.abs(x))
    fade = int(rate * 0.01)
    ramp = np.linspace(0.0, 1.0, fade)
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 0.9 * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class Siren:
    """Сирена через mpv радио на колонку Bluetooth (RadioService.play_alert / end_alert): громкость 100, потом
    радио возвращается к тому, что играло (станция, громкость), или к тишине. Колонки нет — пропускаем (в лог)."""

    def __init__(self, radio: RadioService | None, wav_path: Path,
                 allowed: Callable[[], bool] = lambda: True) -> None:
        self.radio = radio
        self.wav_path = wav_path
        self.allowed = allowed  # охрану могли выключить, пока сирена собиралась зазвучать
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Lock()

    @property
    def sounding(self) -> bool:
        return self._task is not None and not self._task.done()

    def ensure_wav(self) -> Path:
        """Свой WAV в data_dir (пишется один раз)."""
        with contextlib.suppress(OSError):
            if self.wav_path.stat().st_size == 44 + int(SIREN_RATE * SIREN_SECONDS) * 2:
                return self.wav_path
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.wav_path.with_suffix(".tmp")
        tmp.write_bytes(siren_wav())
        os.replace(tmp, self.wav_path)
        return self.wav_path

    async def start(self, seconds: float) -> bool:
        async with self._busy:
            if self.sounding:
                return True
            if not self.allowed():
                return False
            if self.radio is None:
                log.info("alarm: siren skipped - no radio player")
                return False
            try:
                path = await asyncio.to_thread(self.ensure_wav)
            except OSError as e:
                log.warning("alarm: siren skipped - cannot write %s: %s", self.wav_path, e)
                return False
            why = await self.radio.play_alert(str(path), volume=100)
            if why:
                log.info("alarm: siren skipped - %s", why)
                return False
            log.warning("alarm: siren on for %.0f s", seconds)
            self._task = spawn(self._hold(seconds), name="alarm-siren")
            return True

    async def _hold(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        assert self.radio is not None
        await asyncio.shield(self.radio.end_alert())  # отмена в этот момент не оставит радио «на сирене»
        log.info("alarm: siren off")

    async def stop(self) -> None:
        async with self._busy:
            t, self._task = self._task, None
            if t is None:
                return
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
                log.info("alarm: siren silenced")
            if self.radio is not None:
                await self.radio.end_alert()


class HazardLights:
    """Аварийка: RC-реле параллельно её кнопке (с фиксацией) замкнуто, пока мигаем (docs/contracts/alarm.md,
    «Аварийка»). Мигание длится до срока, новое событие его продлевает.
    """

    def __init__(self, get_cfg: Callable[[], HazardSettings],
                 open_output: Callable[[HazardSettings], HazardOutput] = FakeHazard,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.get_cfg = get_cfg
        self.open_output = open_output
        self.mono = mono
        self._out: HazardOutput | None = None
        self._key: tuple | None = None
        self.error: str | None = None
        self.lit = False  # реле замкнуто — аварийка горит
        self._until = 0.0  # monotonic: когда погасить
        self._task: asyncio.Task | None = None  # мигание по тревоге / тест
        self._blink: asyncio.Task | None = None  # подтверждение Arm/Disarm
        self._lock = asyncio.Lock()  # переключения реле — по одному

    @property
    def active(self) -> bool:
        return any(t is not None and not t.done() for t in (self._task, self._blink))

    def _output(self, cfg: HazardSettings) -> HazardOutput | None:
        """Открыть вывод (или заново — сменились вывод/сигнал); ошибка — в self.error."""
        key = output_key(cfg)
        if self._out is not None and key == self._key:
            return self._out
        self._close_output()
        try:
            self._out, self._key = self.open_output(cfg), key
            self.error = None
            log.info("alarm: hazard relay ready (pulses %d / %d us)", cfg.on_us, cfg.off_us)
        except Exception as e:  # noqa: BLE001 — нет gpiozero, вывод занят и т.п.
            if self.error != str(e):
                log.warning("alarm: hazard relay unavailable: %s", e)
            self.error = str(e)
        return self._out

    def _close_output(self) -> None:
        if self._out is not None:
            with contextlib.suppress(Exception):
                self._out.close()
        self._out, self._key, self.lit = None, None, False

    def apply(self, cfg: HazardSettings) -> None:
        """Настройки: включена — держать вывод открытым (у RC-реле — импульсы «выкл»), выключена — отпустить."""
        if self.active:
            return  # мигает (или тест) — вывод отпустим/переоткроем после
        if cfg.enabled:
            self._output(cfg)
        else:
            self._close_output()
            self.error = None

    async def _switch(self, on: bool) -> None:
        cfg = self.get_cfg()
        async with self._lock:
            out = self._output(cfg)
            if out is None or self.lit == on:
                return
            out.set(on)
            self.lit = on

    def flash(self, seconds: float) -> None:
        """Мигать seconds (уже мигает — продлить до now + seconds)."""
        self._until = max(self._until, self.mono() + seconds)
        if self._task is None or self._task.done():
            self._task = spawn(self._run(), name="alarm-hazard")

    async def _run(self) -> None:
        if self._blink is not None:
            await cancel_tasks(self._blink)
            self._blink = None
        try:
            await self._switch(True)
            if self.lit:
                log.warning("alarm: hazard lights on")
            else:
                log.warning("alarm: hazard lights skipped - %s", self.error or "relay unavailable")
            while (left := self._until - self.mono()) > 0:
                await asyncio.sleep(min(left, 1.0))
        finally:
            was = self.lit
            await asyncio.shield(self._switch(False))
            if was:
                log.info("alarm: hazard lights off")
            self._after()

    def _after(self) -> None:
        if not self.get_cfg().enabled:
            self._close_output()  # тест при выключенной аварийке — вывод не держим

    async def stop(self) -> None:
        self._until = 0.0
        await cancel_tasks(self._task, self._blink)
        self._task = self._blink = None
        await self._switch(False)
        self._after()

    def confirm(self, times: int) -> None:
        """Мигнуть times раз (Arm — 1, Disarm — 2), если не мигаем по тревоге."""
        if self._task is not None and not self._task.done():
            return
        if self._blink is not None:
            self._blink.cancel()
        self._blink = spawn(self._blinks(times), name="alarm-hazard-confirm")

    async def _blinks(self, times: int) -> None:
        try:
            for i in range(times):
                if i:
                    await asyncio.sleep(HAZARD_BLINK_OFF_S)
                await self._switch(True)
                await asyncio.sleep(HAZARD_BLINK_ON_S)
                await self._switch(False)
        finally:
            await asyncio.shield(self._switch(False))

    async def test(self) -> str | None:
        """Мигнуть HAZARD_TEST_S текущими настройками (даже выключенной). None — пошло, иначе — почему нет."""
        if self._output(self.get_cfg()) is None:
            return self.error or "hazard relay unavailable"
        self.flash(HAZARD_TEST_S)
        return None

    async def close(self) -> None:
        await self.stop()
        self._close_output()

    def status(self) -> AlarmHazard:
        return AlarmHazard(enabled=self.get_cfg().enabled, active=self.active, error=self.error)


# ───────────────────────── сервис ─────────────────────────


class AlarmService:
    def __init__(self, data_dir: Path, get_settings: Callable[[], AlarmSettings], imu: ImuService,
                 relay: RelayConfig, *, get_fix: Callable[[], GpsFix] | None = None,
                 protect: Callable[[], Awaitable[dict]] | None = None, radio: RadioService | None = None,
                 snapshot: Callable[[], Awaitable[bytes]] | None = None,
                 open_hazard: Callable[[HazardSettings], HazardOutput] = FakeHazard,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.state_path = data_dir / "alarm_state.json"
        self.events_path = data_dir / "alarm_events.json"
        self.get_settings = get_settings
        self.imu = imu
        self.relay = relay
        self.get_fix = get_fix
        self.protect = protect
        self.snapshot = snapshot
        # задняя камера (если есть): ставит runtime; выключена — функции отдают пусто
        self.protect_rear: Callable[[], Awaitable[dict]] | None = None
        self.snapshot_rear: Callable[[], Awaitable[bytes]] | None = None
        self.snap_dir = data_dir / "alarm_snaps"
        self.vision_status: Callable[[], AlarmVision] | None = None  # fnav/core/vision.py
        self.clock = clock
        self.mono = mono
        self.detector = Detector()
        self.move = MoveWatch()
        self.siren = Siren(radio, data_dir / "alarm_siren.wav", allowed=lambda: self.state in DETECTED)
        self.hazard = HazardLights(lambda: self.get_settings().hazard, open_hazard, mono=mono)
        self.outbox = Outbox(data_dir / "alarm_outbox.db", max_rows=OUTBOX_MAX_ROWS)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None  # создаётся при первой отправке (сервер может быть не настроен)
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fnav-alarm-io")  # записи файлов — по порядку
        self.state = "off"
        self.armed_at: float | None = None
        self._arm_deadline: float | None = None  # monotonic
        self._last_event_mono: float | None = None
        self.events: list[AlarmEvent] = []  # новые первыми
        self.seq = 0
        self._last_push: dict[str, float] = {}  # kind (события задней камеры — kind:rear) → monotonic
        self._pending = 0
        self._notify = get_settings().notify_offline
        self._kick = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sim_tilt = -1.0
        self.last_error: str | None = None
        self.last_sent: float | None = None
        self.detector.configure(get_settings())
        imu.subscribe(self._on_samples)

    # ── жизненный цикл ──
    async def start(self, background: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        await self._run_io(self._load)
        if self.state != "off":
            self.detector.rebase()  # база наклона — через 2 с после старта
            log.info("alarm: armed (restored after restart)")
        self._pending = await asyncio.to_thread(self.outbox.count)
        self.hazard.apply(self.get_settings().hazard)
        if background:
            self._tasks = [asyncio.create_task(self._tick_loop(), name="alarm-tick"),
                           asyncio.create_task(self._relay_loop(), name="alarm-relay")]

    async def stop(self) -> None:
        await cancel_tasks(*self._tasks)
        self._tasks.clear()
        await self.siren.stop()
        await self.hazard.close()
        with contextlib.suppress(Exception):
            await self._save_state()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await asyncio.to_thread(self.outbox.close)
        self._io.shutdown(wait=True)
        self._loop = None

    def apply_settings(self, s: AlarmSettings) -> None:
        self.detector.configure(s)
        self.hazard.apply(s.hazard)
        if s.notify_offline != self._notify:
            self._notify = s.notify_offline
            self._kick.set()

    # ── файлы ──
    async def _run_io(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._io, fn, *args)

    def _load(self) -> None:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if isinstance(d, dict):
            with contextlib.suppress(TypeError, ValueError):
                self.seq = int(d.get("seq") or 0)
            if d.get("on"):
                self.state = "armed"
                try:
                    self.armed_at = float(d["armed_at"]) if d.get("armed_at") else self.clock()
                except (TypeError, ValueError):
                    self.armed_at = self.clock()
            self.move = MoveWatch.from_dict(d.get("move"))
        try:
            raw = json.loads(self.events_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        events = []
        for e in raw if isinstance(raw, list) else []:
            with contextlib.suppress(ValidationError, TypeError):
                events.append(AlarmEvent.model_validate(e))
        self.events = events[:JOURNAL_MAX]

    async def _save_state(self) -> None:
        d = {"on": self.state != "off", "armed_at": self.armed_at, "seq": self.seq, "move": self.move.to_dict()}
        await self._run_io(atomic_write_text, self.state_path, json.dumps(d))

    async def _save_events(self) -> None:
        data = json.dumps([e.model_dump(mode="json") for e in self.events], ensure_ascii=False)
        await self._run_io(atomic_write_text, self.events_path, data)

    # ── состояние ──
    async def _changed(self) -> None:
        await self._save_state()
        self._kick.set()

    def _set_armed(self) -> None:
        self.state, self.armed_at, self._arm_deadline = "armed", self.clock(), None
        self._last_event_mono = None
        self.detector.rebase()
        self.move.reset()
        log.info("alarm: armed")

    async def arm(self) -> AlarmStatus:
        """off → arming (arm_delay_s) → armed; при arm_delay_s = 0 — сразу armed. Уже включена — ничего."""
        if self.state == "off":
            cfg = self.get_settings()
            if cfg.hazard.enabled and cfg.hazard.confirm:
                self.hazard.confirm(1)
            delay = cfg.arm_delay_s
            if delay <= 0:
                self._set_armed()
            else:
                self.state, self._arm_deadline, self.armed_at = "arming", self.mono() + delay, None
                log.info("alarm: arming in %d s", delay)
            await self._changed()
        return self.status()

    async def disarm(self) -> AlarmStatus:
        was = self.state
        self.state, self.armed_at, self._arm_deadline, self._last_event_mono = "off", None, None, None
        self.detector.clear_base()
        self.move.reset()
        await self.siren.stop()
        await self.hazard.stop()
        if was != "off":
            hz = self.get_settings().hazard
            if hz.enabled and hz.confirm:
                self.hazard.confirm(2)
            log.info("alarm: disarmed")
            await self._changed()
        return self.status()

    async def silence(self) -> AlarmStatus:
        """Сирена и аварийка замолкают, охрана остаётся."""
        await self.siren.stop()
        await self.hazard.stop()
        return self.status()

    async def hazard_test(self) -> str | None:
        """Мигнуть аварийкой 3 с текущими настройками. None — пошло, иначе — почему нет."""
        return await self.hazard.test()

    async def toggle(self) -> bool:
        """Клавиша «Alarm on/off»: off → arming/armed, иначе (и из triggered) → off. → включена ли теперь."""
        if self.state == "off":
            await self.arm()
            return True
        await self.disarm()
        return False

    async def poll(self) -> None:
        """Переходы по времени: arming → armed, triggered → armed (сирена молчит и 30 с без событий)."""
        now = self.mono()
        if self.state == "arming" and self._arm_deadline is not None and now >= self._arm_deadline:
            self._set_armed()
            await self._changed()
        elif (self.state == "triggered" and not self.siren.sounding
              and now - (self._last_event_mono or -math.inf) >= TRIGGERED_HOLD_S):
            self.state = "armed"
            log.info("alarm: back to armed")
            await self._changed()

    async def check_gps(self) -> None:
        cfg = self.get_settings()
        if self.state not in DETECTED or not cfg.move_enabled or self.get_fix is None:
            return
        before = self.move.to_dict()
        det = self.move.check(self.get_fix(), cfg.move_m, self.clock())
        if self.move.to_dict() != before:
            await self._save_state()
        if det is not None:
            await self._fire(det)

    async def _tick_loop(self) -> None:
        next_gps = 0.0
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.poll()
                if self.mono() >= next_gps:
                    next_gps = self.mono() + GPS_EVERY_S
                    await self.check_gps()
            except Exception:
                log.exception("alarm tick")

    # ── события ──
    def _on_samples(self, samples: list[Vec], dt: float) -> None:
        """Поток чтения датчика: обнаружение здесь, события — в event loop."""
        dets = self.detector.feed(samples, dt)
        loop = self._loop
        if not dets or loop is None:
            return
        for det in dets:
            with contextlib.suppress(RuntimeError):  # loop уже закрыт (остановка)
                loop.call_soon_threadsafe(self._detected, det)

    def _detected(self, det: Detection) -> None:
        if self.state in DETECTED:
            spawn(self._fire(det), name="alarm-event")

    def _record(self, ev: AlarmEvent) -> None:
        self.events = [ev, *self.events][:JOURNAL_MAX]
        self.seq += 1

    async def _fire(self, det: Detection) -> None:
        """Событие охраны: журнал → сирена и защита записи → outbox (push не чаще push_cooldown_s на вид)."""
        if self.state not in DETECTED:
            return
        cfg = self.get_settings()
        ev = make_event(det.kind, dict(det.data), self.clock())
        first = self.state != "triggered"
        self.state, self._last_event_mono = "triggered", self.mono()
        self._record(ev)
        log.warning("alarm: %s - %s", ev.title, ev.body)
        if first:
            await self._changed()
        else:
            await self._save_state()
        await self._save_events()
        if (cfg.siren and det.kind not in VISION_KINDS and self.state in DETECTED
                and not self.siren.sounding):
            spawn(self.siren.start(cfg.siren_s), name="alarm-siren-start")
        if cfg.hazard.enabled and self.state in DETECTED:
            self.hazard.flash(cfg.hazard.seconds)
        seg, snap, seg_rear, snap_rear = await asyncio.gather(
            self._protect(), self._save_snapshot(ev.id),
            self._protect("rear"), self._save_snapshot(ev.id, "rear"))
        if seg:
            ev.data["segment"] = seg
        if snap:
            ev.data["snapshot"] = True
        if seg_rear:
            ev.data["segment_rear"] = seg_rear
        if snap_rear:
            ev.data["snapshot_rear"] = True
        if seg or snap or seg_rear or snap_rear:
            await self._save_events()
        await self._enqueue(ev, cooldown=True)

    async def vision_event(self, kind: str, data: dict) -> bool:
        """Событие fnav-watch (человек подошёл / лицо у стекла / стоит у фургона; data.camera — какая камера) →
        как у датчика, но без сирены. С задней — только пока alarm.vision.rear (выключили — сервис узнает за ~3 с)."""
        cfg = self.get_settings().vision
        if kind not in VISION_KINDS or not cfg.enabled or not getattr(cfg, kind) or self.state not in DETECTED:
            return False
        if data.get("camera") == "rear" and not cfg.rear:
            return False
        await self._fire(Detection(kind, dict(data)))
        return True

    # ── снимки камеры к событиям ──
    def snapshot_path(self, event_id: str, camera: str = "front") -> Path | None:
        if not _EVENT_ID_RE.match(event_id):
            return None
        p = self.snap_dir / _snap_name(event_id, camera)
        return p if p.is_file() else None

    async def _save_snapshot(self, event_id: str, camera: str = "front") -> bool:
        fn = self.snapshot_rear if camera == "rear" else self.snapshot
        if fn is None:
            return False
        try:
            jpg = await fn()
        except Exception as e:  # noqa: BLE001 — камеры нет: событие идёт дальше
            log.info("alarm: no snapshot: %s", e)
            return False
        if not jpg:
            return False
        await self._run_io(self._write_snapshot, event_id, jpg, camera)
        return True

    def _write_snapshot(self, event_id: str, jpg: bytes, camera: str = "front") -> None:
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        name = _snap_name(event_id, camera)
        tmp = self.snap_dir / f".{name}.tmp"
        tmp.write_bytes(jpg)
        tmp.replace(self.snap_dir / name)
        old = sorted(self.snap_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[:-SNAPSHOTS_MAX * 2]
        for p in old:
            with contextlib.suppress(OSError):
                p.unlink()

    async def _protect(self, camera: str = "front") -> str | None:
        fn = self.protect_rear if camera == "rear" else self.protect
        if fn is None:
            return None
        try:
            res = await fn()
        except Exception as e:  # noqa: BLE001 — камеры нет: событие идёт дальше
            log.warning("alarm: cannot protect the recording: %s", e)
            return None
        seg = res.get("segment") if isinstance(res, dict) else None
        return seg if isinstance(seg, str) and seg else None

    async def _enqueue(self, ev: AlarmEvent, cooldown: bool) -> None:
        if not self.relay.configured:
            return
        if cooldown:
            # события камер — порознь по камерам: «подходит сзади» после «подходит спереди» — новость
            key = f"{ev.kind}:rear" if ev.kind in VISION_KINDS and ev.data.get("camera") == "rear" else ev.kind
            now, last = self.mono(), self._last_push.get(key)
            if last is not None and now - last < self.get_settings().push_cooldown_s:
                log.info("alarm: no push for %s (cooldown)", key)
                return
            self._last_push[key] = now
        await asyncio.to_thread(self.outbox.add, ev.model_dump(mode="json"))
        self._pending += 1
        self._kick.set()

    async def test(self) -> AlarmStatus:
        """Проверка уведомлений: событие test — журнал → сервер → push, без сирены и камеры, в любом состоянии."""
        ev = make_event("test", {}, self.clock())
        self._record(ev)
        await self._save_state()
        await self._save_events()
        await self._enqueue(ev, cooldown=False)
        return self.status()

    async def clear_events(self) -> AlarmStatus:
        self.events = []
        await self._save_events()
        await self._run_io(self._clear_snapshots)
        return self.status()

    def _clear_snapshots(self) -> None:
        for p in self.snap_dir.glob("*.jpg"):
            with contextlib.suppress(OSError):
                p.unlink()

    async def simulate(self, kind: str, g: float = 0.5, camera: str = "front") -> AlarmStatus:
        """Только --mock. Удар и наклон — через «датчик», если он читается: виден на уровне вибрации, событие даёт
        настоящее обнаружение (удар — через 1 с, наклон — через ~10 с). Иначе и для сдвига — сразу событие.
        Как у настоящих событий — только в armed/triggered."""
        if kind in VISION_KINDS:  # как от fnav-watch: с учётом настроек alarm.vision
            await self.vision_event(kind, {"simulated": True, "camera": camera})
            return self.status()
        cfg = self.get_settings()
        fake = self.imu.fake if self.imu.running else None
        if fake is not None and kind == "impact":
            fake.inject_impact(g)
        elif fake is not None and kind == "tilt":
            self._sim_tilt = -self._sim_tilt  # туда-обратно, чтобы «машина» не заваливалась всё сильнее
            fake.inject_tilt(self._sim_tilt * (cfg.tilt_deg + 2.0))
        else:
            if kind == "impact":
                data: dict = {"peak_g": round(g, 3)}
            elif kind == "tilt":
                data = {"tilt_deg": round(cfg.tilt_deg + 1.0, 1)}
            else:
                fix = self.get_fix() if self.get_fix else None
                data = {"distance_m": cfg.move_m + 50, "lat": fix.lat if fix else None,
                        "lon": fix.lon if fix else None}
            await self._fire(Detection(kind, data))
        return self.status()

    # ── сервер (fnav-relay) ──
    def _relay_state(self) -> dict:
        return {"armed": self.state != "off", "notify_offline": self.get_settings().notify_offline}

    async def send_once(self) -> int:
        """Состояние + события из outbox (пачками); события удаляются только после ответа 2xx."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), transport=self._transport,
                                             trust_env=False)
        sent = 0
        while True:
            rows = await asyncio.to_thread(self.outbox.peek, BATCH)
            r = await self._client.post(f"{self.relay.url}/device/alarm", headers={TOKEN_HEADER: self.relay.token or ""},
                                        json={"state": self._relay_state(), "events": [e for _, e in rows]})
            r.raise_for_status()
            if rows:
                await asyncio.to_thread(self.outbox.drop_upto, rows[-1][0])
                sent += len(rows)
            self._pending = await asyncio.to_thread(self.outbox.count)
            self.last_sent = time.time()
            if len(rows) < BATCH:
                return sent

    async def _relay_loop(self) -> None:
        delay = 0.0  # сразу после старта: состояние и то, что осталось в outbox
        retry: float | None = None
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=delay)
            self._kick.clear()
            if not self.relay.configured:
                delay = HEARTBEAT_S
                continue
            try:
                await self.send_once()
                if self.last_error:
                    log.info("alarm: server reachable again")
                self.last_error, retry, delay = None, None, HEARTBEAT_S
            except (httpx.HTTPError, OSError) as e:
                if self.last_error is None:
                    log.warning("alarm: cannot reach the server (%s), will retry", e)
                self.last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                retry = RETRY_MIN_S if retry is None else min(HEARTBEAT_S, retry * 2)
                delay = retry
            except Exception as e:
                log.exception("alarm relay")
                self.last_error = f"{type(e).__name__}: {e}"
                delay = HEARTBEAT_S

    # ── статус ──
    def _arm_in(self) -> int | None:
        if self.state != "arming" or self._arm_deadline is None:
            return None
        return max(0, math.ceil(self._arm_deadline - self.mono()))

    def _level(self, key: str) -> float | None:
        v = self.detector.take_level(key)
        return None if v is None or not self.imu.ok else round(v, 3)

    def status(self) -> AlarmStatus:
        angle = self.detector.angle
        return AlarmStatus(
            state=self.state, armed_at=self.armed_at, arm_in_s=self._arm_in(),  # type: ignore[arg-type]
            sensor=AlarmSensor(ok=self.imu.ok, model=self.imu.model, address=self.imu.address, bus=self.imu.bus,
                               error=self.imu.error),
            level_g=self._level("status"),
            tilt_deg=round(angle, 1) if angle is not None and self.state in DETECTED else None,
            siren=self.siren.sounding, events=self.events[:STATUS_EVENTS],
            relay=AlarmRelay(configured=self.relay.configured, pending=self._pending),
            hazard=self.hazard.status(),
            vision=self.vision_status() if self.vision_status else AlarmVision(
                enabled=self.get_settings().vision.enabled),
        )

    def telemetry(self) -> AlarmTelemetry:
        last = self.events[0] if self.events else None
        return AlarmTelemetry(
            state=self.state, arm_in_s=self._arm_in(), level_g=self._level("telemetry"),  # type: ignore[arg-type]
            sensor_ok=self.imu.ok, siren=self.siren.sounding, hazard=self.hazard.active, seq=self.seq,
            last=AlarmLast(kind=last.kind, ts=last.ts, title=last.title) if last else None,
        )
