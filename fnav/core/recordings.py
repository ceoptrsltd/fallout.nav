"""Записи регистратора: список, длительность (по PTS MPEG-TS), HLS-плейлист, защита, удаление, безопасные пути."""

from __future__ import annotations

import asyncio
import math
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from fnav import paths
from fnav.camera.tsindex import KeyIndex, chunks, sidecar_path, update_index

_DIR_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,128}\.ts$")
_NAME_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REAR_DIR = "rear"  # записи задней камеры: rear/ДАТА/ИМЯ.ts (fnav-camera-rear)
TS_PACKET = 188
_PROBE_BYTES = 256 * 1024
IN_PROGRESS_MTIME_S = 3.0
_INDEX_CACHE = 32  # индексов ключевых кадров в памяти (LRU)


class RecordingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def safe_rel_path(rel: str) -> PurePosixPath:
    """Проверить относительный путь записи `ДАТА/ИМЯ.ts` (задняя камера — `rear/ДАТА/ИМЯ.ts`).
    Любой выход за каталог → ошибка 400."""
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/") or ":" in rel:
        raise RecordingError(400, "Invalid path")
    p = PurePosixPath(rel)
    parts = p.parts
    if len(parts) == 3 and parts[0] == REAR_DIR:
        parts = parts[1:]
    elif parts and parts[0] == REAR_DIR:  # rear/ИМЯ.ts — не день передней и не путь задней
        raise RecordingError(400, "Invalid path")
    if len(parts) != 2 or not _DIR_RE.match(parts[0]) or not _FILE_RE.match(parts[1]):
        raise RecordingError(400, "Invalid path")
    if any(x in ("..", ".") for x in parts) or parts[1].startswith("."):
        raise RecordingError(400, "Invalid path")
    return p


def camera_of(rel: str) -> str:
    return "rear" if rel.startswith(REAR_DIR + "/") else "front"


def resolve(root: Path, rel: str) -> Path:
    p = safe_rel_path(rel)
    full = root.joinpath(*p.parts)
    root_r = root.resolve()
    try:
        full.resolve().relative_to(root_r)
    except ValueError:
        raise RecordingError(400, "Invalid path") from None
    return full


def lock_path(f: Path) -> Path:
    return f.with_name(f.name + ".lock")


# ── HLS ──


def hls_playlist(parts: list[tuple[int, int, float]], uri: str, *, live: bool) -> str:
    """Плейлист HLS из кусков одного файла (EXT-X-BYTERANGE, версия 4).

    Всегда EVENT: запись только дописывается. Идёт запись — без ENDLIST (плеер перечитывает и видит новое),
    закончена — с ENDLIST (обычный VOD). START=0: и идущую запись смотрим с начала."""
    target = max(1, math.ceil(max((d for _, _, d in parts), default=1.0)))
    out = [
        "#EXTM3U",
        "#EXT-X-VERSION:4",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        "#EXT-X-START:TIME-OFFSET=0",
    ]
    for off, length, dur in parts:
        out += [f"#EXTINF:{max(dur, 0.001):.3f},", f"#EXT-X-BYTERANGE:{length}@{off}", uri]
    if not live:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


# ── длительность по PTS ──


def _pts_list(buf: bytes) -> list[int]:
    """PTS всех PES-заголовков в буфере (выравнивание по синхробайту ищем сами)."""
    out: list[int] = []
    start = 0
    while start < min(len(buf), TS_PACKET * 2):
        if buf[start] == 0x47 and (start + TS_PACKET >= len(buf) or buf[start + TS_PACKET] == 0x47):
            break
        start += 1
    else:
        return out
    for off in range(start, len(buf) - TS_PACKET + 1, TS_PACKET):
        pkt = buf[off:off + TS_PACKET]
        if pkt[0] != 0x47 or not (pkt[1] & 0x40):  # payload_unit_start
            continue
        afc = (pkt[3] >> 4) & 0x3
        i = 4
        if afc in (2, 3):
            i += 1 + pkt[4]
        if afc == 2 or i + 14 > TS_PACKET:
            continue
        if pkt[i:i + 3] != b"\x00\x00\x01":
            continue
        sid = pkt[i + 3]
        if not (0xE0 <= sid <= 0xEF or 0xC0 <= sid <= 0xDF or sid == 0xBD):
            continue
        flags = pkt[i + 7]
        if not (flags & 0x80):
            continue
        b = pkt[i + 9:i + 14]
        pts = (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)
        out.append(pts)
    return out


def ts_duration(path: Path) -> float | None:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(_PROBE_BYTES)
            if size > _PROBE_BYTES:
                f.seek(max(0, size - _PROBE_BYTES))
                tail = f.read(_PROBE_BYTES)
            else:
                tail = head
    except OSError:
        return None
    first = _pts_list(head)
    last = _pts_list(tail)
    if not first or not last:
        return None
    a = min(first[:30])
    b = max(last)
    d = (b - a) % (1 << 33)
    if d <= 0 or d > 90000 * 3600 * 6:
        return None
    return round(d / 90000.0, 2)


# ── сервис ──


