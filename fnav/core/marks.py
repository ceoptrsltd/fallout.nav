"""Отметки движения с камер на охране (fnav-watch, docs/contracts/vision.md «Отметки»): без тревоги — не журнал
охраны, не push, не аварийка, не защита записи; серые метки на шкале записи и свёрнутый список на странице охраны.

data_dir/vision_marks.json — список {ts, kind, camera, dur_s} по времени (ts — начало куска движения, unix; kind —
"motion" | "vehicle" | "person"): последние MARKS_MAX и не старше MARKS_MAX_AGE_S. На диск — не чаще раза в
SAVE_EVERY_S (карта памяти) и при остановке.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
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
MAX_SKEW_S = 3600.0  # начало куска — не раньше столько до события (кривые данные — время события)
MAX_DUR_S = 600.0


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class MarkLog:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.items: list[dict] = []  # по ts
        self._ts: list[float] = []
        self._dirty = False
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
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = []
        except (OSError, ValueError) as e:
            log.warning("marks: cannot read %s: %s", self.path, e)
            raw = []
        items = []
        for m in raw if isinstance(raw, list) else []:
            if (isinstance(m, dict) and _num(m.get("ts")) is not None and m.get("kind") in MARK_KINDS
                    and m.get("camera") in CAMERAS):
                items.append({"ts": float(m["ts"]), "kind": m["kind"], "camera": m["camera"],
                              "dur_s": max(0.0, min(MAX_DUR_S, _num(m.get("dur_s")) or 0.0))})
        items.sort(key=lambda m: m["ts"])
        self.items = items
        self._ts = [m["ts"] for m in items]
        self._prune()

    async def _saver(self) -> None:
        while True:
            await asyncio.sleep(SAVE_EVERY_S)
            try:
                await self.flush()
            except Exception:
                log.exception("marks: save")

    async def flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        data = json.dumps(self.items, separators=(",", ":"))
        try:
            await asyncio.to_thread(atomic_write_text, self.path, data)
        except OSError as e:
            self._dirty = True
            log.warning("marks: cannot write %s: %s", self.path, e)

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
        self._prune()
        self._dirty = True
        return m

    def _prune(self) -> None:
        cut = self.clock() - MARKS_MAX_AGE_S
        n = max(bisect.bisect_left(self._ts, cut), len(self.items) - MARKS_MAX)
        if n > 0:
            del self.items[:n]
            del self._ts[:n]
            self._dirty = True

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
        self._dirty = True
