"""Хранилище записей: очистка по свободному месту и лимиту объёма, статистика, восстановление после сбоя."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .tsmux import repair_ts

log = logging.getLogger("fnav.camera.storage")

GB = 1024**3

DiskFree = Callable[[Path], int]


def disk_free(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(p).free


def lock_path(ts: Path) -> Path:
    return ts.with_name(ts.name + ".lock")


def is_protected(ts: Path) -> bool:
    return lock_path(ts).exists()


def list_recordings(root: Path) -> list[Path]:
    """Все .ts под root, от старых к новым (по mtime, затем по имени)."""
    if not root.is_dir():
        return []
    items = []
    for p in root.rglob("*.ts"):
        try:
            items.append((p.stat().st_mtime, p.as_posix(), p))
        except OSError:
            continue
    items.sort()
    return [p for _, _, p in items]


def stats(root: Path) -> tuple[int, int]:
    """(число файлов, байт)."""
    n = total = 0
    for p in list_recordings(root):
        try:
            total += p.stat().st_size
            n += 1
        except OSError:
            pass
    return n, total


def segment_reserve_bytes(bitrate_kbps: int, segment_minutes: float) -> int:
    return int(bitrate_kbps * 1000 / 8 * segment_minutes * 60)


@dataclass
class CleanupResult:
    deleted: list[Path] = field(default_factory=list)
    free_bytes: int = 0
    target_bytes: int = 0
    ok: bool = True  # свободного места ≥ target
    total_bytes: int = 0  # все записи после очистки (с защищёнными и текущей)
    over_limit: bool = False  # лимит объёма не выдержан: остались только защищённые

    @property
    def free_gb(self) -> float:
        return self.free_bytes / GB


def _remove_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass
        except OSError:
            pass


def cleanup(
    root: Path,
    target_free_bytes: int,
    current: Path | None = None,
    free_fn: DiskFree = disk_free,
    max_total_bytes: int | None = None,
) -> CleanupResult:
    """Удалять самые старые незащищённые .ts (кроме текущего), пока свободно < target или все записи
    вместе (с защищёнными) занимают > max_total_bytes. Защищённые не удаляются никогда."""
    res = CleanupResult(target_bytes=target_free_bytes)
    files: list[tuple[Path, int]] = []
    for p in list_recordings(root):
        try:
            files.append((p, p.stat().st_size))
        except OSError:
            continue
    total = sum(size for _, size in files)
    free = free_fn(root)

    def need() -> bool:
        return free < target_free_bytes or (max_total_bytes is not None and total > max_total_bytes)

    if need():
        cur = current.resolve() if current else None
        for p, size in files:
            if not need():
                break
            if is_protected(p):
                continue
            try:
                if cur is not None and p.resolve() == cur:
                    continue
                p.unlink()
                res.deleted.append(p)
                total -= size
                log.info("cleanup: deleted %s (%.1f MB)", p.relative_to(root).as_posix(), size / 1e6)
            except OSError as e:
                log.warning("cleanup: cannot delete %s: %s", p, e)
                continue
            free = free_fn(root)
        if res.deleted:
            _remove_empty_dirs(root)
    res.free_bytes = free
    res.ok = free >= target_free_bytes
    res.total_bytes = total
    res.over_limit = max_total_bytes is not None and total > max_total_bytes
    return res


def repair_latest(root: Path, count: int = 3) -> list[tuple[Path, int, int]]:
    """Восстановить последние файлы (обрыв питания). Пустые/без кадров — удалить."""
    out = []
    for p in list_recordings(root)[-count:]:
        try:
            before, after = repair_ts(p)
        except OSError as e:
            log.error("repair %s failed: %s", p, e)
            continue
        if after == 0 and not is_protected(p):
            try:
                p.unlink()
                log.warning("repair: %s has no frames, removed", p)
            except OSError:
                pass
        elif before != after:
            log.warning("repair: %s truncated %d -> %d bytes", p, before, after)
        out.append((p, before, after))
    return out
