"""Регистратор: сегменты MPEG-TS, fsync в отдельном потоке, именование по camera.md.

Что гарантирует «≤ 2 с потери»:
  * файл открыт без буферизации Python (``buffering=0``), каждый кадр (AU) → целые TS-пакеты →
    один ``write()`` (с дозаписью при частичном write) сразу в page cache ядра;
  * в муксере нет внутренних буферов (см. tsmux.py);
  * поток fsync раз в ``fsync_interval_s`` вызывает ``os.fsync(fd)`` (данные + размер файла);
  * при создании файла/каталога — fsync каталога (иначе запись каталога может не попасть на диск);
  * очередь от кодера к писателю — обычно пустая (писатель успевает), задержка ≪ 1 кадра.
Итого после обрыва питания теряется ≤ fsync_interval_s + время fsync + 1 кадр.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .h264 import AccessUnitFixer
from .tsmux import PTS_START, TsMuxer, pts_from_us

log = logging.getLogger("fnav.camera.recorder")

NOSYNC_DIR = "nosync"


# ───────────────────────────── время / boot id ─────────────────────────────

_BOOT_ID: str | None = None


def boot_id() -> str:
    global _BOOT_ID
    if _BOOT_ID is None:
        try:
            _BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
        except OSError:
            _BOOT_ID = uuid.uuid4().hex
    return _BOOT_ID


def check_time_synced(now: datetime | None = None) -> bool:
    """NTPSynchronized=yes и год ≥ 2025. Не на Linux — только проверка года."""
    now = now or datetime.now()
    if now.year < 2025:
        return False
    if not sys.platform.startswith("linux"):
        return True
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=3, check=False)
        return out.stdout.strip().lower() == "yes"
    except (OSError, subprocess.SubprocessError):
        return True  # нет timedatectl (контейнер и т.п.) — доверяем году


class TimeSync:
    """Кэш состояния синхронизации времени (обновляется в фоне, писатель не ждёт subprocess)."""

    def __init__(self, fn: Callable[[], bool] = check_time_synced) -> None:
        self._fn = fn
        self.value = False
        self.refresh()

    def refresh(self) -> bool:
        try:
            self.value = bool(self._fn())
        except Exception:
            log.exception("time sync check failed")
        return self.value


# ───────────────────────────── именование ─────────────────────────────

_NOSYNC_RE = re.compile(r"^boot-([0-9a-f]{8})_(\d{4,})\.ts$")


def dated_path(root: Path, now: datetime) -> Path:
    """{root}/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.ts; если имя занято — следующая свободная секунда."""
    t = now.replace(microsecond=0)
    while True:
        p = root / t.strftime("%Y-%m-%d") / (t.strftime("%Y-%m-%d_%H-%M-%S") + ".ts")
        if not p.exists():
            return p
        t += timedelta(seconds=1)


def nosync_path(root: Path, bid: str) -> Path:
    d = root / NOSYNC_DIR
    b8 = bid[:8]
    seq = 0
    if d.is_dir():
        for p in d.iterdir():
            m = _NOSYNC_RE.match(p.name)
            if m and m.group(1) == b8:
                seq = max(seq, int(m.group(2)) + 1)
    return d / f"boot-{b8}_{seq:04d}.ts"


def fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ───────────────────────────── файл сегмента ─────────────────────────────


class SegmentFile:
    def __init__(self, path: Path) -> None:
        new_dirs = []
        d = path.parent
        while not d.exists():
            new_dirs.append(d)
            d = d.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = open(path, "xb", buffering=0)  # noqa: SIM115 — без буфера Python
        self._lock = threading.Lock()
        self.closed = False
        self.size = 0
        self.synced_size = 0
        fsync_dir(path.parent)
        for nd in new_dirs:
            fsync_dir(nd.parent)

    def write(self, data: bytes) -> None:
        mv = memoryview(data)
        while mv:
            n = self._f.write(mv)
            if n is None:  # pragma: no cover — неблокирующий режим не используем
                continue
            mv = mv[n:]
        self.size += len(data)

    def sync(self) -> int:
        """fsync; возвращает размер, который гарантированно на диске."""
        with self._lock:
            if self.closed:
                return self.synced_size
            size = self.size
            os.fsync(self._f.fileno())
            self.synced_size = size
            return size

    def close(self, tail: bytes = b"") -> None:
        with self._lock:
            if self.closed:
                return
            try:
                if tail:
                    self.write(tail)
                os.fsync(self._f.fileno())
                self.synced_size = self.size
            finally:
                self.closed = True
                self._f.close()


# ───────────────────────────── регистратор ─────────────────────────────


@dataclass
class _AU:
    data: bytes
    keyframe: bool
    ts_us: int


class Recorder:
    """Принимает H.264 AU (из любого потока), пишет сегменты в своём потоке."""

    def __init__(
        self,
        root: Path,
        *,
        segment_s: float,
        fsync_interval_s: float,
        gop: int,
        fps: float,
        time_sync: TimeSync | None = None,
        clock: Callable[[], datetime] = datetime.now,
        request_keyframe: Callable[[], None] | None = None,
        on_segment_start: Callable[[Path], None] | None = None,
        on_sync: Callable[[Path, int, int], None] | None = None,
        queue_frames: int = 150,
    ) -> None:
        self.root = Path(root)
        self.segment_s = float(segment_s)
        self.fsync_interval_s = float(fsync_interval_s)
        self.gop = max(1, int(gop))
        self.fps = float(fps)
        self.time_sync = time_sync or TimeSync()
        self.clock = clock
        self.request_keyframe = request_keyframe
        self.on_segment_start = on_segment_start
        self.on_sync = on_sync  # (path, synced_bytes, frames_in_segment) — для тестов/диагностики

        self._q: queue.Queue[_AU | None] = queue.Queue(maxsize=queue_frames)
        self._fixer = AccessUnitFixer()
        self._mux: TsMuxer | None = None
        self._seg: SegmentFile | None = None
        self._seg_lock = threading.Lock()
        self._seg_first_ts: int | None = None
        self._seg_frames = 0
        self._rotate_pending = False
        self._pending_frames = 0
        self._need_key = True  # после старта/паузы ждём ключевой (поток писателя)
        self._resync = False  # очередь переполнялась — не ставить P-кадры до ключевого (поток источника)
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._syncer: threading.Thread | None = None
        self._last_ts: int | None = None

        self.paused = False  # нет места → не пишем
        self.write_error: str | None = None
        self._error_until = 0.0
        self.frames_dropped = 0
        self.frames_written = 0
        self.segment_rel: str | None = None
        self.segment_started_utc: str | None = None
        self.segment_time_synced = False
        self.last_frame_mono = 0.0
        self._rate: deque[tuple[float, int]] = deque()  # (ts_s, bytes) входных AU

    # ── жизненный цикл ──
    def start(self) -> None:
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="rec-writer", daemon=True)
        self._syncer = threading.Thread(target=self._sync_loop, name="rec-fsync", daemon=True)
        self._writer.start()
        self._syncer.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Дописать очередь, корректно закрыть сегмент."""
        if self._writer is None:
            return
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            pass
        self._writer.join(timeout)
        self._stop.set()
        if self._syncer:
            self._syncer.join(timeout)
        self._close_segment()
        self._writer = None

    def drain(self, timeout: float = 10.0) -> bool:
        """Дождаться, пока очередь записана (для тестов)."""
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return False

    # ── вход ──
    def push(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        self.last_frame_mono = time.monotonic()
        self._account_rate(ts_us, len(au))
        if self._last_ts is not None and self.fps > 0:
            gap = (ts_us - self._last_ts) * self.fps / 1e6
            if gap > 1.5:
                self.frames_dropped += round(gap) - 1
        self._last_ts = ts_us
        if self._resync and not keyframe:
            self.frames_dropped += 1  # цепочка P-кадров порвана — ждём ключевой
            return
        try:
            self._q.put_nowait(_AU(au, keyframe, ts_us))
            self._resync = False
        except queue.Full:
            self.frames_dropped += 1
            self._resync = True

    def _account_rate(self, ts_us: int, n: int) -> None:
        t = ts_us / 1e6
        self._rate.append((t, n))
        while self._rate and t - self._rate[0][0] > 3.0:
            self._rate.popleft()

    def rates(self) -> tuple[float | None, float | None]:
        """(fps, kbps) по последним ~3 с."""
        r = list(self._rate)
        if len(r) < 2:
            return None, None
        dt = r[-1][0] - r[0][0]
        if dt <= 0:
            return None, None
        fps = (len(r) - 1) / dt
        kbps = sum(n for _, n in r[1:]) * 8 / dt / 1000
        return round(fps, 2), round(kbps, 1)

    # ── защита ──
    def protect_current(self) -> str | None:
        with self._seg_lock:
            seg = self._seg
            rel = self.segment_rel
        if seg is None or rel is None:
            return None
        lock = seg.path.with_name(seg.path.name + ".lock")
        lock.touch(exist_ok=True)
        fsync_dir(lock.parent)
        return rel

    @property
    def current_path(self) -> Path | None:
        seg = self._seg
        return seg.path if seg else None

    # ── писатель ──
    def _writer_loop(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                self._handle(item)
            except Exception:
                log.exception("recorder: unexpected error")
            finally:
                self._q.task_done()

    def _handle(self, item: _AU) -> None:
        au, key = self._fixer.fix(item.data, item.keyframe)
        if self.paused or time.monotonic() < self._error_until:
            if self._seg is not None:
                self._close_segment()
            self._need_key = True
            return
        if self._need_key:
            if not key or not self._fixer.has_params:
                return
            self._need_key = False  # после пропуска кадров продолжаем тот же сегмент

        if self._seg is not None and self._seg_first_ts is not None:
            # допуск полкадра: ключевой кадр «ровно на границе» не должен уезжать на следующий GOP
            tol = 0.5 / self.fps if self.fps > 0 else 0.0
            if not self._rotate_pending and (item.ts_us - self._seg_first_ts) / 1e6 >= self.segment_s - tol:
                self._rotate_pending = True
                self._pending_frames = 0
            if self._rotate_pending:
                if key:
                    self._close_segment()
                else:
                    self._pending_frames += 1
                    if self._pending_frames >= 2 * self.gop and self._pending_frames % self.gop == 0:
                        log.warning("no keyframe for %d frames, requesting one", self._pending_frames)
                        if self.request_keyframe:
                            try:
                                self.request_keyframe()
                            except Exception:
                                log.exception("request_keyframe failed")

        try:
            if self._seg is None:
                if not key:
                    return
                self._open_segment(item.ts_us)
            assert self._seg is not None and self._mux is not None and self._seg_first_ts is not None
            pts = PTS_START + pts_from_us(item.ts_us - self._seg_first_ts)
            self._seg.write(self._mux.mux(au, pts, keyframe=key))
            self._seg_frames += 1
            self.frames_written += 1
        except OSError as e:
            log.error("recorder write error: %s", e)
            self.write_error = f"Recording write error: {e}"
            self._error_until = time.monotonic() + 5.0
            self._close_segment(silent=True)
            self._need_key = True

    def _open_segment(self, first_ts: int) -> None:
        now = self.clock()
        synced = self.time_sync.value
        path = dated_path(self.root, now) if synced else nosync_path(self.root, boot_id())
        seg = SegmentFile(path)
        mux = TsMuxer()
        seg.write(mux.tables())
        with self._seg_lock:
            self._seg = seg
            self._mux = mux
            self.segment_rel = path.relative_to(self.root).as_posix()
        self._seg_first_ts = first_ts
        self._seg_frames = 0
        self._rotate_pending = False
        self.segment_started_utc = datetime.now(UTC).isoformat(timespec="seconds")
        self.segment_time_synced = synced
        self.write_error = None
        log.info("segment started: %s", self.segment_rel)
        if self.on_segment_start:
            try:
                self.on_segment_start(path)
            except Exception:
                log.exception("on_segment_start failed")

    def _close_segment(self, silent: bool = False) -> None:
        with self._seg_lock:
            seg, mux = self._seg, self._mux
            self._seg = None
            self._mux = None
            rel = self.segment_rel
            self.segment_rel = None
        if seg is None:
            return
        try:
            seg.close(mux.end_marker() if mux else b"")
            if not silent:
                log.info("segment closed: %s (%d frames, %.1f MB)", rel, self._seg_frames, seg.size / 1e6)
        except OSError as e:
            log.error("segment close error %s: %s", rel, e)
        self._seg_first_ts = None

    # ── fsync ──
    def _sync_loop(self) -> None:
        while not self._stop.wait(self.fsync_interval_s):
            seg = self._seg
            if seg is None:
                continue
            frames = self._seg_frames
            try:
                size = seg.sync()
            except (OSError, ValueError) as e:
                if not seg.closed:
                    log.error("fsync failed: %s", e)
                continue
            if self.on_sync:
                try:
                    self.on_sync(seg.path, size, frames)
                except Exception:
                    log.exception("on_sync failed")
