"""Разрешённая скорость на дороге под колёсами: офлайн-база дорог OpenStreetMap (data_dir/speedlimits.sqlite,
собирает tools/speedlimits.sh из выгрузки Geofabrik; формат — tools/speedlimits/build_db.py).

Раз в секунду: ячейки сетки вокруг позиции GPS → дороги рядом → ближайший отрезок с подходящим направлением
(курс GPS; на односторонних — только по ходу движения). Держимся за текущую дорогу, пока другая не станет
заметно ближе: параллельный дублёр или развязка не дёргают знак. Ограничение — по направлению движения
(maxspeed:forward/backward). База обновилась (mtime) — открываем заново, без перезапуска.
Данные © OpenStreetMap contributors (ODbL).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import math
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from fnav.models import GpsFix, SpeedLimit

log = logging.getLogger("fnav.core.speedlimit")

MAX_DIST_M = 25.0  # дальше — «не на дороге из базы»
KEEP_MARGIN_M = 8.0  # другая дорога должна быть ближе на столько, чтобы перескочить
COURSE_MIN_MPS = 2.0  # медленнее — курс GPS ненадёжен, направление не учитываем
HOLD_S = 10.0  # фикс пропал — показываем последнее ещё столько
TICK_S = 1.0
SRC = {1: "osm", 2: "zone", 3: "default"}


def _unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def decode_pts(blob: bytes, q: float = 1e5) -> list[tuple[float, float]]:
    """pts из базы → [(lat, lon), …]."""
    out: list[tuple[float, float]] = []
    vals: list[int] = []
    n = shift = 0
    for b in blob:
        n |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
            continue
        vals.append(_unzigzag(n))
        n = shift = 0
    lat = lon = 0
    for i in range(0, len(vals) - 1, 2):
        lat += vals[i]
        lon += vals[i + 1]
        out.append((lat / q, lon / q))
    return out


def angle_diff(a: float, b: float) -> float:
    """0…180°."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


class Way:
    __slots__ = ("bwd", "cls", "fwd", "id", "oneway", "pts", "src")

    def __init__(self, row: tuple) -> None:
        self.id, self.fwd, self.bwd, self.cls, self.oneway, self.src, blob = row
        self.pts = decode_pts(blob)


def match(ways: list[Way], lat: float, lon: float, course: float | None) -> list[tuple[float, Way, bool]]:
    """Кандидаты (оценка, дорога, едем по направлению рисования) по возрастанию оценки; оценка — метры."""
    kx = math.cos(math.radians(lat)) * 111_320.0
    ky = 110_540.0
    res: list[tuple[float, Way, bool]] = []
    for w in ways:
        best: tuple[float, bool] | None = None
        pts = [((lo - lon) * kx, (la - lat) * ky) for la, lo in w.pts]
        for (x1, y1), (x2, y2) in itertools.pairwise(pts):
            dx, dy = x2 - x1, y2 - y1
            ll = dx * dx + dy * dy
            t = 0.0 if ll == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / ll))
            dist = math.hypot(x1 + t * dx, y1 + t * dy)
            if dist > MAX_DIST_M:
                continue
            forward = True
            score = dist
            if course is not None and ll > 0:
                seg = math.degrees(math.atan2(dx, dy)) % 360.0
                d = angle_diff(course, seg)
                forward = d <= 90.0
                if w.oneway == 1 and not forward or w.oneway == -1 and forward:
                    score += 40.0  # против одностороннего — почти наверняка другая дорога
                score += max(0.0, min(d, 180.0 - d) - 25.0) * 0.4  # поперёк дороги — не эта дорога
            if best is None or score < best[0]:
                best = (score, forward)
        if best is not None:
            res.append((best[0], w, best[1]))
    res.sort(key=lambda r: r[0])
    return res


