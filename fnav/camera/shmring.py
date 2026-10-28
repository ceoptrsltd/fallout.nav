"""Кольцо lores-кадров в общей памяти: fnav-camera пишет, fnav-watch читает (docs/contracts/camera.md).

Файл фиксированного размера (на Pi — /dev/shm/fnav-lores), обе стороны отображают его через mmap. Создаёт файл
читатель; писатель (поток камеры) лишь раз в секунду проверяет, появился ли он. Кадры копируются, только пока
читатель обновляет в заголовке «хочу до» (монотонные часы — общие для процессов) и не чаще нужной ему частоты.

Заголовок 64 байта: magic, version, слотов, ёмкость слота, номер последнего кадра, хочу-до, частота, pid писателя.
Слот: 64 байта заголовка (begin-seq, время, формат, размеры, экспозиция, end-seq) + данные. Писатель пишет begin,
данные, метаданные, end, затем номер последнего кадра; читатель берёт кадр, только если end == begin == номер
(seqlock без блокировок: камера никогда не ждёт читателя).
"""

from __future__ import annotations

import contextlib
import logging
import math
import mmap
import os
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("fnav.camera.shmring")

MAGIC = b"FNAVRNG1"
VERSION = 1
SLOTS = 3
HDR = struct.Struct("<8sIIQQddI")  # magic, version, slots, cap, latest, want_until, want_fps, writer_pid
HDR_SIZE = 64
OFF_LATEST = 24
OFF_WANT = 32
OFF_PID = 48
SLOT = struct.Struct("<QdIIIIIIffff")  # begin, ts, kind, w, h, stride, nbytes, pad, exp_us, again, dgain, lux
SLOT_HDR = 64
OFF_END = 56  # end-seq в заголовке слота
KINDS = {"yuv420": 0, "rgb": 1}
KIND_NAMES = {v: k for k, v in KINDS.items()}
EXPOSURE_KEYS = ("ExposureTime", "AnalogueGain", "DigitalGain", "Lux")
CHECK_S = 1.0  # писатель: как часто проверять файл, пока его нет / читатель молчит


def default_path(camera: str = "front") -> Path:
    """FNAV_LORES_RING или /dev/shm/fnav-lores (на ПК — во временном каталоге); задняя камера —
    FNAV_LORES_RING_REAR или …/fnav-lores-rear."""
    rear = camera == "rear"
    env = os.environ.get("FNAV_LORES_RING_REAR" if rear else "FNAV_LORES_RING")
    if env:
        return Path(env)
    name = "fnav-lores-rear" if rear else "fnav-lores"
    shm = Path("/dev/shm")
    return shm / name if shm.is_dir() else Path(tempfile.gettempdir()) / name


def _round64(n: int) -> int:
    return (n + 63) // 64 * 64


def file_size(cap: int, slots: int = SLOTS) -> int:
    return HDR_SIZE + slots * (SLOT_HDR + _round64(cap))


