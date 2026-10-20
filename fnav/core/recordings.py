"""Записи регистратора: список, длительность (по PTS MPEG-TS), защита, удаление, безопасные пути."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path, PurePosixPath

from fnav import paths

_DIR_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,128}\.ts$")
_NAME_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TS_PACKET = 188
_PROBE_BYTES = 256 * 1024
IN_PROGRESS_MTIME_S = 3.0


class RecordingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def safe_rel_path(rel: str) -> PurePosixPath:
    """Проверить относительный путь записи `ДАТА/ИМЯ.ts`. Любой выход за каталог → ошибка 400."""
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/") or ":" in rel:
        raise RecordingError(400, "Invalid path")
    p = PurePosixPath(rel)
    parts = p.parts
    if len(parts) != 2 or not _DIR_RE.match(parts[0]) or not _FILE_RE.match(parts[1]):
        raise RecordingError(400, "Invalid path")
    if any(x in ("..", ".") for x in parts) or parts[1].startswith("."):
        raise RecordingError(400, "Invalid path")
    return p


def resolve(root: Path, rel: str) -> Path:
    p = safe_rel_path(rel)
    full = (root / p.parts[0] / p.parts[1])
    root_r = root.resolve()
    try:
        full.resolve().relative_to(root_r)
    except ValueError:
        raise RecordingError(400, "Invalid path") from None
    return full


def lock_path(f: Path) -> Path:
    return f.with_name(f.name + ".lock")


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

    @property
    def root(self) -> Path:
        return self._root or paths.recordings_dir()

    def _scan(self, current: str | None) -> list[dict]:
        import time

        root = self.root
        items: list[tuple[float, dict]] = []
        if not root.is_dir():
            return []
        now = time.time()
        seen: set[str] = set()
        for d in os.scandir(root):
            if not d.is_dir() or not _DIR_RE.match(d.name):
                continue
            for f in os.scandir(d.path):
                if not f.is_file() or not _FILE_RE.match(f.name) or f.name.startswith("."):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                rel = f"{d.name}/{f.name}"
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
                date = d.name if _DATE_DIR_RE.match(d.name) else (
                    start_dt.date().isoformat() if start_dt else d.name)
                items.append((
                    (start_dt.timestamp() if start_dt else st.st_mtime),
                    {
                        "path": rel,
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
        items.sort(key=lambda t: t[0], reverse=True)
        return [x for _, x in items]

    async def list(self, current: str | None = None) -> list[dict]:
        return await asyncio.to_thread(self._scan, current)

    def total_bytes(self) -> int:
        total = 0
        root = self.root
        if not root.is_dir():
            return 0
        for d in os.scandir(root):
            if d.is_dir():
                for f in os.scandir(d.path):
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
        import time

        st = p.stat()
        if rel == current or (time.time() - st.st_mtime) < IN_PROGRESS_MTIME_S:
            raise RecordingError(409, "Cannot delete: this file is being recorded")
        lk = lock_path(p)
        if lk.exists() and not force:
            raise RecordingError(409, "Recording is protected. Unprotect it or delete with force=1")

        def _do() -> None:
            p.unlink()
            try:
                lk.unlink()
            except FileNotFoundError:
                pass
            self._dur_cache.pop(rel, None)
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