class Recordings:
    def __init__(self, root: Path | None = None):
        self._root = root
        self._dur_cache: dict[str, tuple[int, float, float | None]] = {}
        self._idx_cache: OrderedDict[str, KeyIndex] = OrderedDict()
        self._idx_lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root or paths.recordings_dir()

    def _date_dirs(self) -> list[tuple[str, str]]:
        """(префикс пути записи, каталог дня): передняя — ДАТА/, задняя — rear/ДАТА/."""
        out: list[tuple[str, str]] = []
        for base, prefix in ((self.root, ""), (self.root / REAR_DIR, REAR_DIR + "/")):
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for d in entries:
                if d.is_dir() and _DIR_RE.match(d.name) and not (prefix == "" and d.name == REAR_DIR):
                    out.append((prefix + d.name, d.path))
        return out

    def _scan(self, current: str | None) -> list[dict]:
        root = self.root
        items: list[tuple[float, dict]] = []
        if not root.is_dir():
            return []
        now = time.time()
        seen: set[str] = set()
        for dir_rel, dir_path in self._date_dirs():
            dname = dir_rel.rsplit("/", 1)[-1]
            for f in os.scandir(dir_path):
                if not f.is_file() or not _FILE_RE.match(f.name) or f.name.startswith("."):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                rel = f"{dir_rel}/{f.name}"
                seen.add(rel)
                start_dt = _parse_start(f.name)
                in_prog = rel == current or (now - st.st_mtime) < IN_PROGRESS_MTIME_S
                # длительность: кэш по (размер, mtime)
                c = self._dur_cache.get(rel)
                if c and c[0] == st.st_size and c[1] == st.st_mtime:
                    dur = c[2]
                else:
                    dur = ts_duration(Path(f.path))
                    self._dur_cache[rel] = (st.st_size, st.st_mtime, dur)
                date = dname if _DATE_DIR_RE.match(dname) else (
                    start_dt.date().isoformat() if start_dt else dname)
                items.append((
                    (start_dt.timestamp() if start_dt else st.st_mtime),
                    {
                        "path": rel,
                        "camera": camera_of(rel),
                        "date": date,
                        "start": start_dt.isoformat(timespec="seconds") if start_dt else None,
                        "size_bytes": st.st_size,
                        "duration_s": dur,
                        "protected": os.path.exists(f.path + ".lock"),
                        "in_progress": in_prog,
                    },
                ))
        for k in list(self._dur_cache):
            if k not in seen:
                del self._dur_cache[k]
        with self._idx_lock:
            for k in [k for k in self._idx_cache if k not in seen]:
                del self._idx_cache[k]
        items.sort(key=lambda t: t[0], reverse=True)
        return [x for _, x in items]

    async def list(self, current: str | None = None) -> list[dict]:
        return await asyncio.to_thread(self._scan, current)

    def total_bytes(self) -> int:
        total = 0
        if not self.root.is_dir():
            return 0
        for _rel, dir_path in self._date_dirs():
            for f in os.scandir(dir_path):
                if f.name.endswith(".ts"):
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total

    def file(self, rel: str) -> Path:
        p = resolve(self.root, rel)
        if not p.is_file():
            raise RecordingError(404, "Recording not found")
        return p

    def index(self, rel: str) -> KeyIndex:
        """Индекс ключевых кадров (кэш по размеру и mtime; растущий файл — дочитывается)."""
        p = self.file(rel)
        with self._idx_lock:
            try:
                idx = update_index(p, self._idx_cache.get(rel))
            except OSError:
                raise RecordingError(404, "Recording not found") from None
            self._idx_cache[rel] = idx
            self._idx_cache.move_to_end(rel)
            while len(self._idx_cache) > _INDEX_CACHE:
                self._idx_cache.popitem(last=False)
        return idx

    def _hls(self, rel: str, current: str | None) -> str:
        idx = self.index(rel)
        live = rel == current or (time.time() - idx.mtime_ns / 1e9) < IN_PROGRESS_MTIME_S
        # относительно плейлиста …/hls/ДАТА/ИМЯ.ts.m3u8 → …/file/ДАТА/ИМЯ.ts (и через префикс /fnadmin/);
        # задняя: …/hls/rear/ДАТА/ИМЯ.ts.m3u8 → …/file/rear/ДАТА/ИМЯ.ts — на уровень выше
        parts = PurePosixPath(rel).parts
        safe_rel_path(rel)
        uri = "../" * len(parts) + "file/" + "/".join(quote(x) for x in parts)
        return hls_playlist(chunks(idx, live=live), uri, live=live)

    async def hls(self, rel: str, current: str | None = None) -> str:
        return await asyncio.to_thread(self._hls, rel, current)

    async def protect(self, rel: str, protected: bool) -> bool:
        p = self.file(rel)
        lk = lock_path(p)

        def _do() -> None:
            if protected:
                lk.touch(exist_ok=True)
            else:
                try:
                    lk.unlink()
                except FileNotFoundError:
                    pass

        await asyncio.to_thread(_do)
        return protected

    async def delete(self, rel: str, *, current: str | None, force: bool) -> None:
        p = self.file(rel)
        st = p.stat()
        if rel == current or (time.time() - st.st_mtime) < IN_PROGRESS_MTIME_S:
            raise RecordingError(409, "Cannot delete: this file is being recorded")
        lk = lock_path(p)
        if lk.exists() and not force:
            raise RecordingError(409, "Recording is protected. Unprotect it or delete with force=1")

        def _do() -> None:
            p.unlink()
            for extra in (lk, sidecar_path(p)):
                try:
                    extra.unlink()
                except FileNotFoundError:
                    pass
            self._dur_cache.pop(rel, None)
            with self._idx_lock:
                self._idx_cache.pop(rel, None)
            d = p.parent
            today = datetime.now().strftime("%Y-%m-%d")
            if d.name != today:
                try:
                    d.rmdir()  # только если пустой
                except OSError:
                    pass

        await asyncio.to_thread(_do)

    def newest(self) -> str | None:
        items = self._scan(None)
        return items[0]["path"] if items else None