def create(path: Path, cap: int, slots: int = SLOTS) -> None:
    """Новый файл кольца (атомарно: tmp + rename — у того, кто отобразил старый, отображение остаётся целым)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.truncate(file_size(cap, slots))
        f.write(HDR.pack(MAGIC, VERSION, slots, _round64(cap), 0, 0.0, 0.0, 0))
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class _Mapped:
    """Открытое отображение файла кольца + разбор заголовка."""

    def __init__(self, path: Path) -> None:
        with open(path, "r+b") as f:
            st = os.fstat(f.fileno())
            self.ino = (st.st_dev, st.st_ino)
            self.mm = mmap.mmap(f.fileno(), 0)
        try:
            magic, ver, slots, cap = HDR.unpack_from(self.mm, 0)[:4]
            if magic != MAGIC or ver != VERSION or slots < 2 or len(self.mm) < file_size(cap, slots):
                raise ValueError(f"not a lores ring: {path}")
        except Exception:
            self.mm.close()
            raise
        self.slots, self.cap = slots, cap

    def slot_off(self, seq: int) -> int:
        return HDR_SIZE + (seq % self.slots) * (SLOT_HDR + self.cap)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.mm.close()


def _same_file(path: Path, ino: tuple[int, int]) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == ino


class RingWriter:
    """Сторона камеры. wants_frame() и write() вызываются из потока камеры: без ожиданий, ошибки — не наружу."""

    def __init__(self, path: Path | None = None, clock=time.monotonic) -> None:
        self.path = Path(path) if path else default_path()
        self.clock = clock
        self._m: _Mapped | None = None
        self._next_check = 0.0
        self._last_write = 0.0
        self._seq = 0
        self._err_logged = False
        self.frames = 0  # сколько кадров записано (для статуса и тестов)

    def _check(self, now: float) -> None:
        """Открыть файл, если появился / пересоздан (раз в CHECK_S, пока читатель не просит кадров)."""
        self._next_check = now + CHECK_S
        m = self._m
        if m is not None and _same_file(self.path, m.ino):
            return
        if m is not None:
            m.close()
            self._m = None
        if not self.path.exists():
            return
        try:
            self._m = _Mapped(self.path)
        except (OSError, ValueError) as e:
            self._log_once(f"lores ring {self.path}: {e}")
            return
        self._seq = max(self._seq, struct.unpack_from("<Q", self._m.mm, OFF_LATEST)[0])
        log.info("lores ring opened: %s", self.path)

    def wants_frame(self) -> bool:
        """Нужен ли читателю этот кадр (по «хочу до» и частоте). Дёшево: одно чтение из mmap."""
        try:
            now = self.clock()
            if self._m is None:
                if now < self._next_check:
                    return False
                self._check(now)
            m = self._m
            if m is None:
                return False
            until, fps = struct.unpack_from("<dd", m.mm, OFF_WANT)
            if now > until:
                if now >= self._next_check:
                    self._check(now)  # читатель пропал — вдруг файл пересоздан
                return False
            return fps > 0 and now - self._last_write >= 0.8 / fps
        except Exception as e:  # noqa: BLE001
            self._log_once(f"lores ring: {e}")
            return False

    def demand_fps(self) -> float:
        """Сколько кадров/с просит читатель сейчас (0 — не просит). Без учёта момента последней записи."""
        try:
            now = self.clock()
            if self._m is None:
                if now < self._next_check:
                    return 0.0
                self._check(now)
            m = self._m
            if m is None:
                return 0.0
            until, fps = struct.unpack_from("<dd", m.mm, OFF_WANT)
            return float(fps) if now <= until and fps > 0 else 0.0
        except Exception as e:  # noqa: BLE001
            self._log_once(f"lores ring: {e}")
            return 0.0

    def write(self, data: np.ndarray, kind: str, w: int, h: int, exposure: dict | None = None) -> bool:
        """Положить кадр (yuv420: (h*3/2, stride); rgb: (h, w, 3)). False — не записан (ошибка — в лог один раз)."""
        try:
            now = self.clock()
            m = self._m
            if m is None:
                return False
            arr = np.ascontiguousarray(data)
            nbytes = arr.nbytes
            if nbytes > m.cap:
                # кадр крупнее слота (сменился размер lores): пересоздать файл, читатель откроет новый
                create(self.path, nbytes, m.slots)
                self._check(now)
                m = self._m
                if m is None:
                    return False
            stride = arr.shape[1] * (arr.shape[2] if arr.ndim == 3 else 1)
            exp = exposure or {}
            vals = [float(exp.get(k, math.nan)) for k in EXPOSURE_KEYS]
            n = self._seq + 1
            off = m.slot_off(n)
            mm = m.mm
            struct.pack_into("<Q", mm, off, n)  # begin
            mm[off + SLOT_HDR:off + SLOT_HDR + nbytes] = arr.reshape(-1).data
            SLOT.pack_into(mm, off, n, now, KINDS[kind], w, h, stride, nbytes, 0, *vals)
            struct.pack_into("<Q", mm, off + OFF_END, n)  # end
            struct.pack_into("<Q", mm, OFF_LATEST, n)
            struct.pack_into("<I", mm, OFF_PID, os.getpid())
            self._seq = n
            self._last_write = now
            self.frames += 1
            return True
        except Exception as e:  # noqa: BLE001
            self._log_once(f"lores ring write: {e}")
            return False

    def _log_once(self, msg: str) -> None:
        if not self._err_logged:
            self._err_logged = True
            log.warning("%s (further errors are not logged)", msg)

    def close(self) -> None:
        if self._m is not None:
            self._m.close()
            self._m = None


@dataclass
class RingFrame:
    seq: int
    ts: float  # time.monotonic() писателя в момент записи
    kind: str  # "yuv420" | "rgb"
    w: int
    h: int
    stride: int
    data: np.ndarray  # копия: yuv420 — (h*3/2, stride), rgb — (h, w, 3)
    exposure: dict[str, float] = field(default_factory=dict)


class RingReader:
    """Сторона fnav-watch: создаёт файл, если его нет, сообщает «хочу кадры», читает последний кадр."""

    def __init__(self, path: Path | None = None, cap: int = 768 * 576 * 3, clock=time.monotonic) -> None:
        self.path = Path(path) if path else default_path()
        self.cap = cap
        self.clock = clock
        self._m: _Mapped | None = None
        self._next_check = 0.0

    def open(self) -> None:
        if self._m is not None:
            return
        try:
            self._m = _Mapped(self.path)
        except FileNotFoundError:
            create(self.path, self.cap)
            self._m = _Mapped(self.path)
        except ValueError:
            create(self.path, self.cap)  # чужой/старый формат — заменить
            self._m = _Mapped(self.path)
        self._next_check = self.clock() + CHECK_S

    def _recheck(self) -> None:
        now = self.clock()
        if now < self._next_check:
            return
        self._next_check = now + CHECK_S
        m = self._m
        if m is not None and not _same_file(self.path, m.ino):
            log.info("lores ring was recreated, reopening")
            m.close()
            self._m = None
            self.open()

    def want(self, fps: float, hold_s: float = 3.0) -> None:
        """Хочу кадры с частотой fps ещё hold_s секунд (вызывать регулярно, пока смотрим)."""
        self.open()
        self._recheck()
        assert self._m is not None
        struct.pack_into("<dd", self._m.mm, OFF_WANT, self.clock() + hold_s, float(fps))

    def stop(self) -> None:
        """Больше не нужны кадры: писатель перестаёт копировать сразу."""
        if self._m is not None:
            with contextlib.suppress(Exception):
                struct.pack_into("<dd", self._m.mm, OFF_WANT, 0.0, 0.0)

    @property
    def latest(self) -> int:
        return struct.unpack_from("<Q", self._m.mm, OFF_LATEST)[0] if self._m is not None else 0

    def read(self, after: int = 0) -> RingFrame | None:
        """Последний кадр новее after, или None. Слот, перезаписанный во время чтения, отбрасывается."""
        m = self._m
        if m is None:
            return None
        mm = m.mm
        for _ in range(3):
            n = struct.unpack_from("<Q", mm, OFF_LATEST)[0]
            if n == 0 or n <= after:
                return None
            off = m.slot_off(n)
            if struct.unpack_from("<Q", mm, off + OFF_END)[0] != n:
                continue  # слот уже переписывается
            _, ts, kind, w, h, stride, nbytes, _, *vals = SLOT.unpack_from(mm, off)
            if nbytes > m.cap or kind not in KIND_NAMES:
                return None
            buf = bytes(mm[off + SLOT_HDR:off + SLOT_HDR + nbytes])
            if struct.unpack_from("<Q", mm, off)[0] != n:
                continue  # писатель начал этот слот, пока мы копировали
            arr = np.frombuffer(buf, np.uint8)
            name = KIND_NAMES[kind]
            arr = arr.reshape(h, w, 3) if name == "rgb" else arr.reshape(-1, stride)
            exp = {k: v for k, v in zip(EXPOSURE_KEYS, vals, strict=True) if not math.isnan(v)}
            return RingFrame(n, ts, name, w, h, stride, arr, exp)
        return None

    def close(self) -> None:
        if self._m is not None:
            self._m.close()
            self._m = None
