"""Отметки движения с камер на охране (fnav-watch, docs/contracts/vision.md «Отметки»): без тревоги — не журнал
охраны, не push, не аварийка, не защита записи; серые метки на шкале записи и свёрнутый список на странице охраны.

data_dir/vision_marks.jsonl — по строке JSON на отметку {ts, kind, camera, dur_s} (ts — начало куска движения, unix;
kind — "motion" | "vehicle" | "person"). В памяти — последние MARKS_MAX и не старше MARKS_MAX_AGE_S, по времени.
Карта памяти: новые отметки дописываются в конец файла (не чаще раза в SAVE_EVERY_S и при остановке), целиком файл
переписывается, только когда в нём набралось COMPACT_AT устаревших строк или отметки очистили. Оборванная
последняя строка (питание пропало при записи) при чтении пропускается.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from fnav.core.config import atomic_write_text
from fnav.core.util import cancel_tasks

log = logging.getLogger("fnav.core.marks")

MARK_KINDS = ("motion", "vehicle", "person")
CAMERAS = ("front", "rear")
MARKS_MAX = 5000
MARKS_MAX_AGE_S = 30 * 86400
SAVE_EVERY_S = 30.0
COMPACT_AT = 1000  # столько строк файла уже не в памяти (старые) — переписать файл
MAX_SKEW_S = 3600.0  # начало куска — не раньше столько до события (кривые данные — время события)
MAX_DUR_S = 600.0
LEGACY_NAME = "vision_marks.json"  # первая версия: JSON-список целиком


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clean(m: object) -> dict | None:
    if not (isinstance(m, dict) and _num(m.get("ts")) is not None and m.get("kind") in MARK_KINDS
            and m.get("camera") in CAMERAS):
        return None
    return {"ts": float(m["ts"]), "kind": m["kind"], "camera": m["camera"],
            "dur_s": max(0.0, min(MAX_DUR_S, _num(m.get("dur_s")) or 0.0))}


def _line(m: dict) -> str:
    return json.dumps(m, separators=(",", ":")) + "\n"


class MarkLog:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.items: list[dict] = []  # по ts
        self._ts: list[float] = []
        self._pending: list[dict] = []  # ещё не дописаны в файл
        self._stale = 0  # строк в файле, которых уже нет в памяти
        self._rewrite = False  # файл переписать целиком (очистили, много старых строк, перенос со старого формата)
        self._legacy: Path | None = None
        self._task: asyncio.Task | None = None

    # ── жизненный цикл ──
    async def start(self, background: bool = True) -> None:
        await asyncio.to_thread(self._load)
        if background:
            self._task = asyncio.create_task(self._saver(), name="marks-save")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        self._task = None
        await self.flush()

    def _load(self) -> None:
        raw: list = []
        lines = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    lines += 1
                    try:
                        raw.append(json.loads(ln))
                    except ValueError:
                        self._rewrite = True  # оборванная строка: дописывать после неё нельзя — склеится
                    if not ln.endswith("\n"):
                        self._rewrite = True
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("marks: cannot read %s: %s", self.path, e)
        legacy = self.path.with_name(LEGACY_NAME)
        if legacy.is_file():
            with contextlib.suppress(OSError, ValueError):
                old = json.loads(legacy.read_text(encoding="utf-8"))
                raw += old if isinstance(old, list) else []
            self._legacy, self._rewrite = legacy, True
        items = [c for c in map(_clean, raw) if c is not None]
        items.sort(key=lambda m: m["ts"])
        self.items = items
        self._ts = [m["ts"] for m in items]
        self._stale = max(0, lines - len(items))  # битые строки тоже — до переписывания
        self._prune()

    async def _saver(self) -> None:
        while True:
            await asyncio.sleep(SAVE_EVERY_S)
            try:
                await self.flush()
            except Exception:
                log.exception("marks: save")

    async def flush(self) -> None:
        if self._rewrite or self._stale >= COMPACT_AT:
            pending, self._pending, self._rewrite = self._pending, [], False
            stale, self._stale = self._stale, 0
            try:
                await asyncio.to_thread(atomic_write_text, self.path, "".join(map(_line, self.items)))
            except OSError as e:
                self._pending, self._rewrite, self._stale = pending + self._pending, True, stale
                log.warning("marks: cannot write %s: %s", self.path, e)
                return
            if self._legacy is not None:
                with contextlib.suppress(OSError):
                    self._legacy.unlink()
                self._legacy = None
            return
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        try:
            await asyncio.to_thread(self._append, pending)
        except OSError as e:
            self._pending = pending + self._pending
            log.warning("marks: cannot write %s: %s", self.path, e)

    def _append(self, ms: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write("".join(map(_line, ms)))
            f.flush()
            os.fsync(f.fileno())

    # ── данные ──
    def add(self, data: dict, ts: float | None = None) -> dict | None:
        """Событие "mark" от fnav-watch (data: what, start, dur_s, camera; ts — когда пришло) → запись или None."""
        now = self.clock() if ts is None else ts
        kind = data.get("what")
        camera = data.get("camera") or "front"
        if kind not in MARK_KINDS or camera not in CAMERAS:
            return None
        start = _num(data.get("start"))
        if start is None or not now - MAX_SKEW_S <= start <= now + 60:
            start = now
        m = {"ts": round(start, 3), "kind": kind, "camera": camera,
             "dur_s": round(max(0.0, min(MAX_DUR_S, _num(data.get("dur_s")) or 0.0)), 1)}
        i = bisect.bisect_right(self._ts, m["ts"])
        self.items.insert(i, m)
        self._ts.insert(i, m["ts"])
        self._pending.append(m)
        self._prune()
        return m

    def _prune(self) -> None:
        cut = self.clock() - MARKS_MAX_AGE_S
        n = max(bisect.bisect_left(self._ts, cut), len(self.items) - MARKS_MAX)
        if n > 0:
            gone = {id(m) for m in self.items[:n]}
            pending = [m for m in self._pending if id(m) not in gone]  # не дописанные — и не нужны
            self._stale += n - (len(self._pending) - len(pending))  # в файле — только дописанные
            self._pending = pending
            del self.items[:n]
            del self._ts[:n]

    def between(self, t0: float, t1: float, camera: str | None = None) -> list[dict]:
        lo, hi = bisect.bisect_left(self._ts, t0), bisect.bisect_right(self._ts, t1)
        return [m for m in self.items[lo:hi] if camera is None or m["camera"] == camera]

    def latest(self, limit: int = 50, since: float | None = None) -> list[dict]:
        """Новые первыми."""
        lo = bisect.bisect_left(self._ts, since) if since is not None else 0
        return list(reversed(self.items[max(lo, len(self.items) - limit):]))

    def counts(self, since: float) -> dict[str, int]:
        out = dict.fromkeys(MARK_KINDS, 0)
        for m in self.items[bisect.bisect_left(self._ts, since):]:
            out[m["kind"]] += 1
        return out

    def clear(self) -> None:
        self.items.clear()
        self._ts.clear()
        self._pending.clear()
        self._stale = 0
        self._rewrite = True
