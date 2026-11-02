"""Охрана (docs/contracts/alarm.md): удар и наклон по MPU-6050, сдвиг по GPS, пропажа видео камер; состояния off →
arming → armed → triggered → armed; журнал событий; действия — защита текущей записи камеры, сирена через колонку
Bluetooth (mpv радио), push через fnav-relay (outbox SQLite, как у журнала позиций).

Отсчёты акселерометра приходят в потоке чтения (fnav/core/imu.py) — обнаружение (Detector) считается там же,
в event loop попадают только готовые события и величины.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import statistics
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
    AlarmCameraVideo,
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
MOVE_REF_FIXES = 5  # точка отсчёта сдвига — медиана стольких первых хороших точек
MOVE_CONFIRM_FIXES = 5  # сдвиг — столько хороших точек подряд дальше move_m …
MOVE_CONFIRM_S = 10.0  # … от первой до последней не меньше
MOVE_GAP_S = 30.0  # хороших точек не было дольше — «далёкие» считаются заново (редкие выбросы не копятся)
TRIGGERED_HOLD_S = 30.0  # triggered — пока звучит сирена и ещё столько после последнего события
JOURNAL_MAX = 200
STATUS_EVENTS = 100
TICK_S = 0.25
GPS_EVERY_S = 1.0
VIDEO_EVERY_S = 1.0  # видео камер — проверка раз в секунду (ядро опрашивает камеры так же)
VIDEO_BACK_S = 10.0  # пропавшая камера «вернулась», когда видео идёт столько подряд (мигающая — одно событие)
HEARTBEAT_S = 60.0
RETRY_MIN_S = 5.0
OUTBOX_MAX_ROWS = 1000
OUTBOX_RETRY_S = 60.0  # база outbox не работала — снова пробовать её открыть не чаще
BATCH = 50
PUSH_THUMB_WAIT_S = 3.0  # push ждёт кадр к событию (миниатюра в уведомлении) не дольше; сирена и аварийка — не ждут
PUSH_THUMB_MAX = 32_000  # миниатюра больше — push без картинки (обычно ~5 КБ)
SIREN_RATE = 16000
SIREN_SECONDS = 8.0  # файл короткий, играет по кругу siren_s секунд
EPS = 1e-9

HAZARD_TEST_S = 3.0
HAZARD_BLINK_ON_S, HAZARD_BLINK_OFF_S = 0.7, 0.5  # подтверждение Arm/Disarm

DETECTED = ("armed", "triggered")  # события и сирена — только в этих состояниях
VISION_KINDS = ("approach", "face", "loiter")  # от камеры (fnav-watch): без сирены — push, аварийка, защита записи
# пропало видео камеры: как события камер (без сирены: бывает и сбой Wi-Fi, и перезагрузка камеры); вернулось —
# только журнал и push (не тревога)
NO_SIREN_KINDS = (*VISION_KINDS, "video_lost")
# поставили / сняли с охраны: не тревога — только журнал (и push, если с клавиатуры или снята посреди тревоги)
SWITCH_KINDS = ("armed", "disarmed")
SOURCE_TEXT = {"keypad": "with the keypad", "web": "from the app"}  # откуда поставили / сняли (data.source)
CAMERA_NAMES = {"front": "front camera", "rear": "rear camera"}
SNAPSHOTS_MAX = 100  # событий со снимками камер (alarm_snaps/<id>.jpg, задняя — <id>-rear.jpg)
SNAPSHOT_BEFORE_S = 5.0  # снимок к событию — кадр за столько до него: видно, что к нему привело
THUMBS_DIR = "thumbs"  # миниатюры снимков для списков (alarm_snaps/thumbs/<имя снимка>)


_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _snap_name(event_id: str, camera: str) -> str:
    return f"{event_id}-rear.jpg" if camera == "rear" else f"{event_id}.jpg"


@dataclass
class CameraTake:
    """Что взять у камеры к событию: снимок — кадр за before_s секунд до сейчас; recent — запись: текущей нет (камера
    пропала), тогда последнюю закрытую."""

    before_s: float
    recent: bool = False


# ───────────────────────── обнаружение ─────────────────────────


@dataclass
class Detection:
    kind: str  # "impact" | "tilt" | "move" | "approach" | "face" | "loiter" | "video_lost" | "video_back"
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
    """Точка годится для сдвига: только 3D (2D между домами и в паркинге скачет сильнее всего), HDOP ≤ 5, свежая."""
    return (fix.fix == "3d" and fix.lat is not None and fix.lon is not None and fix.hdop is not None
            and fix.hdop <= MOVE_MAX_HDOP and (fix.age_s is None or fix.age_s <= MOVE_MAX_AGE_S))


class MoveWatch:
    """Сдвиг по GPS. Точка отсчёта — медиана первых MOVE_REF_FIXES хороших точек (3D, HDOP ≤ 5) после постановки на
    охрану: выброс среди них (двор между домами, паркинг — скачок на 100–200 м) её не сдвинет. Сдвиг — когда хорошие
    точки дальше move_m идут подряд: не меньше MOVE_CONFIRM_FIXES и от первой до последней ≥ MOVE_CONFIRM_S (точка
    ближе — счёт заново; хороших не было дольше MOVE_GAP_S — тоже). Дальше — не чаще раза в 300 с и только если от
    последнего сообщённого места снова ≥ move_m (подряд, по тому же правилу). После перезапуска точка отсчёта —
    сохранённая, подтверждение — то же."""

    def __init__(self) -> None:
        self.ref: tuple[float, float] | None = None
        self.last: tuple[float, float] | None = None
        self.last_ts: float | None = None
        self._seed: list[tuple[float, float]] = []  # хорошие точки для точки отсчёта
        self._far_n = 0  # хороших точек подряд дальше move_m
        self._far_t0: float | None = None  # первая из них
        self._good_t: float | None = None  # последняя хорошая точка

    def reset(self) -> None:
        self.ref = self.last = self.last_ts = self._far_t0 = self._good_t = None
        self._seed, self._far_n = [], 0

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

    def check(self, fix: GpsFix, move_m: float, now: float, mono: float | None = None) -> Detection | None:
        """now — unix-время (last_ts переживает перезапуск), mono — для счёта подряд (без него — now)."""
        if not good_fix(fix):
            return None
        t = now if mono is None else mono
        cur = (float(fix.lat), float(fix.lon))  # type: ignore[arg-type]
        if self._good_t is not None and t - self._good_t > MOVE_GAP_S:
            self._far_n, self._far_t0 = 0, None
        self._good_t = t
        if self.ref is None:
            self._seed.append(cur)
            if len(self._seed) >= MOVE_REF_FIXES:
                self.ref = (statistics.median(p[0] for p in self._seed), statistics.median(p[1] for p in self._seed))
                self._seed = []
            return None
        if haversine_m(self.ref if self.last is None else self.last, cur) < move_m:
            self._far_n, self._far_t0 = 0, None
            return None
        if self._far_t0 is None:
            self._far_t0 = t
        self._far_n += 1
        if self._far_n < MOVE_CONFIRM_FIXES or t - self._far_t0 < MOVE_CONFIRM_S - EPS:
            return None
        if self.last is not None and now - (self.last_ts or 0.0) < MOVE_REPEAT_S:
            return None
        self.last, self.last_ts = cur, now
        self._far_n, self._far_t0 = 0, None
        return Detection("move", {"distance_m": round(haversine_m(self.ref, cur)), "lat": round(cur[0], 7),
                                  "lon": round(cur[1], 7)})


@dataclass
class _CamVideo:
    seen: bool = False  # видео было (после постановки на охрану): только такая камера может «пропасть»
    down_since: float | None = None  # monotonic: кадров нет с этой проверки
    down_ts: float | None = None  # то же, unix-время
    lost: bool = False  # сообщили video_lost — ждём, пока видео вернётся
    up_since: float | None = None  # после lost: видео снова идёт с …


class VideoWatch:
    """Пропало видео камеры на охране (docs/contracts/alarm.md «Камера пропала»). Камера, у которой после постановки
    видео было, без кадров дольше grace_s → video_lost (data.camera: "front" / "rear", без видео обе — "both";
    data.since — когда пропало); видео снова идёт VIDEO_BACK_S подряд → video_back (data.lost_s — сколько не было).
    Камеры нет в опросе (задняя выключена) — забыта; видео не было с постановки — не считается (видно на странице
    охраны). Короткие пропуски (сбой Wi-Fi, перезапуск сервиса камеры — несколько секунд) короче grace_s."""

    def __init__(self) -> None:
        self.cams: dict[str, _CamVideo] = {}

    def reset(self) -> None:
        self.cams.clear()

    def is_lost(self, camera: str) -> bool:
        c = self.cams.get(camera)
        return bool(c and c.lost)

    def check(self, video: dict[str, bool], grace_s: float, now: float, wall: float) -> list[Detection]:
        for cam in [c for c in self.cams if c not in video]:
            del self.cams[cam]
        out: list[Detection] = []
        newly: list[str] = []
        for cam, ok in video.items():
            c = self.cams.setdefault(cam, _CamVideo())
            if ok and not c.lost:
                c.seen, c.down_since, c.down_ts = True, None, None
            elif ok:
                if c.up_since is None:
                    c.up_since = now
                if now - c.up_since >= VIDEO_BACK_S - EPS:
                    lost_s = c.up_since - (c.down_since if c.down_since is not None else c.up_since)
                    out.append(Detection("video_back", {"camera": cam, "lost_s": round(lost_s)}))
                    c.seen, c.lost, c.up_since, c.down_since, c.down_ts = True, False, None, None, None
            else:
                c.up_since = None
                if not c.seen or c.lost:
                    continue
                if c.down_since is None:
                    c.down_since, c.down_ts = now, wall
                elif now - c.down_since >= grace_s - EPS:
                    c.lost = True
                    newly.append(cam)
        if newly:
            down = [cam for cam, c in self.cams.items() if c.lost]
            since = min(self.cams[cam].down_ts or wall for cam in newly)
            out.insert(0, Detection("video_lost", {"camera": "both" if len(down) > 1 else newly[0],
                                                   "since": round(since, 3)}))
        return out

    def status(self, video: dict[str, bool]) -> list[AlarmCameraVideo]:
        return [AlarmCameraVideo(camera=cam, video=ok, lost=self.is_lost(cam))  # type: ignore[arg-type]
                for cam, ok in video.items()]


def _gap_text(s: float) -> str:
    """Сколько не было видео: «40 s», «3 min», «1 h 20 min»."""
    n = max(0, round(s))
    if n < 60:
        return f"{n} s"
    m = n // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def make_event(kind: str, data: dict, ts: float) -> AlarmEvent:
    """Тексты push и журнала (по-английски); время — местное устройства, HH:MM. События камеры с задней
    (data.camera == "rear") — свои тексты: «за фургоном». armed / disarmed / siren — откуда (data.source)."""
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
    elif kind == "video_lost":
        since = time.strftime("%H:%M", time.localtime(float(data.get("since") or ts)))
        cam = data.get("camera")
        if cam == "both":
            title, body = "Both cameras lost", f"No video from either camera since {since}"
        else:
            name = CAMERA_NAMES.get(str(cam), "camera")
            title, body = f"{name.capitalize()} lost", f"No video from the {name} since {since}"
    elif kind == "video_back":
        name = CAMERA_NAMES.get(str(data.get("camera")), "camera")
        gap = _gap_text(float(data.get("lost_s") or 0))
        title, body = f"{name.capitalize()} is back", f"Video from the {name} again at {hm}, after {gap} without it"
    elif kind in SWITCH_KINDS:
        how = SOURCE_TEXT.get(str(data.get("source")), "")
        verb = "Armed" if kind == "armed" else "Disarmed"
        title, body = f"Alarm {verb.lower()}", " ".join(x for x in (verb, how, f"at {hm}") if x)
        delay = data.get("delay_s")
        if kind == "armed" and isinstance(delay, (int, float)) and delay > 0:
            body += f", active in {delay:.0f} s"
        elif kind == "disarmed" and data.get("was") == "triggered":
            body += " while the alarm was going off"
    elif kind == "siren":
        how = SOURCE_TEXT.get(str(data.get("source")), "")
        title, body = "Siren sounded", " ".join(x for x in ("Sounded", how, f"at {hm}") if x)
        secs = data.get("seconds")
        if isinstance(secs, (int, float)) and secs > 0:
            body += f" for {secs:.0f} s"
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
        self.skipped: str | None = None  # почему последний start() не включил сирену
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

    async def start(self, seconds: float, manual: bool = False) -> bool:
        """manual — по кнопке хозяина: в любом состоянии охраны (allowed не спрашивается). Не зазвучала — почему,
        в self.skipped."""
        async with self._busy:
            self.skipped = None
            if self.sounding:
                return True
            if not manual and not self.allowed():
                self.skipped = "the alarm is off"
                return False
            if self.radio is None:
                self.skipped = "no radio player"
                log.info("alarm: siren skipped - no radio player")
                return False
            try:
                path = await asyncio.to_thread(self.ensure_wav)
            except OSError as e:
                self.skipped = f"cannot write the siren sound: {e}"
                log.warning("alarm: siren skipped - cannot write %s: %s", self.wav_path, e)
                return False
            why = await self.radio.play_alert(str(path), volume=100)
            if why:
                self.skipped = why
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

    Вывод открывается и закрывается в потоке, не в event loop: у аппаратного ШИМ это ожидание прав от udev и импульсы
    «выкл» перед снятием сигнала. Погасить незажжённое — ничего не делать: выключенная аварийка ШИМ не трогает
    (без dtoverlay=pwm это не ошибка, пока её не включили); удачное открытие или переключение стирает прошлую ошибку.
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
        self._lock = asyncio.Lock()  # вывод и переключения реле — по одному

    @property
    def active(self) -> bool:
        return any(t is not None and not t.done() for t in (self._task, self._blink))

    async def _output(self, cfg: HazardSettings) -> HazardOutput | None:
        """Открыть вывод (или заново — сменились импульсы); ошибка — в self.error. Только под self._lock."""
        key = output_key(cfg)
        if self._out is not None and key == self._key:
            return self._out
        await self._close_output()
        try:
            out = await asyncio.to_thread(self.open_output, cfg)
        except Exception as e:  # noqa: BLE001 — нет dtoverlay=pwm, нет прав и т.п.
            if self.error != str(e):
                log.warning("alarm: hazard relay unavailable: %s", e)
            self.error = str(e)
            return None
        self._out, self._key, self.error = out, key, None
        log.info("alarm: hazard relay ready (pulses %d / %d us)", cfg.on_us, cfg.off_us)
        return out

    async def _close_output(self) -> None:
        out, self._out, self._key, self.lit = self._out, None, None, False
        if out is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(out.close)

    async def apply(self, cfg: HazardSettings) -> None:
        """Настройки: включена — держать вывод открытым (у RC-реле — импульсы «выкл»), выключена — отпустить."""
        async with self._lock:
            if self.active:
                return  # мигает (или тест) — вывод отпустим/переоткроем после
            if cfg.enabled:
                await self._output(cfg)
            else:
                await self._close_output()
                self.error = None

    async def _switch(self, on: bool) -> None:
        async with self._lock:
            if self.lit == on:
                return  # уже так: погасить незажжённое — вывод не открывать
            out = await self._output(self.get_cfg())
            if out is None:
                return
            try:
                out.set(on)
            except Exception as e:  # noqa: BLE001 — sysfs ШИМ пропал / нет прав
                self.error = _err_text(e)
                log.warning("alarm: hazard relay failed: %s", self.error)
                await self._close_output()
                return
            self.lit, self.error = on, None

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
            await asyncio.shield(self._after())

    async def _after(self) -> None:
        async with self._lock:
            if not self.get_cfg().enabled:
                await self._close_output()  # тест при выключенной аварийке — вывод не держим

    async def stop(self) -> None:
        self._until = 0.0
        await cancel_tasks(self._task, self._blink)
        self._task = self._blink = None
        await self._switch(False)
        await self._after()

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
        """Мигнуть HAZARD_TEST_S текущими настройками (даже выключенной). None — пошло, иначе — почему нет.
        Выключенная не открылась — причина только в ответе: в статусе она не «сломана» (ею не пользуются)."""
        cfg = self.get_cfg()
        async with self._lock:
            if await self._output(cfg) is None:
                why = self.error or "hazard relay unavailable"
                if not cfg.enabled:
                    self.error = None
                return why
        self.flash(HAZARD_TEST_S)
        return None

    async def close(self) -> None:
        await self.stop()
        async with self._lock:
            await self._close_output()

    def status(self) -> AlarmHazard:
        return AlarmHazard(enabled=self.get_cfg().enabled, active=self.active, error=self.error)


# ───────────────────────── outbox для сервера ─────────────────────────


def _err_text(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _db_broken(e: BaseException) -> bool:
    """Файл базы испорчен (а не «нельзя писать»): его можно убрать в сторону и начать новую."""
    name = getattr(e, "sqlite_errorname", None) or ""
    return name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")) or type(e) is sqlite3.DatabaseError


class AlarmOutbox:
    """Очередь событий для fnav-relay: SQLite (переживает обрывы и перезагрузки), а когда база не открывается или не
    пишется — в памяти (push уходит, пока жив процесс). Битый файл (пропало питание) — в сторону (*.bad-<время>) и
    новая база; не вышло и так (карта только для чтения, полна, чужой владелец файла) — память, error — почему.
    Базу пробуем открыть снова не чаще OUTBOX_RETRY_S (retry): заработала — очередь из памяти уходит в неё. Пока база не
    работает, очередь из памяти лежит и в маленьком запасном файле (spill, без миниатюр), если его можно записать:
    после перезапуска она снова в очереди. Строки — ((«db» | «mem», id), событие); сервер события с известным id не
    дублирует, повтор не страшен."""

    def __init__(self, path: Path, max_rows: int = OUTBOX_MAX_ROWS, mono: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self.max_rows = max_rows
        self.mono = mono
        self.db: Outbox | None = Outbox(path, max_rows=max_rows)
        self.error: str | None = None  # база не работает — очередь в памяти
        self.moved: Path | None = None  # куда убран битый файл
        self.spill = path.with_name(path.name + ".pending.json")  # запасная копия очереди из памяти
        self.spilled = False  # копия очереди из памяти сейчас в self.spill (переживёт перезапуск)
        self._spill_error: str | None = None
        self._retry_at = 0.0  # monotonic: раньше базу снова не открывать
        self._mem: list[tuple[int, dict]] = []
        self._seq = 0
        self._lock = threading.RLock()

    def open(self) -> None:
        """При старте: открыть базу; битая — в сторону и заново; не вышло — память. Очередь из запасного файла
        (с прошлого раза) — туда же."""
        with self._lock:
            if self.db is None:
                return
            left = self._read_spill()
            why = self._connect(self.db)
            if why is None and left:
                try:
                    for b in left:
                        self.db.add(b)
                    left = []
                except Exception as e:  # noqa: BLE001 — недописанное повторится: сервер дубли по id отбросит
                    why = _err_text(e)
            if why is None:
                self._drop_spill()
                return
            self._mem = self._numbered([*left, *(b for _, b in self._mem)])
            self._fail(why, salvage=False)  # читать из неё нечего (а рядом может лежать чужой журнал WAL)

    def _connect(self, db: Outbox) -> str | None:
        """Проверить базу; битая — в сторону и новая. None — работает, иначе — почему нет."""
        try:
            db.count()
            return None
        except Exception as e:  # noqa: BLE001 — sqlite3.Error, OSError
            why = _err_text(e)
            if not _db_broken(e):
                return why
        moved: Path | None = None
        try:
            moved = self._move_aside(db)
            self.moved = moved
            log.error("alarm: outbox %s is broken (%s) - moved to %s, starting a new one", self.path, why, moved.name)
            db.count()
            return None
        except Exception as e:  # noqa: BLE001
            return f"{why}; no new one: {_err_text(e)}" if moved is None else _err_text(e)

    def _move_aside(self, db: Outbox) -> Path:
        db.close()
        bad = self.path.with_name(f"{self.path.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(self.path, bad)
        for sfx in ("-wal", "-shm"):  # журнал битой базы к новой не относится
            p = self.path.with_name(self.path.name + sfx)
            if p.exists():
                os.replace(p, bad.with_name(bad.name + sfx))
        return bad

    def _fail(self, why: str, salvage: bool = True) -> None:
        """База не работает (why — почему): дальше — память; что из базы ещё читается (salvage) — туда же."""
        self.error = why
        log.error("alarm: outbox %s unusable (%s) - events wait for the server in memory, retry in %.0f s",
                  self.path, self.error, OUTBOX_RETRY_S)
        self._retry_at = self.mono() + OUTBOX_RETRY_S
        old: list[tuple[int, dict]] = []
        if self.db is not None:
            if salvage:
                with contextlib.suppress(Exception):
                    old = self.db.peek(self.max_rows)
            with contextlib.suppress(Exception):
                self.db.close()
        self.db = None
        self._mem = self._numbered([*(b for _, b in old), *(b for _, b in self._mem)])
        self._spill_mem()

    def retry(self) -> bool:
        """База не работала: открыть снова (не чаще OUTBOX_RETRY_S). Вышло — очередь из памяти в неё, запасной файл
        удаляется. → работает ли база."""
        with self._lock:
            if self.db is not None:
                return True
            now = self.mono()
            if now < self._retry_at:
                return False
            self._retry_at = now + OUTBOX_RETRY_S
            db = Outbox(self.path, max_rows=self.max_rows)
            why = self._connect(db)
            if why is None:
                try:
                    for _, b in self._mem:
                        db.add(b)
                except Exception as e:  # noqa: BLE001 — недописанное повторится: сервер дубли по id отбросит
                    why = _err_text(e)
            if why is not None:
                with contextlib.suppress(Exception):
                    db.close()
                self.error = why
                return False
            log.warning("alarm: outbox %s works again (%d event(s) moved there from memory)", self.path,
                        len(self._mem))
            self.db, self._mem, self.error = db, [], None
            self._drop_spill()
            return True

    def _numbered(self, bodies: list[dict]) -> list[tuple[int, dict]]:
        """Новые id (больше всех прежних: недосланная пачка со старыми id ничего лишнего не удалит)."""
        out = []
        for b in bodies[-self.max_rows:]:
            self._seq += 1
            out.append((self._seq, b))
        return out

    # ── запасной файл ──
    def _spill_mem(self) -> None:
        """Очередь из памяти → запасной файл (без миниатюр: файл маленький), пусто — удалить. Не пишется — в лог."""
        if not self._mem:
            self._drop_spill()
            return
        try:
            atomic_write_text(self.spill, json.dumps([{k: v for k, v in b.items() if k != "thumb"}
                                                      for _, b in self._mem], ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 — карта «только чтение» / полна: очередь только в памяти
            msg = _err_text(e)
            if msg != self._spill_error:
                log.error("alarm: cannot keep the queue in %s either: %s", self.spill, msg)
            self._spill_error, self.spilled = msg, False
            return
        self._spill_error, self.spilled = None, True

    def _drop_spill(self) -> None:
        with contextlib.suppress(OSError):
            self.spill.unlink(missing_ok=True)
        self.spilled = False

    def _read_spill(self) -> list[dict]:
        """При старте: очередь, оставшаяся в запасном файле (база в прошлый раз не работала)."""
        try:
            raw = json.loads(self.spill.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.error("alarm: cannot read %s: %s", self.spill, _err_text(e))
            return []
        rows = raw if isinstance(raw, list) else []
        bodies = [b for b in rows if isinstance(b, dict) and isinstance(b.get("ts"), (int, float))]
        log.warning("alarm: %d event(s) for the server left in %s from the last run", len(bodies), self.spill.name)
        return bodies

    # ── очередь ──
    def add(self, ev: dict) -> None:
        with self._lock:
            if self.db is not None:
                try:
                    self.db.add(ev)
                    return
                except Exception as e:  # noqa: BLE001
                    self._fail(_err_text(e))
            self._mem = [*self._mem, *self._numbered([ev])][-self.max_rows:]
            self._spill_mem()

    def peek(self, n: int = BATCH) -> list[tuple[tuple[str, int], dict]]:
        with self._lock:
            rows: list[tuple[tuple[str, int], dict]] = []
            if self.db is not None:
                try:
                    rows = [(("db", i), e) for i, e in self.db.peek(n)]
                except Exception as e:  # noqa: BLE001
                    self._fail(_err_text(e))
            return rows + [(("mem", i), e) for i, e in self._mem[:max(0, n - len(rows))]]

    def drop(self, rows: list[tuple[tuple[str, int], dict]]) -> None:
        """Удалить доставленные строки (из peek)."""
        db_ids = [k[1] for k, _ in rows if k[0] == "db"]
        mem_ids = [k[1] for k, _ in rows if k[0] == "mem"]
        with self._lock:
            if db_ids and self.db is not None:
                try:
                    self.db.drop_upto(max(db_ids))
                except Exception as e:  # noqa: BLE001
                    self._fail(_err_text(e))
            if mem_ids:
                last = max(mem_ids)
                self._mem = [r for r in self._mem if r[0] > last]
                if self.db is None:
                    self._spill_mem()

    def count(self) -> int:
        with self._lock:
            n = 0
            if self.db is not None:
                try:
                    n = self.db.count()
                except Exception as e:  # noqa: BLE001
                    self._fail(_err_text(e))
            return n + len(self._mem)

    def close(self) -> None:
        with self._lock:
            if self.db is not None:
                with contextlib.suppress(Exception):
                    self.db.close()


# ───────────────────────── сервис ─────────────────────────


class AlarmService:
    def __init__(self, data_dir: Path, get_settings: Callable[[], AlarmSettings], imu: ImuService,
                 relay: RelayConfig, *, get_fix: Callable[[], GpsFix] | None = None,
                 protect: Callable[..., Awaitable[dict]] | None = None, radio: RadioService | None = None,
                 snapshot: Callable[..., Awaitable[bytes]] | None = None,
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
        self.protect_rear: Callable[..., Awaitable[dict]] | None = None  # (recent=False) → {"segment": …}
        self.snapshot_rear: Callable[..., Awaitable[bytes]] | None = None  # (before_s=…) → jpeg
        self.snap_dir = data_dir / "alarm_snaps"
        self.vision_status: Callable[[], AlarmVision] | None = None  # fnav/core/vision.py
        # идёт ли видео камер ({"front": True, "rear": False}; выключенной задней нет): ставит runtime
        self.get_video: Callable[[], dict[str, bool]] | None = None
        self.clock = clock
        self.mono = mono
        self.detector = Detector()
        self.move = MoveWatch()
        self.video = VideoWatch()
        self.siren = Siren(radio, data_dir / "alarm_siren.wav", allowed=lambda: self.state in DETECTED)
        self.hazard = HazardLights(lambda: self.get_settings().hazard, open_hazard, mono=mono)
        self.outbox = AlarmOutbox(data_dir / "alarm_outbox.db", max_rows=OUTBOX_MAX_ROWS, mono=mono)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None  # создаётся при первой отправке (сервер может быть не настроен)
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fnav-alarm-io")  # записи файлов — по порядку
        self.state = "off"
        self.armed_at: float | None = None
        self._arm_deadline: float | None = None  # monotonic
        # то же по часам (unix): в alarm_state.json — перезапуск посреди отсчёта продолжает его, а не ставит сразу
        self._arm_until: float | None = None
        self._last_event_mono: float | None = None
        self.events: list[AlarmEvent] = []  # новые первыми
        self.seq = 0
        # правка журнала (событие, снимок к нему, очистка): админка берёт журнал, только когда номер сменился; с часов
        # старта — после перезапуска не совпадёт с прежним
        self.events_rev = int(clock() * 1000) % 1_000_000_000_000
        self._last_push: dict[str, float] = {}  # kind (события камер — kind:камера) → monotonic
        self._pending = 0
        self._notify = get_settings().notify_offline
        self._kick = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sim_tilt = -1.0
        self.last_error: str | None = None
        self.last_sent: float | None = None
        self._write_errors: dict[str, str] = {}  # файл охраны → почему не записался (карта полна, «только чтение»)
        self.detector.configure(get_settings())
        imu.subscribe(self._on_samples)

    # ── жизненный цикл ──
    async def start(self, background: bool = True) -> None:
        """Файлы не читаются, outbox битый — охрана всё равно работает: циклы событий и сервера запускаются всегда."""
        self._loop = asyncio.get_running_loop()
        try:
            await self._run_io(self._load)
        except Exception:
            log.exception("alarm: cannot load the saved state")
        if self.state == "arming":
            log.info("alarm: arming resumed after restart, %s s left", self._arm_in())
        elif self.state != "off":
            self.detector.rebase()  # база наклона — через 2 с после старта
            log.info("alarm: armed (restored after restart)")
        try:
            await asyncio.to_thread(self.outbox.open)  # битая база — в сторону и новая; не вышло — очередь в памяти
            self._pending = await asyncio.to_thread(self.outbox.count)
            await self.hazard.apply(self.get_settings().hazard)
        except Exception:
            log.exception("alarm start")
        if self.siren.radio is not None:  # WAV сирены — заранее: карта «только чтение» потом не помешает сирене
            try:
                await asyncio.to_thread(self.siren.ensure_wav)
            except OSError as e:
                log.warning("alarm: cannot write %s: %s", self.siren.wav_path, e)
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
        if self._loop is not None:  # до start() — вывод аварийки откроет сам start()
            spawn(self.hazard.apply(s.hazard), name="alarm-hazard-apply")
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
            if d.get("on") and not self._resume_arming(d.get("arm_until")):
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

    def _resume_arming(self, until: Any) -> bool:
        """Перезапуск посреди отсчёта постановки (arm_until — срок по часам): досчитать остаток, а не ставить сразу —
        иначе база наклона берётся, пока ещё закрывают двери (удар / наклон → сирена). Срок прошёл — False (сразу
        armed). Остаток — не дольше arm_delay_s: часы после перезагрузки без RTC могут отставать."""
        if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
            return False
        left = min(float(until) - self.clock(), float(self.get_settings().arm_delay_s))
        if left <= 0:
            return False
        self.state, self.armed_at = "arming", None
        self._arm_deadline, self._arm_until = self.mono() + left, self.clock() + left
        return True

    async def _write(self, path: Path, text: str) -> bool:
        """Записать файл охраны. Не вышло (карта полна, перемонтирована «только чтение») — в лог и в
        AlarmStatus.storage_error, охрана идёт дальше: состояние и журнал — в памяти."""
        try:
            await self._run_io(atomic_write_text, path, text)
        except Exception as e:  # noqa: BLE001
            msg = _err_text(e)
            if self._write_errors.get(path.name) != msg:
                log.error("alarm: cannot write %s: %s", path, msg)
            self._write_errors[path.name] = msg
            return False
        if self._write_errors.pop(path.name, None):
            log.info("alarm: %s is written again", path.name)
        return True

    async def _save_state(self) -> bool:
        d = {"on": self.state != "off", "armed_at": self.armed_at, "seq": self.seq, "move": self.move.to_dict(),
             "arm_until": self._arm_until if self.state == "arming" else None}
        return await self._write(self.state_path, json.dumps(d))

    async def _save_events(self) -> bool:
        data = json.dumps([e.model_dump(mode="json") for e in self.events], ensure_ascii=False)
        return await self._write(self.events_path, data)

    def storage_error(self) -> str | None:
        """Что не пишется на карту (охрана при этом работает): файлы охраны, outbox."""
        parts = []
        if self._write_errors:
            parts.append("Alarm state and log are not saved: " + next(iter(self._write_errors.values())))
        if self.outbox.error and self.outbox.spilled:
            parts.append(f"Notifications wait in memory and in {self.outbox.spill.name}: {self.outbox.error}")
        elif self.outbox.error:
            parts.append(f"Notifications wait in memory only (lost on restart): {self.outbox.error}")
        return "; ".join(parts) or None

    # ── состояние ──
    async def _changed(self) -> None:
        self._kick.set()  # серверу — сразу, даже если файл не запишется
        await self._save_state()

    def _set_armed(self) -> None:
        self.state, self.armed_at, self._arm_deadline, self._arm_until = "armed", self.clock(), None, None
        self._last_event_mono = None
        self.detector.rebase()
        self.move.reset()
        self.video.reset()
        log.info("alarm: armed")

    async def arm(self, source: str = "web") -> AlarmStatus:
        """off → arming (arm_delay_s) → armed; при arm_delay_s = 0 — сразу armed. Уже включена — ничего.
        source — откуда («web» — API админки, «keypad» — клавиша): в журнал; не из админки — и push."""
        if self.state == "off":
            cfg = self.get_settings()
            if cfg.hazard.enabled and cfg.hazard.confirm:
                self.hazard.confirm(1)
            delay = cfg.arm_delay_s
            if delay <= 0:
                self._set_armed()
            else:
                self.state, self._arm_deadline, self.armed_at = "arming", self.mono() + delay, None
                self._arm_until = self.clock() + delay
                log.info("alarm: arming in %d s", delay)
            await self._journal("armed", {"source": source, "delay_s": max(0, delay)}, push=source != "web")
            await self._changed()
        return self.status()

    async def disarm(self, source: str = "web") -> AlarmStatus:
        """→ off, сирена и аварийка молчат. В журнал (data.source, data.was); push — не из админки или посреди
        тревоги (хозяин должен узнать, что сирену заглушили в кабине)."""
        was = self.state
        self.state, self.armed_at, self._arm_deadline, self._last_event_mono = "off", None, None, None
        self._arm_until = None
        self.detector.clear_base()
        self.move.reset()
        self.video.reset()
        await self.siren.stop()
        await self.hazard.stop()
        if was != "off":
            hz = self.get_settings().hazard
            if hz.enabled and hz.confirm:
                self.hazard.confirm(2)
            log.info("alarm: disarmed")
            await self._journal("disarmed", {"source": source, "was": was},
                                push=source != "web" or was == "triggered")
            await self._changed()
        return self.status()

    async def silence(self) -> AlarmStatus:
        """Сирена и аварийка замолкают, охрана остаётся."""
        await self.siren.stop()
        await self.hazard.stop()
        return self.status()

    async def sound_siren(self, source: str = "web") -> str | None:
        """Сирена по кнопке хозяина (паника: на снимке события камеры — человек у двери, а такие события сирену не
        включают): siren_s секунд в любом состоянии охраны и даже при settings.alarm.siren = false. Не тревога:
        состояние, аварийка, записи — как были; в журнал — siren (data.source), push — не из админки.
        None — звучит, иначе — почему нет (нет колонки…). Уже звучит — None без новой записи."""
        if self.siren.sounding:
            return None
        secs = self.get_settings().siren_s
        if not await self.siren.start(secs, manual=True):
            return self.siren.skipped or "the siren did not start"
        log.warning("alarm: siren sounded by hand (%s)", source)
        await self._journal("siren", {"source": source, "seconds": secs}, push=source != "web")
        await self._save_state()  # seq
        return None

    async def hazard_test(self) -> str | None:
        """Мигнуть аварийкой 3 с текущими настройками. None — пошло, иначе — почему нет."""
        return await self.hazard.test()

    async def toggle(self, source: str = "keypad") -> bool:
        """Клавиша «Alarm on/off»: off → arming/armed, иначе (и из triggered) → off. → включена ли теперь."""
        if self.state == "off":
            await self.arm(source)
            return True
        await self.disarm(source)
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
        det = self.move.check(self.get_fix(), cfg.move_m, self.clock(), self.mono())
        if self.move.to_dict() != before:
            await self._save_state()
        if det is not None:
            await self._fire(det)

    async def check_video(self) -> None:
        """Пропало видео камеры (VideoWatch): video_lost — тревога, video_back — только журнал и push."""
        cfg = self.get_settings()
        if self.get_video is None or self.state not in DETECTED or not cfg.video_loss:
            self.video.reset()  # выключили — после включения камера снова должна «показаться»
            return
        for det in self.video.check(self.get_video(), cfg.video_loss_s, self.mono(), self.clock()):
            if det.kind == "video_lost":
                await self._fire(det)
            else:
                await self._note(det)

    async def _tick_loop(self) -> None:
        next_gps = next_video = 0.0
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.poll()
                if self.mono() >= next_gps:
                    next_gps = self.mono() + GPS_EVERY_S
                    await self.check_gps()
                if self.mono() >= next_video:
                    next_video = self.mono() + VIDEO_EVERY_S
                    await self.check_video()
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
        self.events_rev += 1

    async def _fire(self, det: Detection) -> None:
        """Событие охраны: сирена и аварийка → outbox (push не чаще push_cooldown_s на вид; с миниатюрой кадра, если он
        успел за PUSH_THUMB_WAIT_S) → журнал и состояние на карту, защита записи и снимки. Сначала то, что от карты
        памяти не зависит: полная или «только чтение» карта не оставит без сирены, аварийки и push; каждый шаг сам по
        себе (ошибка — в лог, записи — в storage_error). Кадры и защита записи запрашиваются сразу, не дожидаясь push."""
        if self.state not in DETECTED:
            return
        cfg = self.get_settings()
        ev = make_event(det.kind, dict(det.data), self.clock())
        first = self.state != "triggered"
        self.state, self._last_event_mono = "triggered", self.mono()
        self._record(ev)
        log.warning("alarm: %s - %s", ev.title, ev.body)
        if cfg.siren and det.kind not in NO_SIREN_KINDS and not self.siren.sounding:
            spawn(self.siren.start(cfg.siren_s), name="alarm-siren-start")
        if cfg.hazard.enabled:
            self.hazard.flash(cfg.hazard.seconds)
        if first:
            self._kick.set()  # новое состояние — серверу сразу
        try:
            plan = self._camera_plan(det)
        except Exception:
            log.exception("alarm: camera plan")
            plan = {}
        front, rear = plan.get("front"), plan.get("rear")
        grabs = {cam: spawn(self._grab(cam, take), name=f"alarm-snapshot-{cam}") for cam, take in plan.items()}
        protects = [spawn(self._protect(cam, take), name=f"alarm-protect-{cam}") for cam, take in
                    (("front", front), ("rear", rear))]
        thumb: tuple[str, bytes] | None = None
        if self._push_due(ev, cooldown=True):
            thumb = await self._push_thumb(det, grabs)
            await self._queue(ev, thumb[1] if thumb else None)
        res = await asyncio.gather(
            self._save_state(), self._save_events(),
            protects[0], self._store_snapshot(ev.id, "front", grabs.get("front"), thumb),
            protects[1], self._store_snapshot(ev.id, "rear", grabs.get("rear"), thumb), return_exceptions=True)
        for r in res:
            if isinstance(r, BaseException):
                log.error("alarm: %s - step failed: %s", det.kind, _err_text(r))
        seg, snap, seg_rear, snap_rear = (None if isinstance(r, BaseException) else r for r in res[2:])
        if seg:
            ev.data["segment"] = seg
        if snap:
            ev.data["snapshot"] = True
        if seg_rear:
            ev.data["segment_rear"] = seg_rear
        if snap_rear:
            ev.data["snapshot_rear"] = True
        if seg or snap or seg_rear or snap_rear:
            self.events_rev += 1
            await self._save_events()

    async def _note(self, det: Detection) -> None:
        """Событие без тревоги (камера снова передаёт видео): журнал и push; состояние, сирена и аварийка — как были."""
        if self.state not in DETECTED:
            return
        ev = make_event(det.kind, dict(det.data), self.clock())
        self._record(ev)
        log.info("alarm: %s - %s", ev.title, ev.body)
        await self._enqueue(ev, cooldown=True)
        await self._save_state()
        await self._save_events()

    async def _journal(self, kind: str, data: dict, push: bool) -> None:
        """Запись журнала «поставили / сняли с охраны» (SWITCH_KINDS) и «сирена по кнопке» (siren): не тревога — без
        аварийки, защиты записи и снимков; push — если push (без паузы push_cooldown_s: «сняли» вслед за «поставили»
        не теряется). Состояние сохраняет вызывающий (_changed)."""
        ev = make_event(kind, data, self.clock())
        self._record(ev)
        log.info("alarm: %s - %s", ev.title, ev.body)
        if push:
            await self._enqueue(ev, cooldown=False)
        await self._save_events()

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

    async def snapshot_thumb(self, event_id: str, camera: str = "front") -> Path | None:
        """Миниатюра снимка для списков (~5 КБ, 192×144): делается при первом запросе и лежит рядом
        (alarm_snaps/thumbs/), удаляется вместе со снимком."""
        src = self.snapshot_path(event_id, camera)
        if src is None:
            return None
        p = self.snap_dir / THUMBS_DIR / src.name
        if p.is_file():
            return p
        try:
            await self._run_io(self._write_thumb, src, p)
        except Exception as e:  # noqa: BLE001 — битый снимок: без миниатюры
            log.info("alarm: no thumbnail for %s: %s", src.name, e)
            return None
        return p if p.is_file() else None

    @classmethod
    def _write_thumb(cls, src: Path, dst: Path) -> None:
        from fnav.camera.jpeg import thumbnail_jpeg

        cls._put_thumb(dst, thumbnail_jpeg(src.read_bytes()))

    @staticmethod
    def _put_thumb(dst: Path, data: bytes) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)

    def _camera_plan(self, det: Detection) -> dict[str, CameraTake]:
        """Что брать у камер к событию: снимок (кадр за before_s до сейчас) и защиту записи. Камера, у которой видео
        сейчас нет (или задняя выключена — её нет в get_video), не даёт ничего: ни стоп-кадра из памяти сервиса, ни
        старой записи. Кроме пропавшей камеры в событии video_lost: её последние кадры (до data.since) и последний
        сегмент, закрытый при перезапуске конвейера. get_video не задан — как раньше: обе."""
        if self.get_video is None:
            return {cam: CameraTake(SNAPSHOT_BEFORE_S) for cam in CAMERA_NAMES}
        video = self.get_video()
        lost: list[str] = []
        if det.kind == "video_lost":
            cam = det.data.get("camera")
            lost = list(CAMERA_NAMES) if cam == "both" else [str(cam)]
        plan: dict[str, CameraTake] = {}
        for cam, ok in video.items():
            if cam in lost and not ok:
                since = float(det.data.get("since") or self.clock())
                plan[cam] = CameraTake(max(0.0, self.clock() - since) + SNAPSHOT_BEFORE_S, recent=True)
            elif ok:
                plan[cam] = CameraTake(SNAPSHOT_BEFORE_S)
        return plan

    async def _grab(self, camera: str, take: CameraTake | None) -> bytes | None:
        """Кадр камеры к событию (JPEG) или None: камеры нет / кадров тогда не было."""
        fn = self.snapshot_rear if camera == "rear" else self.snapshot
        if fn is None or take is None:
            return None
        try:
            jpg = await fn(before_s=take.before_s)
        except Exception as e:  # noqa: BLE001 — камеры нет: событие идёт дальше
            log.info("alarm: no %s snapshot: %s", camera, e)
            return None
        return jpg or None

    @staticmethod
    def _push_camera(det: Detection, cams: Any) -> str | None:
        """Чей кадр — в push: камеры, поднявшей событие (data.camera; «обе пропали» — передней), иначе передней."""
        cam = det.data.get("camera")
        for c in ([cam] if cam in CAMERA_NAMES else []) + ["front", "rear"]:
            if c in cams:
                return c
        return None

    async def _push_thumb(self, det: Detection, grabs: dict[str, asyncio.Task]) -> tuple[str, bytes] | None:
        """Миниатюра кадра к событию для push (camera, JPEG ~5 КБ) — ждём кадр не дольше PUSH_THUMB_WAIT_S (камера
        не отвечает — push уходит без картинки; сам снимок сохранится, когда придёт)."""
        cam = self._push_camera(det, grabs)
        if cam is None:
            return None
        try:
            jpg = await asyncio.wait_for(asyncio.shield(grabs[cam]), PUSH_THUMB_WAIT_S)
        except TimeoutError:
            log.info("alarm: push without a picture - no %s snapshot in %.0f s", cam, PUSH_THUMB_WAIT_S)
            return None
        except Exception:  # noqa: BLE001 — _grab ошибок не бросает; на всякий случай — push без картинки
            return None
        if not jpg:
            return None
        from fnav.camera.jpeg import thumbnail_jpeg

        try:
            small = await asyncio.to_thread(thumbnail_jpeg, jpg)
        except Exception as e:  # noqa: BLE001 — битый кадр: без картинки
            log.info("alarm: no thumbnail for the push: %s", e)
            return None
        return (cam, small) if 0 < len(small) <= PUSH_THUMB_MAX else None

    async def _store_snapshot(self, event_id: str, camera: str, grab: asyncio.Task | None,
                              thumb: tuple[str, bytes] | None = None) -> bool:
        """Кадр (grab) → alarm_snaps на карту; миниатюра, уже сделанная для push, — рядом (не делать её второй раз)."""
        if grab is None:
            return False
        jpg = await grab
        if not jpg:
            return False
        small = thumb[1] if thumb and thumb[0] == camera else None
        try:
            await self._run_io(self._write_snapshot, event_id, jpg, camera, small)
        except Exception as e:  # noqa: BLE001 — карта полна / «только чтение»: событие идёт дальше
            log.error("alarm: cannot save the %s snapshot: %s", camera, _err_text(e))
            return False
        return True

    def _write_snapshot(self, event_id: str, jpg: bytes, camera: str = "front", thumb: bytes | None = None) -> None:
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        name = _snap_name(event_id, camera)
        tmp = self.snap_dir / f".{name}.tmp"
        tmp.write_bytes(jpg)
        tmp.replace(self.snap_dir / name)
        if thumb:
            with contextlib.suppress(OSError):  # не вышло — сделается при первом запросе
                self._put_thumb(self.snap_dir / THUMBS_DIR / name, thumb)
        old = sorted(self.snap_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[:-SNAPSHOTS_MAX * 2]
        for p in old:
            with contextlib.suppress(OSError):
                p.unlink()
            with contextlib.suppress(OSError):
                (self.snap_dir / THUMBS_DIR / p.name).unlink()

    async def _protect(self, camera: str = "front", take: CameraTake | None = None) -> str | None:
        fn = self.protect_rear if camera == "rear" else self.protect
        if fn is None or take is None:
            return None
        try:
            res = await (fn(recent=True) if take.recent else fn())
        except Exception as e:  # noqa: BLE001 — камеры нет: событие идёт дальше
            log.warning("alarm: cannot protect the %s recording: %s", camera, e)
            return None
        seg = res.get("segment") if isinstance(res, dict) else None
        return seg if isinstance(seg, str) and seg else None

    async def _enqueue(self, ev: AlarmEvent, cooldown: bool) -> None:
        if self._push_due(ev, cooldown):
            await self._queue(ev)

    def _push_due(self, ev: AlarmEvent, cooldown: bool) -> bool:
        """Отправлять ли событие на сервер (push): сервер настроен и (cooldown) прошла пауза push_cooldown_s этого
        вида — тогда пауза начинается заново."""
        if not self.relay.configured:
            return False
        if cooldown:
            # события камер — порознь по камерам: «подходит сзади» после «подходит спереди» — новость
            cam = ev.data.get("camera")
            key = f"{ev.kind}:{cam}" if cam else ev.kind
            now, last = self.mono(), self._last_push.get(key)
            if last is not None and now - last < self.get_settings().push_cooldown_s:
                log.info("alarm: no push for %s (cooldown)", key)
                return False
            self._last_push[key] = now
        return True

    async def _queue(self, ev: AlarmEvent, thumb: bytes | None = None) -> None:
        """В outbox; thumb — миниатюра кадра (JPEG): едет к серверу в поле thumb (base64), в журнал не попадает."""
        body = ev.model_dump(mode="json")
        if thumb:
            body["thumb"] = base64.b64encode(thumb).decode("ascii")
        try:  # база не пишется — AlarmOutbox держит событие в памяти; сюда — только совсем неожиданное
            await asyncio.to_thread(self.outbox.add, body)
        except Exception:
            log.exception("alarm: cannot queue %s for the server", ev.kind)
            return
        self._pending += 1
        self._kick.set()

    async def test(self) -> AlarmStatus:
        """Проверка уведомлений: событие test — журнал → сервер → push, без сирены и камеры, в любом состоянии."""
        ev = make_event("test", {}, self.clock())
        self._record(ev)
        await self._enqueue(ev, cooldown=False)
        await self._save_state()
        await self._save_events()
        return self.status()

    async def clear_events(self) -> AlarmStatus:
        self.events = []
        self.events_rev += 1
        await self._save_events()
        await self._run_io(self._clear_snapshots)
        return self.status()

    def _clear_snapshots(self) -> None:
        for p in [*self.snap_dir.glob("*.jpg"), *(self.snap_dir / THUMBS_DIR).glob("*.jpg")]:
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
        """Состояние + события из outbox (пачками); события удаляются только после ответа 2xx. База outbox не
        работала — сначала попытка открыть её снова (не чаще OUTBOX_RETRY_S)."""
        if self.outbox.db is None:
            await asyncio.to_thread(self.outbox.retry)
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
                await asyncio.to_thread(self.outbox.drop, rows)
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

    def journal(self, limit: int = STATUS_EVENTS) -> dict:
        """GET /api/alarm/events: журнал (новые первыми, ≤ JOURNAL_MAX) и его номер правки."""
        return {"rev": self.events_rev, "seq": self.seq,
                "events": [e.model_dump(mode="json") for e in self.events[:max(0, min(limit, JOURNAL_MAX))]]}

    def status(self, events: int | None = None) -> AlarmStatus:
        """events — сколько последних событий журнала (None — STATUS_EVENTS; 0 — без журнала: опрос страницы охраны
        берёт журнал отдельно, когда сменился events_rev)."""
        events = STATUS_EVENTS if events is None else events
        angle = self.detector.angle
        return AlarmStatus(
            state=self.state, armed_at=self.armed_at, arm_in_s=self._arm_in(),  # type: ignore[arg-type]
            sensor=AlarmSensor(ok=self.imu.ok, model=self.imu.model, address=self.imu.address, bus=self.imu.bus,
                               error=self.imu.error),
            level_g=self._level("status"),
            tilt_deg=round(angle, 1) if angle is not None and self.state in DETECTED else None,
            siren=self.siren.sounding, events=self.events[:max(0, min(events, STATUS_EVENTS))],
            seq=self.seq, events_rev=self.events_rev,
            relay=AlarmRelay(configured=self.relay.configured, pending=self._pending),
            hazard=self.hazard.status(),
            vision=self.vision_status() if self.vision_status else AlarmVision(
                enabled=self.get_settings().vision.enabled),
            cameras=self.video.status(self.get_video()) if self.get_video else [],
            storage_error=self.storage_error(),
        )

    def telemetry(self) -> AlarmTelemetry:
        last = self.events[0] if self.events else None
        return AlarmTelemetry(
            state=self.state, arm_in_s=self._arm_in(), level_g=self._level("telemetry"),  # type: ignore[arg-type]
            sensor_ok=self.imu.ok, siren=self.siren.sounding, hazard=self.hazard.active, seq=self.seq,
            last=AlarmLast(kind=last.kind, ts=last.ts, title=last.title) if last else None,
        )
