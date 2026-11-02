"""Трек GPS к записям (docs/contracts/camera.md «Трек GPS записи»): раз в секунду точка из хаба датчиков → строка в
боковой файл ``<сегмент>.ts.gps`` текущего сегмента каждой камеры (fnav/camera/gpstrack.py). Плеер админки показывает
по нему скорость и место в момент воспроизведения (сверка — по unix-времени точки и start_ts записи).

Текущий сегмент — из статуса сервиса камеры (ядро опрашивает его раз в секунду). На смене сегмента новый файл
начинается с последней точки прошлого: начало записи не остаётся без места. Файл для записи, которой нет (ещё не
создана или уже удалена очисткой), не создаётся. Запись на карту — в потоке; fsync — раз в FSYNC_S (при обрыве питания
теряются последние секунды трека, как у .ts.idx).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import BinaryIO

from fnav.camera.gpstrack import encode, gps_path, make_sample
from fnav.core.camera import STATUS_STALE_S, CameraService
from fnav.core.recordings import RecordingError, resolve
from fnav.core.util import cancel_tasks
from fnav.models import GpsFix

log = logging.getLogger("fnav.core.recgps")

EVERY_S = 1.0
FSYNC_S = 5.0
MAX_AGE_S = 3.0  # точка GPS старше — не пишется (gpsd молчит: это уже не «здесь и сейчас»)
CARRY_S = 3.0  # последняя точка прошлого сегмента — в начало нового, если она не старше


def sample_of(fix: GpsFix, now: float) -> dict | None:
    """Точка трека из снимка хаба (время — когда GPS её дал: now − age_s); нет фикса или устарела — None."""
    age = fix.age_s if isinstance(fix.age_s, (int, float)) and fix.age_s >= 0 else 0.0
    if fix.fix == "none" or age > MAX_AGE_S:
        return None
    return make_sample(now - age, fix.lat, fix.lon, alt=fix.alt_m, speed=fix.speed_mps, course=fix.course_deg,
                       fix=fix.fix)


def current_segment(cam: CameraService) -> str | None:
    """Сегмент, который камера пишет сейчас (путь записи); выключена, не пишет, статус устарел, демо-камера --mock —
    None (демо-записи не растут: трек в них не нужен)."""
    if not cam.enabled or cam.using_mock or time.monotonic() - cam.status_mono > STATUS_STALE_S:
        return None
    st = cam.status
    return st.current_segment if st.current_segment and (st.recording or st.state == "recording") else None


class RecGps:
    def __init__(self, root: Callable[[], Path], get_fix: Callable[[], GpsFix], cameras: Iterable[CameraService],
                 *, clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.root = root
        self.get_fix = get_fix
        self.cameras = tuple(cameras)
        self.clock = clock
        self.mono = mono
        self._files: dict[str, tuple[BinaryIO, float]] = {}  # путь записи → (файл трека, когда был fsync)
        self._failed: set[str] = set()  # не открылся / не пишется: в лог один раз
        self._last: dict | None = None  # последняя записанная точка
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="recgps")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        self._task = None
        await asyncio.to_thread(self.write, [], None)  # закрыть файлы (с fsync)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(EVERY_S)
            try:
                await self.tick()
            except Exception:
                log.exception("recgps tick")

    async def tick(self) -> None:
        segs = [s for s in (current_segment(c) for c in self.cameras) if s]
        await asyncio.to_thread(self.write, segs, sample_of(self.get_fix(), self.clock()))

    # ── в потоке ──
    def write(self, segs: list[str], sample: dict | None) -> None:
        """Точку — в треки сегментов segs (у каждой камеры свой текущий); остальные открытые — закрыть."""
        for rel in [r for r in self._files if r not in segs]:
            self._close(rel)
        self._failed &= set(segs)
        if sample is not None and self._last is not None and sample["t"] <= self._last["t"]:
            sample = None  # GPS с прошлого раза не обновлялся
        for rel in dict.fromkeys(segs):
            fresh = rel not in self._files
            if fresh and not self._open(rel):
                continue
            lines = []
            if fresh and self._last is not None and self.clock() - self._last["t"] <= CARRY_S:
                lines.append(self._last)  # новый сегмент — с последней точки прошлого (она раньше sample)
            if sample is not None:
                lines.append(sample)
            if lines:
                self._put(rel, b"".join(encode(x) for x in lines))
        if sample is not None:
            self._last = sample

    def _open(self, rel: str) -> bool:
        try:
            ts = resolve(self.root(), rel)
            if not ts.is_file():
                return False
            f = open(gps_path(ts), "ab", buffering=0)  # noqa: SIM115 — держим открытым, пока сегмент текущий
        except (OSError, RecordingError) as e:
            if rel not in self._failed:
                self._failed.add(rel)
                log.warning("recgps: cannot open the GPS track of %s: %s", rel, e)
            return False
        self._files[rel] = (f, self.mono())
        return True

    def _put(self, rel: str, data: bytes) -> None:
        f, synced = self._files[rel]
        try:
            f.write(data)
            if self.mono() - synced >= FSYNC_S:
                os.fsync(f.fileno())
                self._files[rel] = (f, self.mono())
        except OSError as e:
            if rel not in self._failed:
                self._failed.add(rel)
                log.warning("recgps: cannot write the GPS track of %s: %s", rel, e)
            self._close(rel)

    def _close(self, rel: str) -> None:
        f, _ = self._files.pop(rel)
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
        try:
            f.close()
        except OSError:
            pass
