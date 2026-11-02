"""Хранилище записей: очистка по свободному месту и лимиту объёма, статистика, восстановление после сбоя."""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .tsindex import sidecar_path
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


def remove_sidecar(ts: Path) -> None:
    """Индекс ключевых кадров удаляется вместе с записью."""
    try:
        sidecar_path(ts).unlink()
    except OSError:
        pass


def list_recordings(root: Path, exclude: tuple[str, ...] = ()) -> list[Path]:
    """Все .ts под root, от старых к новым (по mtime, затем по имени). exclude — каталоги верхнего уровня, которые
    не смотреть (записи задней камеры в rear/ — их пишет и чинит другой процесс)."""
    if not root.is_dir():
        return []
    items = []
    for p in root.rglob("*.ts"):
        if exclude and len(p.relative_to(root).parts) > 1 and p.relative_to(root).parts[0] in exclude:
            continue
        try:
            items.append((p.stat().st_mtime, p.as_posix(), p))
        except OSError:
            continue
    items.sort()
    return [p for _, _, p in items]


def stats(root: Path, exclude: tuple[str, ...] = ()) -> tuple[int, int]:
    """(число файлов, байт)."""
    n = total = 0
    for p in list_recordings(root, exclude):
        try:
            total += p.stat().st_size
            n += 1
        except OSError:
            pass
    return n, total


def segment_reserve_bytes(bitrate_kbps: float, segment_minutes: float) -> int:
    return int(bitrate_kbps * 1000 / 8 * segment_minutes * 60)


def loop_minimum(bytes_per_hour: int, max_total_bytes: int) -> int:
    """Меньше этого лимит объёма петлю (незащищённые записи) не сокращает: час записи, но не больше половины лимита
    (иначе лимит не работал бы и без защищённых). Глубже чистит только нехватка свободного места."""
    return max(0, min(int(bytes_per_hour), max_total_bytes // 2))


@dataclass
class CleanupResult:
    deleted: list[Path] = field(default_factory=list)
    free_bytes: int = 0
    target_bytes: int = 0
    ok: bool = True  # свободного места ≥ target
    total_bytes: int = 0  # все записи после очистки (с защищёнными и текущей)
    over_limit: bool = False  # лимит объёма не выдержан: остались защищённые и минимум петли
    protected_bytes: int = 0  # защищённые записи
    loop_bytes: int = 0  # петля после очистки: незащищённые записи (с текущей)

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
    skip_recent_s: float = 0.0,
    min_loop_bytes: int = 0,
) -> CleanupResult:
    """Удалять самые старые незащищённые .ts (кроме текущего), пока свободно < target или все записи
    вместе (с защищёнными) занимают > max_total_bytes. Защищённые не удаляются никогда.
    Ради лимита объёма петля (незащищённые, с текущей) не сокращается меньше min_loop_bytes — иначе защищённые
    сверх лимита оставили бы от петли минуты; ниже — только ради свободного места (диск правда кончается).
    skip_recent_s — не трогать файлы, изменённые недавно (текущий сегмент другой камеры: его пишет другой процесс)."""
    res = CleanupResult(target_bytes=target_free_bytes)
    files: list[tuple[Path, int, bool]] = []
    for p in list_recordings(root):
        try:
            files.append((p, p.stat().st_size, is_protected(p)))
        except OSError:
            continue
    total = sum(size for _, size, _ in files)
    loop = sum(size for _, size, prot in files if not prot)
    free = free_fn(root)

    def over() -> bool:
        return max_total_bytes is not None and total > max_total_bytes

    if free < target_free_bytes or over():
        cur = current.resolve() if current else None
        for p, size, prot in files:
            space = free < target_free_bytes
            if not space and not over():
                break
            if prot:
                continue
            try:
                if cur is not None and p.resolve() == cur:
                    continue
                if skip_recent_s and time.time() - p.stat().st_mtime < skip_recent_s:
                    continue
                if not space and loop - size < min_loop_bytes:
                    break  # только лимит: петлю меньше минимума не режем (и дыр в ней не делаем)
                if is_protected(p):  # защитили только что
                    loop -= size
                    continue
                p.unlink()
                remove_sidecar(p)
                res.deleted.append(p)
                total -= size
                loop -= size
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
    res.over_limit = over()
    res.protected_bytes = total - loop
    res.loop_bytes = loop
    return res


def repair_latest(root: Path, count: int = 3, exclude: tuple[str, ...] = ()) -> list[tuple[Path, int, int]]:
    """Восстановить последние файлы (обрыв питания). Пустые/без кадров — удалить."""
    out = []
    for p in list_recordings(root, exclude)[-count:]:
        try:
            before, after = repair_ts(p)
        except OSError as e:
            log.error("repair %s failed: %s", p, e)
            continue
        if after == 0 and not is_protected(p):
            try:
                p.unlink()
                remove_sidecar(p)
                log.warning("repair: %s has no frames, removed", p)
            except OSError:
                pass
        elif before != after:
            log.warning("repair: %s truncated %d -> %d bytes", p, before, after)
        out.append((p, before, after))
    return out
