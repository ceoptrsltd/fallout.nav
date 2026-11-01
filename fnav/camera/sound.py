"""Громкие звуки у задней камеры, пока стоит охрана (docs/contracts/vision.md «Звук»): отметка с клипом, без тревоги.

Вход — PCM s16 моно RATE Гц с микрофона IP-камеры (rtsp_pull ``--audio``). Уровень — по кускам BLOCK_S (RMS, dBFS),
фон — FLOOR_PCT-й перцентиль уровня за FLOOR_WINDOW_S: ровный шум (ветер, дождь, дорога вдали) — это фон. Громко —
кусок на LOUD_OVER_DB выше фона и не тише LOUD_MIN_DB. Пока охрана стоит (``armed``): с первого громкого куска пишется
клип WAV (за PRE_S до звука и до GAP_S тишины после последнего громкого куска, не длиннее MAX_EPISODE_S); громкого
набралось меньше MIN_LOUD_S (щелчок) — клип выбрасывается. Иначе — событие {seq, start, dur_s, peak_db, floor_db,
clip, clip_start, clip_dur_s} (время — unix), его забирает fnav-core (отметка «sound» на записи задней камеры).
Охрана снята — клипов нет: разговоры в фургоне не пишутся. Клипы — data_dir/sound_clips (не больше CLIPS_MAX, CLIPS_MAX_MB
и CLIPS_MAX_AGE_S; старые удаляются при каждом новом).
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

log = logging.getLogger("fnav.camera.sound")

RATE = 8000
BLOCK_S = 0.1
BLOCK = int(RATE * BLOCK_S)
FLOOR_WINDOW_S = 30.0
FLOOR_MIN_S = 3.0  # пока фон не набран — громкого не ищем
FLOOR_PCT = 20
LOUD_OVER_DB = 18.0
LOUD_MIN_DB = -40.0
MIN_LOUD_S = 0.2
GAP_S = 3.0
MAX_EPISODE_S = 60.0
PRE_S = 5.0
LEVEL_S = 1.0  # уровень в статусе — средний за столько
CLIPS_MAX = 300
CLIPS_MAX_MB = 200
CLIPS_MAX_AGE_S = 30 * 86400
EVENTS_KEEP = 50
SILENCE_DB = -96.0
CLIP_RE = re.compile(r"^\d{8}-\d{6}-\d{3}\.wav$")


def level_db(block: np.ndarray) -> float:
    if not block.size:
        return SILENCE_DB
    rms = math.sqrt(float(np.mean(np.square(block.astype(np.float32)))))
    return max(SILENCE_DB, 20 * math.log10(rms / 32768.0)) if rms > 0 else SILENCE_DB


def clip_name(wall: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(wall)) + f"-{int(wall * 1000) % 1000:03d}.wav"


class _Clip:
    def __init__(self, path: Path, first_us: int) -> None:
        self.path = path
        self.part = path.with_name(path.name + ".part")
        self.first_us = first_us
        self.samples = 0
        self.w = wave.open(str(self.part), "wb")  # noqa: SIM115 — открыт, пока идёт звук; закрывает finish()
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(RATE)

    def write(self, block: np.ndarray) -> None:
        self.w.writeframes(block.astype("<i2").tobytes())
        self.samples += len(block)

    def finish(self, keep: bool) -> None:
        self.w.close()
        if keep:
            os.replace(self.part, self.path)
        else:
            self.part.unlink(missing_ok=True)


class SoundMonitor:
    def __init__(self, clip_dir: Path, *, wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.clip_dir = clip_dir
        self.wall = wall
        self.mono = mono
        self.seq = 0
        self.floor_db: float | None = None
        self._armed = False
        self._lock = threading.Lock()
        self._buf = np.zeros(0, np.int16)
        self._buf_us: int | None = None  # метка первого отсчёта в _buf (монотонные мкс)
        self._levels: deque[float] = deque(maxlen=int(FLOOR_WINDOW_S / BLOCK_S))
        self._recent: deque[float] = deque(maxlen=int(LEVEL_S / BLOCK_S))  # мощность, для уровня в статусе
        self._pre: deque[tuple[int, np.ndarray]] = deque(maxlen=int(PRE_S / BLOCK_S))
        self._events: deque[dict] = deque(maxlen=EVENTS_KEEP)
        self._ep: dict | None = None
        self._clean_parts()

    # ── управление ──
    @property
    def armed(self) -> bool:
        return self._armed

    @armed.setter
    def armed(self, on: bool) -> None:
        with self._lock:
            if self._armed and not on and self._ep is not None:
                self._finish()  # охрану сняли посреди звука — что успели, то отметка
            self._armed = bool(on)

    @property
    def level_db(self) -> float | None:
        r = self._recent
        if not r:
            return None
        p = sum(r) / len(r)
        return round(max(SILENCE_DB, 10 * math.log10(p)) if p > 0 else SILENCE_DB, 1)

    def events_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._events if e["seq"] > seq]

    # ── поток звука ──
    def feed(self, pcm: bytes, ts_us: int) -> None:
        """Кусок PCM s16le моно RATE Гц; ts_us — его начало на монотонных часах (мкс)."""
        x = np.frombuffer(pcm, "<i2")
        if not x.size:
            return
        with self._lock:
            if self._buf_us is None or abs(self._buf_us + len(self._buf) * 1_000_000 // RATE - ts_us) > 500_000:
                self._buf, self._buf_us = np.zeros(0, np.int16), ts_us  # пропуск или скачок меток — заново
            self._buf = np.concatenate((self._buf, x))
            while len(self._buf) >= BLOCK:
                blk, self._buf = self._buf[:BLOCK], self._buf[BLOCK:]
                t = self._buf_us
                self._buf_us = t + BLOCK * 1_000_000 // RATE
                self._block(blk, t)

    def _block(self, blk: np.ndarray, t_us: int) -> None:
        lv = level_db(blk)
        self._recent.append(10 ** (lv / 10))
        self._levels.append(lv)
        if len(self._levels) >= FLOOR_MIN_S / BLOCK_S:
            self.floor_db = float(np.percentile(np.fromiter(self._levels, float), FLOOR_PCT))
        loud = self.floor_db is not None and lv >= max(self.floor_db + LOUD_OVER_DB, LOUD_MIN_DB)
        ep = self._ep
        if ep is None:
            if loud and self._armed:
                self._start(t_us, lv)
                if self._ep is not None:
                    self._ep["clip"].write(blk)
        else:
            ep["clip"].write(blk)
            if loud:
                ep["last"] = t_us
                ep["loud_s"] += BLOCK_S
                ep["peak"] = max(ep["peak"], lv)
            if t_us - ep["last"] >= GAP_S * 1e6 or t_us - ep["start"] >= MAX_EPISODE_S * 1e6:
                self._finish()
        self._pre.append((t_us, blk))  # последние PRE_S всегда: и после выброшенного щелчка

    def _to_wall(self, t_us: int) -> float:
        return self.wall() - (self.mono() - t_us / 1e6)

    def _start(self, t_us: int, lv: float) -> None:
        first = self._pre[0][0] if self._pre else t_us
        try:
            self.clip_dir.mkdir(parents=True, exist_ok=True)
            clip = _Clip(self.clip_dir / clip_name(self._to_wall(t_us)), first)
            for _, b in self._pre:
                clip.write(b)
        except OSError as e:
            log.warning("sound clip: %s", e)
            return
        self._ep = {"start": t_us, "last": t_us, "loud_s": BLOCK_S, "peak": lv, "floor": self.floor_db, "clip": clip}

    def _finish(self) -> None:
        ep, self._ep = self._ep, None
        if ep is None:
            return
        clip: _Clip = ep["clip"]
        keep = ep["loud_s"] >= MIN_LOUD_S - 1e-6
        try:
            clip.finish(keep)
        except OSError as e:
            log.warning("sound clip: %s", e)
            return
        if not keep:
            return
        self.seq += 1
        ev = {"seq": self.seq, "start": round(self._to_wall(ep["start"]), 3),
              "dur_s": round((ep["last"] - ep["start"]) / 1e6 + BLOCK_S, 1), "peak_db": round(ep["peak"], 1),
              "floor_db": round(ep["floor"], 1) if ep["floor"] is not None else None, "clip": clip.path.name,
              "clip_start": round(self._to_wall(clip.first_us), 3), "clip_dur_s": round(clip.samples / RATE, 1)}
        self._events.append(ev)
        log.info("loud sound: %s", {k: v for k, v in ev.items() if k != "seq"})
        self._prune()

    # ── клипы на диске ──
    def _clean_parts(self) -> None:
        try:
            for p in self.clip_dir.glob("*.wav.part"):
                p.unlink(missing_ok=True)  # оборванный клип (питание пропало посреди звука)
        except OSError:
            pass

    def _prune(self) -> None:
        try:
            files = sorted(p for p in self.clip_dir.iterdir() if CLIP_RE.match(p.name))
        except OSError:
            return
        cut = self.wall() - CLIPS_MAX_AGE_S
        sizes = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            sizes.append((p, st.st_size, st.st_mtime))
        total = sum(s for _, s, _ in sizes)
        for i, (p, size, mtime) in enumerate(sizes):
            if len(sizes) - i <= CLIPS_MAX and total <= CLIPS_MAX_MB * 1e6 and mtime >= cut:
                break
            p.unlink(missing_ok=True)
            total -= size
