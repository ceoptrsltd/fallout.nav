"""Индекс ключевых кадров MPEG-TS записи: боковой файл регистратора и быстрый поиск по файлу без него.

Индекс — список (смещение, PTS) ключевых кадров. Смещение — начало пакета, с которого начинается кусок для
HLS: PAT/PMT, записанные перед IDR (tsmux пишет их перед каждым ключевым кадром), иначе сам пакет с IDR.

Источники (по порядку):
  * ``<запись>.ts.idx`` — пишет регистратор, строка на каждый ключевой кадр (см. docs/contracts/camera.md);
    в заголовке — истинное начало записи (unix, UTC): имя файла — по местному времени, а пояс меняется в дороге;
  * поиск по файлу — для записей без бокового файла и для хвоста после его последней строки.
    Хвост (≤ DENSE_TAIL) просматривается подряд, остальное — выборочно: битрейт и GOP почти постоянны,
    поэтому место ключевого кадра через ~CHUNK_S секунд предсказуемо — читаем окно чуть раньше него
    до первого ключевого кадра. Читается ~1/10 файла, разбор пакетов — numpy.
Индекс растёт вместе с файлом (запись идёт): ``update_index(path, prev)`` дочитывает только новое.
"""

from __future__ import annotations

import itertools
import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

TS = 188
SYNC = 0x47
PTS_HZ = 90000
PTS_MASK = (1 << 33) - 1

SIDECAR_SUFFIX = ".idx"
SIDECAR_MAGIC = b"fnav-tsindex 2\n"  # 2: сразу после заголовка — строка «start <unix>» (может не быть)
_MAGICS = (SIDECAR_MAGIC, b"fnav-tsindex 1\n")  # 1 — записи до смены формата, без начала
_START = b"start "

DENSE_TAIL = 4 * 1024 * 1024  # хвост файла меньше этого — подряд (все ключевые кадры)
CHUNK_S = 3.0  # длительность куска HLS, с
CHUNK_MAX_BYTES = 3 * 1024 * 1024  # кусок не растёт дальше (высокий битрейт): быстрая перемотка
_BLOCK = TS * 11155  # ≈ 2 МБ — чтение подряд
_PROBE = TS * 349  # ≈ 64 КБ — окно выборочного поиска
_BACK = 8  # пакетов назад: PAT/PMT перед ключевым кадром
_HEAD = 512 * 1024  # начало файла: выравнивание, PAT/PMT
_TAIL = 512 * 1024  # конец файла: PTS последнего кадра


def sidecar_path(ts: Path) -> Path:
    return ts.with_name(ts.name + SIDECAR_SUFFIX)


# ───────────────────────────── боковой файл ─────────────────────────────


class SidecarWriter:
    """Строка ``<смещение> <pts>\\n`` на ключевой кадр, без буфера (сразу в ядро, без fsync).
    start_ts — истинное начало записи (unix, UTC): строка ``start <unix>`` сразу после заголовка."""

    def __init__(self, ts: Path, start_ts: float | None = None) -> None:
        self.path = sidecar_path(ts)
        self._f = open(self.path, "wb", buffering=0)  # noqa: SIM115
        head = SIDECAR_MAGIC
        if start_ts is not None and math.isfinite(start_ts):
            head += b"%s%.3f\n" % (_START, start_ts)
        self._f.write(head)

    def add(self, offset: int, pts: int) -> None:
        self._f.write(b"%d %d\n" % (offset, pts & PTS_MASK))

    def close(self) -> None:
        self._f.close()


def _header(data: bytes) -> tuple[float | None, int] | None:
    """Заголовок бокового файла → (начало записи unix или None, где начинаются строки индекса); не наш — None."""
    magic = next((m for m in _MAGICS if data.startswith(m)), None)
    if magic is None:
        return None
    pos = len(magic)
    if magic != SIDECAR_MAGIC or not data.startswith(_START, pos):
        return None, pos
    end = data.find(b"\n", pos)
    if end < 0:  # строка недописана
        return None, len(data)
    try:
        v = float(data[pos + len(_START):end])
    except ValueError:
        v = math.nan
    return (v if math.isfinite(v) and v > 0 else None), end + 1


