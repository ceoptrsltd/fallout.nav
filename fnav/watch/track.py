"""Сопровождение людей между кадрами детектора и правила событий.

approach: трек подтверждён (≥ 3 попаданий за окно ~3 с), нижний край рамки (ноги / край капота) дошёл до ближней
линии, рамка выросла ≥ 1,25× (или время до контакта по росту < 4 с), человек не идёт вбок — и так
approach_hold_s. Прохожие (высота постоянна, быстро вбок) не проходят. Повтор тем же треком — только если он ушёл
от линии и вернулся, или через REARM_S.

face: крупное лицо около центра — ≥ 2/3 попыток детектора за face_hold_s (4 из 6 при 2 Гц).

Сопоставление — по расстоянию центров в высотах рамки и отношению высот: при 2 Гц рамки соседних детекций часто
не пересекаются, IoU не годится.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .detect import Box, Det, Face

ASPECT = 4 / 3  # ширина/высота кадра: расстояния по x в тех же единицах, что по y
HISTORY_S = 12.0
LOST_S = 2.5  # столько без детекций — трек закрыт (минимум; растёт при редком детекторе)
GROWTH = 1.25
TTC_S = 4.0
SIDEWAYS = 0.6  # скорость вбок, высот рамки в секунду: больше — идёт мимо
LINE_HYST = 0.03  # ушёл от линии: нижний край выше линии на столько
REARM_AWAY_S = 3.0  # ушёл от линии на столько — может сработать снова
REARM_S = 120.0


@dataclass
class Track:
    id: int
    hist: deque = field(default_factory=deque)  # (t, box, score)
    fired_t: float | None = None
    hold_since: float | None = None
    away_since: float | None = None
    grew: bool = False
    growth: float = 1.0
    sideways: float = 0.0

    @property
    def box(self) -> Box:
        return self.hist[-1][1]

    @property
    def last_t(self) -> float:
        return self.hist[-1][0]

    @property
    def h(self) -> float:
        b = self.box
        return b[3] - b[1]

    def hits(self, since: float) -> int:
        return sum(1 for t, _, _ in self.hist if t >= since)


def _centre(b: Box) -> tuple[float, float]:
    return (b[0] + b[2]) / 2 * ASPECT, (b[1] + b[3]) / 2


class Tracker:
    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self._next = 1
        self.interval = 0.5  # средний интервал детектора людей (для окон правил)

    def reset(self) -> None:
        self.tracks.clear()

    def confirmed(self, t: float) -> list[Track]:
        lost = max(LOST_S, 3 * self.interval)
        return [tr for tr in self.tracks if len(tr.hist) >= 2 and t - tr.last_t <= lost]

    def update(self, dets: list[Det], t: float, dt: float | None = None) -> None:
        if dt is not None and dt > 0:
            self.interval = 0.7 * self.interval + 0.3 * min(dt, 5.0)
        lost = max(LOST_S, 3 * self.interval)
        self.tracks = [tr for tr in self.tracks if t - tr.last_t <= lost]
        pairs = []
        for i, d in enumerate(dets):
            cx, cy = _centre(d.box)
            for j, tr in enumerate(self.tracks):
                tx, ty = _centre(tr.box)
                h = max(tr.h, 0.03)
                gap = max(0.5, t - tr.last_t)
                dist = math.hypot(cx - tx, cy - ty) / h
                ratio = max(d.h, 1e-3) / h
                if dist > 1.2 * gap ** 0.5 + 0.3 or not 0.55 < ratio < 1.8:
                    continue
                pairs.append((dist + 2 * abs(math.log(ratio)), i, j))
        used_d, used_t = set(), set()
        for _, i, j in sorted(pairs):
            if i in used_d or j in used_t:
                continue
            used_d.add(i)
            used_t.add(j)
            self.tracks[j].hist.append((t, dets[i].box, dets[i].score))
        for i, d in enumerate(dets):
            if i not in used_d:
                tr = Track(self._next)
                self._next += 1
                tr.hist.append((t, d.box, d.score))
                self.tracks.append(tr)
        for tr in self.tracks:
            while tr.hist and t - tr.hist[0][0] > HISTORY_S:
                tr.hist.popleft()

    # ── правило «подходит» ──
    def approach(self, t: float, near_line: float, hold_s: float) -> list[tuple[Track, dict]]:
        """Треки, для которых только что выполнилось правило (каждый — один раз до перевзвода)."""
        fired = []
        window = max(3.0, 3.5 * self.interval)
        for tr in self.tracks:
            if tr.last_t != t:
                continue  # правило — только по свежей детекции
            hs = [b[3] - b[1] for _, b, _ in tr.hist]
            tr.growth = hs[-1] / max(1e-3, min(hs))
            if tr.growth >= GROWTH or _ttc(tr) < TTC_S:
                tr.grew = True
            tr.sideways = _sideways(tr)
            bottom = tr.box[3]
            if bottom < near_line - LINE_HYST:  # ушёл от линии
                tr.hold_since = None
                tr.away_since = tr.away_since or t
                if tr.fired_t is not None and t - tr.away_since >= REARM_AWAY_S:
                    tr.fired_t = None
                continue
            if bottom < near_line:
                continue  # в полосе гистерезиса: удержание не начинаем и не сбрасываем
            tr.away_since = None
            if tr.fired_t is not None and t - tr.fired_t >= REARM_S:
                tr.fired_t = None
                tr.hold_since = None
            if tr.fired_t is not None:
                continue
            if tr.hits(t - window) < 3 or not tr.grew or tr.sideways > SIDEWAYS:
                tr.hold_since = None
                continue
            if tr.hold_since is None:
                tr.hold_since = t
            if t - tr.hold_since >= hold_s:
                tr.fired_t = t
                fired.append((tr, {
                    "track": tr.id, "height": round(tr.h, 3), "bottom": round(bottom, 3),
                    "growth": round(tr.growth, 2), "held_s": round(t - tr.hold_since, 1),
                    "score": round(max(s for _, _, s in tr.hist), 2), "hits": len(tr.hist),
                }))
        return fired


def _ttc(tr: Track) -> float:
    """Время до контакта по росту высоты за последние ~2,5 с: h / (dh/dt); inf — не растёт."""
    pts = [(t, math.log(max(1e-3, b[3] - b[1]))) for t, b, _ in tr.hist if t >= tr.last_t - 2.5]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 1.0:
        return math.inf
    n = len(pts)
    mt = sum(p[0] for p in pts) / n
    ml = sum(p[1] for p in pts) / n
    den = sum((p[0] - mt) ** 2 for p in pts)
    slope = sum((p[0] - mt) * (p[1] - ml) for p in pts) / den if den > 0 else 0.0
    return 1 / slope if slope > 1e-6 else math.inf


def _sideways(tr: Track) -> float:
    """Скорость вбок за последние ~2 с, в высотах рамки в секунду."""
    pts = [(t, b) for t, b, _ in tr.hist if t >= tr.last_t - 2.0]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.4:
        return 0.0
    (t0, b0), (t1, b1) = pts[0], pts[-1]
    dx = abs(_centre(b1)[0] - _centre(b0)[0])
    h = (b0[3] - b0[1] + b1[3] - b1[1]) / 2
    return dx / max(h, 1e-3) / (t1 - t0)


class FaceRule:
    """Лицо держится: ≥ 2/3 попыток за face_hold_s, первая и последняя — попадания."""

    MIN_CONTRAST = 8.0  # СКО яркости лица (0…255): бледное отражение в стекле не считаем

    def __init__(self) -> None:
        self.attempts: deque[tuple[float, Face | None]] = deque()
        self.fired_t: float | None = None
        self.interval = 0.5

    def reset(self) -> None:
        self.attempts.clear()
        self.fired_t = None

    def best(self, faces: list[Face], face_min: float, aspect: float = ASPECT) -> Face | None:
        from .detect import face_ok

        ok = [f for f in faces if f.box[2] - f.box[0] >= face_min and f.contrast >= self.MIN_CONTRAST
              and face_ok(f, aspect)]
        return max(ok, key=lambda f: f.box[2] - f.box[0]) if ok else None

    @property
    def active(self) -> bool:
        return any(f is not None for _, f in self.attempts)

    def update(self, faces: list[Face], t: float, face_min: float, hold_s: float) -> dict | None:
        if self.attempts:
            dt = t - self.attempts[-1][0]
            if dt > 4 * max(self.interval, 0.5):  # детектор не запускался (не было движения) — заново
                self.attempts.clear()
            else:
                self.interval = 0.7 * self.interval + 0.3 * dt
        f = self.best(faces, face_min)
        self.attempts.append((t, f))
        while self.attempts and t - self.attempts[0][0] > hold_s:
            self.attempts.popleft()
        hits = [(ta, fa) for ta, fa in self.attempts if fa is not None]
        if not hits:
            if self.fired_t is not None and t - self.fired_t > hold_s:
                self.fired_t = None  # лицо ушло — можно снова
            return None
        if self.fired_t is not None:
            if t - self.fired_t < REARM_S:
                return None
            self.fired_t = None
        n = len(self.attempts)
        need = max(3, math.ceil(n * 2 / 3))
        span = t - hits[0][0]
        if f is None or len(hits) < need or span < hold_s - 1.5 * self.interval:
            return None
        self.fired_t = t
        big = max((fa for _, fa in hits), key=lambda x: x.box[2] - x.box[0])
        return {"width": round(big.box[2] - big.box[0], 3), "score": round(max(fa.score for _, fa in hits), 2),
                "hits": f"{len(hits)}/{n}", "held_s": round(span, 1), "contrast": round(big.contrast, 1)}