class SpeedLimitDb:
    """Чтение базы (в потоке: sqlite3 блокирующий)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: sqlite3.Connection | None = None
        self.mtime: float | None = None
        self.cell_deg = 0.005
        self.meta: dict[str, str] = {}
        self._cache: OrderedDict[int, Way] = OrderedDict()

    def ensure_open(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.close()
            return False
        if self.db is not None and mtime == self.mtime:
            return True
        self.close()
        try:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self.meta = dict(db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.Error as e:
            log.warning("speed limits: cannot open %s: %s", self.path, e)
            return False
        self.db, self.mtime = db, mtime
        self.cell_deg = float(self.meta.get("cell_deg", "0.005"))
        log.info("speed limits: %s (%s, built %s, %s roads)", self.path.name, self.meta.get("region"),
                 self.meta.get("built"), self.meta.get("ways"))
        return True

    def close(self) -> None:
        if self.db is not None:
            with contextlib.suppress(sqlite3.Error):
                self.db.close()
        self.db, self.mtime = None, None
        self._cache.clear()

    def ways_near(self, lat: float, lon: float) -> list[Way]:
        if self.db is None:
            return []
        cy, cx = math.floor(lat / self.cell_deg), math.floor(lon / self.cell_deg)
        keys = [(cy + dy + 20000) * 100000 + (cx + dx + 40000) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
        ids = {r[0] for r in self.db.execute(
            f"SELECT DISTINCT w FROM cell WHERE c IN ({','.join('?' * len(keys))})", keys)}
        missing = [i for i in ids if i not in self._cache]
        for k in range(0, len(missing), 500):
            chunk = missing[k:k + 500]
            for row in self.db.execute(
                    f"SELECT id, fwd, bwd, cls, oneway, src, pts FROM way WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk):
                self._cache[row[0]] = Way(row)
        out = []
        for i in ids:
            w = self._cache.get(i)
            if w is not None:
                self._cache.move_to_end(i)
                out.append(w)
        while len(self._cache) > 4000:
            self._cache.popitem(last=False)
        return out


class SpeedLimitService:
    def __init__(self, path: Path, get_gps: Callable[[], GpsFix], clock: Callable[[], float] = time.monotonic) -> None:
        self.db = SpeedLimitDb(path)
        self.get_gps = get_gps
        self.clock = clock
        self.current: SpeedLimit | None = None
        self._way: int | None = None
        self._seen_at = 0.0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="speed-limit")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.db.close()

    async def _loop(self) -> None:
        reopen_at = 0.0
        while True:
            try:
                now = self.clock()
                if now >= reopen_at:  # новая база (tools/speedlimits.sh) — подхватить
                    reopen_at = now + 60.0
                    await asyncio.to_thread(self.db.ensure_open)
                await asyncio.to_thread(self.update)
            except Exception:
                log.exception("speed limits")
            await asyncio.sleep(TICK_S)

    def status(self) -> SpeedLimit | None:
        return self.current

    def update(self, gps: GpsFix | None = None) -> SpeedLimit | None:
        g = gps or self.get_gps()
        now = self.clock()
        if g.fix == "none" or g.lat is None or g.lon is None or self.db.db is None:
            if now - self._seen_at > HOLD_S:
                self.current, self._way = None, None
            return self.current
        moving = (g.speed_mps or 0.0) >= COURSE_MIN_MPS
        course = g.course_deg if moving and g.course_deg is not None else None
        cands = match(self.db.ways_near(g.lat, g.lon), g.lat, g.lon, course)
        self._seen_at = now
        if not cands:
            self.current, self._way = None, None
            return None
        pick = cands[0]
        if self._way is not None and self._way != pick[1].id:
            cur = next((c for c in cands if c[1].id == self._way), None)
            if cur is not None and cur[0] <= pick[0] + KEEP_MARGIN_M:
                pick = cur
        _score, way, forward = pick
        kmh = way.fwd if forward else way.bwd  # неизвестно в эту сторону — не гадаем
        self._way = way.id
        self.current = SpeedLimit(kmh=kmh, src=SRC.get(way.src, "osm"), way=way.id) if kmh else None
        return self.current