def read_start(ts: Path) -> float | None:
    """Истинное начало записи (unix, UTC) из бокового файла; нет (старая запись, nosync, нет файла) — None."""
    try:
        with open(sidecar_path(ts), "rb") as f:
            head = _header(f.read(64))
    except OSError:
        return None
    return head[0] if head else None


def read_sidecar(ts: Path) -> list[tuple[int, int]]:
    """Строки бокового файла; недописанная последняя и всё после первой ошибки — отбрасываются."""
    try:
        data = sidecar_path(ts).read_bytes()
    except OSError:
        return []
    head = _header(data)
    if head is None:
        return []
    out: list[tuple[int, int]] = []
    for line in data[head[1]:].split(b"\n")[:-1]:
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            break
        off, pts = int(parts[0]), int(parts[1])
        if out and off <= out[-1][0]:
            break
        out.append((off, pts))
    return out


# ───────────────────────────── разбор пакетов ─────────────────────────────


@dataclass(frozen=True)
class Layout:
    align: int  # смещение первого пакета (обычно 0)
    vpid: int  # PID видео
    psi: frozenset[int]  # PID таблиц, которые идут перед ключевым кадром (PAT, PMT, SDT)


def _pid(pkt: bytes, off: int = 0) -> int:
    return ((pkt[off + 1] & 0x1F) << 8) | pkt[off + 2]


def _payload(pkt: bytes) -> int | None:
    """Начало полезной нагрузки пакета (после адаптационного поля) или None."""
    afc = (pkt[3] >> 4) & 3
    if not afc & 1:
        return None
    i = 4 + (1 + pkt[4] if afc & 2 else 0)
    return i if i < TS else None


def _pts(b: bytes) -> int:
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def _has_idr(es: bytes) -> bool:
    """В начале данных H.264 есть IDR или SPS (для муксеров без флага random_access)."""
    i = es.find(b"\x00\x00\x01")
    while 0 <= i < len(es) - 3:
        if (es[i + 3] & 0x1F) in (5, 7):
            return True
        i = es.find(b"\x00\x00\x01", i + 3)
    return False


def pes_info(pkt: bytes) -> tuple[bool, int | None]:
    """Первый пакет PES видео → (ключевой кадр?, PTS)."""
    afc = (pkt[3] >> 4) & 3
    rai = bool(afc & 2 and pkt[4] > 0 and pkt[5] & 0x40)
    i = _payload(pkt)
    if i is None or i + 9 > TS or pkt[i:i + 3] != b"\x00\x00\x01":
        return False, None
    pts = _pts(pkt[i + 9:i + 14]) if pkt[i + 7] & 0x80 and i + 14 <= TS else None
    if not rai:
        rai = _has_idr(pkt[i + 9 + pkt[i + 8]:])
    return rai, pts


def _section(pkt: bytes) -> bytes | None:
    i = _payload(pkt)
    if i is None:
        return None
    i += 1 + pkt[i]  # pointer_field
    return pkt[i:] if i < TS else None


def _pat_pmt_pid(pkt: bytes) -> int | None:
    s = _section(pkt)
    if not s or len(s) < 12 or s[0] != 0x00:
        return None
    end = min(len(s), 3 + (((s[1] & 0x0F) << 8) | s[2]) - 4)
    for k in range(8, end - 3, 4):
        if (s[k] << 8) | s[k + 1]:  # program_number 0 — сетевая информация
            return ((s[k + 2] & 0x1F) << 8) | s[k + 3]
    return None