EVENT_MARGIN_S = 2.0  # имя файла — с точностью до секунды; событие на стыке сегментов попадёт в оба


def _span(it: dict, now: float) -> tuple[float, float] | None:
    """Начало (unix, по имени файла — местное время) и длительность записи; без времени (nosync) — None.
    Текущий сегмент — до «сейчас»."""
    try:
        t0 = datetime.fromisoformat(it["start"]).timestamp() if it.get("start") else None
    except ValueError:
        return None
    if t0 is None:
        return None
    dur = it.get("duration_s") or 0.0
    if it.get("in_progress"):
        dur = max(dur, now - t0)
    return t0, dur


def attach_events(items: list[dict], events: Iterable[Any], now: float | None = None) -> list[dict]:
    """Метки событий охраны в записях: offset_s = время события − начало сегмента (по имени файла, местное время).
    Записи без времени (nosync) — без меток. Текущий сегмент — до «сейчас»."""
    now = time.time() if now is None else now
    evs = [e for e in events if e.kind != "test"]
    for it in items:
        marks: list[dict] = []
        span = _span(it, now)
        if span is not None:
            t0, dur = span
            for e in evs:
                off = e.ts - t0
                if -EVENT_MARGIN_S <= off <= dur + EVENT_MARGIN_S:
                    marks.append({"id": e.id, "kind": e.kind, "title": e.title, "ts": e.ts,
                                  "offset_s": round(min(max(off, 0.0), dur), 1),
                                  "snapshot": bool((e.data or {}).get("snapshot"))})
        it["events"] = sorted(marks, key=lambda m: m["offset_s"])
    return items


def attach_marks(items: list[dict], between: Callable[[float, float, str | None], list[dict]],
                 now: float | None = None) -> list[dict]:
    """Отметки движения без тревоги (fnav/core/marks.py) — только своей камеры: {kind, ts, offset_s, dur_s}."""
    now = time.time() if now is None else now
    for it in items:
        out: list[dict] = []
        span = _span(it, now)
        if span is not None:
            t0, dur = span
            for m in between(t0 - EVENT_MARGIN_S, t0 + dur + EVENT_MARGIN_S, camera_of(it.get("path") or "")):
                out.append({"kind": m["kind"], "ts": m["ts"], "offset_s": round(min(max(m["ts"] - t0, 0.0), dur), 1),
                            "dur_s": m["dur_s"]})
        it["marks"] = out
    return items


def recording_at(items: list[dict], ts: float, camera: str, now: float | None = None) -> dict | None:
    """Запись камеры, в которую попадает момент ts: {path, offset_s} или None."""
    now = time.time() if now is None else now
    for it in items:
        span = _span(it, now)
        if span is None or camera_of(it.get("path") or "") != camera:
            continue
        t0, dur = span
        if t0 - EVENT_MARGIN_S <= ts <= t0 + dur + EVENT_MARGIN_S:
            return {"path": it["path"], "offset_s": round(min(max(ts - t0, 0.0), dur), 1)}
    return None


def _parse_start(name: str) -> datetime | None:
    m = _NAME_DT_RE.match(name)
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


# ── отдача с Range ──


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """`bytes=a-b` → (start, end) включительно. None — заголовка нет/непонятен (отдаём целиком).
    Невыполнимый диапазон → RecordingError(416)."""
    if not header:
        return None
    h = header.strip()
    if not h.lower().startswith("bytes="):
        return None
    spec = h[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    a, b = a.strip(), b.strip()
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                raise RecordingError(416, "Invalid range")
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    if start >= size or start < 0 or end < start:
        raise RecordingError(416, "Invalid range")
    return start, min(end, size - 1)


async def iter_file(path: Path, start: int, length: int, chunk: int = 256 * 1024) -> AsyncIterator[bytes]:
    f = await asyncio.to_thread(open, path, "rb")
    try:
        await asyncio.to_thread(f.seek, start)
        left = length
        while left > 0:
            data = await asyncio.to_thread(f.read, min(chunk, left))
            if not data:
                break
            left -= len(data)
            yield data
    finally:
        await asyncio.to_thread(f.close)