def _pmt_video_pid(pkt: bytes) -> int | None:
    s = _section(pkt)
    if not s or len(s) < 16 or s[0] != 0x02:
        return None
    end = min(len(s), 3 + (((s[1] & 0x0F) << 8) | s[2]) - 4)
    k = 12 + (((s[10] & 0x0F) << 8) | s[11])
    found = None
    while k + 5 <= end:
        st, pid = s[k], ((s[k + 1] & 0x1F) << 8) | s[k + 2]
        if st == 0x1B:  # H.264
            return pid
        if found is None and st in (0x01, 0x02, 0x10, 0x24):
            found = pid
        k += 5 + (((s[k + 3] & 0x0F) << 8) | s[k + 4])
    return found


def layout(head: bytes) -> Layout | None:
    """Выравнивание и PID видео по началу файла."""
    align = None
    for a in range(min(TS, len(head))):
        if head[a] == SYNC and all(head[a + k * TS] == SYNC for k in range(1, 4) if a + k * TS < len(head)):
            align = a
            break
    if align is None:
        return None
    pmt = vpid = pes_pid = None
    for off in range(align, len(head) - TS + 1, TS):
        pkt = head[off:off + TS]
        if pkt[0] != SYNC or not pkt[1] & 0x40:
            continue
        pid = _pid(pkt)
        if pid == 0 and pmt is None:
            pmt = _pat_pmt_pid(pkt)
        elif pmt is not None and pid == pmt:
            vpid = _pmt_video_pid(pkt)
            if vpid is not None:
                break
        elif pes_pid is None:
            i = _payload(pkt)
            if i is not None and pkt[i:i + 3] == b"\x00\x00\x01" and 0xE0 <= pkt[i + 3] <= 0xEF:
                pes_pid = pid
    vpid = vpid if vpid is not None else pes_pid
    if vpid is None:
        return None
    return Layout(align, vpid, frozenset({0x0000, 0x0011} | ({pmt} if pmt is not None else set())))


def _unwrap(raw: int, ref: int) -> int:
    """PTS (33 бита) → монотонная шкала относительно ref."""
    d = (raw - ref) & PTS_MASK
    if d >= 1 << 32:
        d -= 1 << 33
    return ref + d


def _scan(f, lay: Layout, lo: int, hi: int, first_only: bool) -> tuple[list[tuple[int, int, int]], int]:
    """Ключевые кадры, чей первый пакет в [lo, hi): [(начало куска, pts, пакет IDR)], докуда просмотрено."""
    out: list[tuple[int, int, int]] = []
    pos = lo
    step = _PROBE if first_only else _BLOCK
    while pos < hi:
        start = max(lay.align, pos - _BACK * TS)
        f.seek(start)
        buf = f.read(min(hi, pos + step) - start)
        n = len(buf) // TS
        if n <= (pos - start) // TS:
            break
        a = np.frombuffer(buf, np.uint8, count=n * TS).reshape(n, TS)
        pid = ((a[:, 1] & 0x1F).astype(np.int32) << 8) | a[:, 2]
        sync = a[:, 0] == SYNC
        cand = np.flatnonzero(sync & ((a[:, 1] & 0x40) != 0) & (pid == lay.vpid))
        r0 = (pos - start) // TS
        for r in cand[cand >= r0].tolist():
            key, pts = pes_info(buf[r * TS:(r + 1) * TS])
            if not key or pts is None:
                continue
            c = r
            while c > 0 and r - c < _BACK and sync[c - 1] and int(pid[c - 1]) in lay.psi:
                c -= 1
            out.append((start + c * TS, pts, start + r * TS))
            if first_only:
                return out, start + (r + 1) * TS
        pos = start + n * TS
    return out, pos


def _tail_end_pts(f, lay: Layout, end: int, ref: int) -> int | None:
    """PTS сразу после последнего кадра: PTS последнего + длительность кадра (по хвосту файла)."""
    start = max(lay.align, end - _TAIL // TS * TS)
    f.seek(start)
    buf = f.read(end - start)
    n = len(buf) // TS
    if not n:
        return None
    a = np.frombuffer(buf, np.uint8, count=n * TS).reshape(n, TS)
    pid = ((a[:, 1] & 0x1F).astype(np.int32) << 8) | a[:, 2]
    rows = np.flatnonzero((a[:, 0] == SYNC) & ((a[:, 1] & 0x40) != 0) & (pid == lay.vpid))
    pts = sorted({_unwrap(p, ref) for r in rows.tolist()
                  if (p := pes_info(buf[r * TS:(r + 1) * TS])[1]) is not None})
    if not pts:
        return None
    steps = sorted(b - a_ for a_, b in itertools.pairwise(pts) if b > a_)
    return pts[-1] + (steps[len(steps) // 2] if steps else 0)


# ───────────────────────────── индекс ─────────────────────────────


@dataclass
class KeyIndex:
    size: int = 0
    mtime_ns: int = 0
    layout: Layout | None = None
    keys: list[tuple[int, int]] = field(default_factory=list)  # (начало куска, pts без переполнения)
    pos: int = 0  # отсюда продолжать поиск
    end_pts: int | None = None  # PTS после последнего кадра
    from_sidecar: int = 0  # сколько ключевых кадров взято из бокового файла
    gop: int = 0  # наименьший интервал между ключевыми кадрами (≈ GOP), тиков 90 кГц

    @property
    def end(self) -> int:
        """Конец последнего целого пакета."""
        a = self.layout.align if self.layout else 0
        return a + max(0, self.size - a) // TS * TS

    def byte_rate(self) -> float | None:
        """Байт/с по найденным ключевым кадрам (нужно ≥ 2 с между первым и последним)."""
        k = self.keys
        if len(k) < 2 or k[-1][1] - k[0][1] < 2 * PTS_HZ:
            return None
        return (k[-1][0] - k[0][0]) * PTS_HZ / (k[-1][1] - k[0][1])

    def probe_step(self) -> int:
        """Байт от последнего ключевого кадра до начала следующей пробы (0 — битрейт ещё неизвестен):
        чуть раньше ключевого кадра, ожидаемого через ~CHUNK_S (целое число GOP по местному битрейту)."""
        rate = self.byte_rate()
        if rate is None:
            return 0
        w = self.keys[-8:]
        if w[-1][1] - w[0][1] >= PTS_HZ:
            rate = (w[-1][0] - w[0][0]) * PTS_HZ / (w[-1][1] - w[0][1])
        n = max(1, round(CHUNK_S * PTS_HZ / self.gop))
        return int(rate * self.gop / PTS_HZ * (n - 0.25))


def _add(idx: KeyIndex, found) -> None:
    for chunk, raw, _pkt in found:
        if idx.keys:
            last_off, last_pts = idx.keys[-1]
            pts = _unwrap(raw, last_pts)
            if chunk <= last_off or pts <= last_pts:
                continue
            idx.gop = min(idx.gop or pts - last_pts, pts - last_pts)
        else:  # первый кусок — с начала файла (PAT/PMT перед первым кадром)
            chunk, pts = idx.layout.align if idx.layout else 0, raw
        idx.keys.append((chunk, pts))


def _merge_sidecar(f, path: Path, idx: KeyIndex) -> None:
    """Ключевые кадры из бокового файла после уже найденных; последний — перепроверяется поиском."""
    lay = idx.layout
    assert lay is not None
    end = idx.end
    side = [(o, p) for o, p in read_sidecar(path) if idx.pos <= o < end and (o - lay.align) % TS == 0]
    if not side:
        return
    for off in {side[0][0], side[-1][0]}:  # боковой файл от этой записи? (выборочная проверка)
        f.seek(off)
        pkt = f.read(TS)
        if len(pkt) < TS or pkt[0] != SYNC or _pid(pkt) not in lay.psi | {lay.vpid}:
            return
    n = len(idx.keys)
    _add(idx, [(o, p, o) for o, p in side[:-1]])
    idx.from_sidecar += len(idx.keys) - n
    idx.pos = side[-1][0]


def update_index(path: Path, prev: KeyIndex | None = None) -> KeyIndex:
    """Индекс файла; prev — прошлый индекс того же файла (файл дописывается — дочитать только новое)."""
    st = os.stat(path)
    if prev is not None and prev.size == st.st_size and prev.mtime_ns == st.st_mtime_ns:
        return prev
    with open(path, "rb") as f:
        if prev is not None and prev.layout is not None and st.st_size >= prev.size:
            idx = replace(prev, keys=list(prev.keys))
        else:
            lay = layout(f.read(_HEAD))
            idx = KeyIndex(layout=lay, pos=lay.align if lay else 0)
        idx.size, idx.mtime_ns = st.st_size, st.st_mtime_ns
        lay = idx.layout
        if lay is None:
            return idx
        end = idx.end
        _merge_sidecar(f, path, idx)
        while idx.pos < end:
            step = idx.probe_step()
            if not step or end - idx.pos <= DENSE_TAIL + step:
                found, pos = _scan(f, lay, idx.pos, min(end, idx.pos + _BLOCK), first_only=False)
                _add(idx, found)
                if pos <= idx.pos:  # файл укоротился во время чтения
                    break
                idx.pos = pos
                continue
            last = idx.keys[-1][0]
            t = lay.align + (max(idx.pos, last + step) - lay.align) // TS * TS
            found, scanned = _scan(f, lay, t, end, first_only=True)
            if not found:
                idx.pos = scanned
                break
            _add(idx, found)
            idx.pos = found[0][2] + TS
        idx.end_pts = _tail_end_pts(f, lay, end, idx.keys[-1][1]) if idx.keys else None
    return idx


def chunks(idx: KeyIndex, *, live: bool, target_s: float = CHUNK_S,
           max_bytes: int = CHUNK_MAX_BYTES) -> list[tuple[int, int, float]]:
    """Куски HLS: [(смещение, длина, длительность с)], каждый — с ключевого кадра (и PAT/PMT перед ним).

    Границы зависят только от предшествующих ключевых кадров — у растущего файла старые куски не меняются.
    live (запись идёт): последний, ещё не законченный кусок не выдаётся."""
    keys, lay = idx.keys, idx.layout
    if not keys or lay is None:
        return []
    end = idx.end
    nxt_end = None if live else (end, idx.end_pts if idx.end_pts is not None else keys[-1][1])
    bounds = [keys[0]]
    for i in range(1, len(keys)):
        off, pts = keys[i]
        b_off, b_pts = bounds[-1]
        if pts - b_pts >= (target_s - 0.5) * PTS_HZ or off - b_off >= max_bytes:
            bounds.append((off, pts))
            continue
        # следующий ключевой кадр далеко (выборочный поиск, длинный GOP) — кусок закрываем здесь
        nxt = keys[i + 1] if i + 1 < len(keys) else nxt_end
        if nxt is None:
            break  # запись идёт: решится, когда появится следующий
        if nxt[1] - b_pts > (target_s + 1.5) * PTS_HZ or nxt[0] - b_off > max_bytes:
            bounds.append((off, pts))
    out = [(o1, o2 - o1, (p2 - p1) / PTS_HZ) for (o1, p1), (o2, p2) in itertools.pairwise(bounds)]
    if not live and end > bounds[-1][0]:
        off, pts = bounds[-1]
        if idx.end_pts is not None and idx.end_pts > pts:
            dur = (idx.end_pts - pts) / PTS_HZ
        else:
            rate = idx.byte_rate()
            dur = (end - off) / rate if rate else target_s
        out.append((off, end - off, dur))
    return out
