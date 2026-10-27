"""Сопровождение людей между кадрами детектора и правила событий.

approach: трек подтверждён (≥ 3 попаданий за окно ~3 с), нижний край рамки (ноги / край капота) дошёл до ближней
линии, рамка выросла ≥ 1,25× (или время до контакта по росту < 4 с), человек не идёт вбок — и так
approach_hold_s. Прохожие (высота постоянна, быстро вбок) не проходят. Повтор тем же треком — только если он ушёл
от линии и вернулся, или через REARM_S.

face: крупное лицо около центра — ≥ 2/3 попыток детектора за face_hold_s (4 из 6 при 2 Гц).

loiter: кто-то стоит рядом около центра ≥ loiter_s — по треку или по крупному «живому» пятну движения (LoiterRule).

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
HOLD_UNSEEN_S = 1.5  # у линии, но детектор потерял: удержание засчитывается, если видели не дольше столько назад


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
                # свежей детекции нет (ночью сеть теряет замерший у капота силуэт): удержание идёт, если человек
                # был у линии при последней детекции совсем недавно
                if (tr.hold_since is not None and tr.fired_t is None and t - tr.last_t <= HOLD_UNSEEN_S
                        and t - tr.hold_since >= hold_s):
                    fired.append((tr, self._fire(tr, t)))
                continue
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
                fired.append((tr, self._fire(tr, t)))
        return fired

    @staticmethod
    def _fire(tr: Track, t: float) -> dict:
        tr.fired_t = t
        assert tr.hold_since is not None
        return {"track": tr.id, "height": round(tr.h, 3), "bottom": round(tr.box[3], 3),
                "growth": round(tr.growth, 2), "held_s": round(t - tr.hold_since, 1),
                "score": round(max(s for _, _, s in tr.hist), 2), "hits": len(tr.hist)}


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


# ── правило «стоит у фургона» (loiter) ──
LOITER_X = (0.15, 0.85)  # «около центра»: центр рамки/пятна по ширине
LOITER_MARGIN = 0.03  # низ рамки не выше ближней линии больше чем на столько
LOITER_MIN_H = 0.2  # высота рамки/пятна — доля кадра (дальше от фургона люди меньше)
LOITER_ASPECT = (0.2, 2.0)  # ширина/высота пятна в одних единицах: не струйка воды; у самого стекла силуэт широк
LOITER_FILL = 0.35  # пятно заполняет рамку (не дворник, не россыпь капель)
LOITER_ACTIVE = 0.03  # доля рамки, изменившаяся с прошлого кадра, — пятно «живое»
LOITER_ALIVE_S = 4.0  # пятно без жизни дольше — неподвижный предмет (машина встала), не человек
LOITER_LIGHT = (0.7, 120.0)  # светлее фона и яркое — фары, блики, а не силуэт
LOITER_TRACK_AGE_S = 1.5
LOITER_GAP_S = 3.0  # пропуск дольше — присутствие кончилось
LOITER_COVER = 0.7  # доля времени присутствия, когда что-то действительно было видно
LOITER_EMPTY_S = 10.0  # перевзвод: столько никого у фургона
LOITER_GROWING = 1.2  # рамка за ~1,5 с выросла больше — ещё подходит: отсчёт начнётся, когда остановится
LOITER_VEHICLE_S = 3.0  # пятно внутри машины, найденной за столько, — не человек
LOITER_VEHICLE_MIN = 0.5  # уверенность; ночью сеть видит «машину» во всём кадре (кабина) с ~0,45 — не верим
LOITER_VEHICLE_MAX_H = 0.8  # рамка во весь кадр — это сам фургон


class LoiterRule:
    """Кто-то стоит у фургона около центра ≥ loiter_s. Свидетельство на кадре — трек человека рядом (низ рамки у
    ближней линии) или крупное «живое» тёмное пятно движения там же: ночью нейросеть теряет силуэт против фонаря,
    а детектор движения держит его (внутри переднего плана фон почти не учится). Прохожие отсеиваются скоростью
    вбок и тем, что проходят быстрее loiter_s; стоящие машины и предметы — тем, что не шевелятся; фары и смена
    света — сбросом фона в детекторе движения и яркостью пятна. Срабатывает раз за присутствие; снова — когда у
    фургона никого не было LOITER_EMPTY_S. Отсчёт loiter_s — с момента, когда человек подошёл (рамка перестала расти):
    подход — дело правила approach; пятно внутри машины, которую нашла та же нейросеть, не считается."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.since: float | None = None  # начало присутствия
        self.last_ev: float | None = None  # последнее свидетельство
        self.last_t: float | None = None
        self.covered = 0.0
        self.alive_t = -1e18
        self.armed = True
        self.pos: deque[tuple[float, float, float]] = deque()  # (t, cx, h) свидетельств
        self.box: Box | None = None  # для картинки
        self.source = ""
        self.vehicles: list[tuple[float, Box]] = []  # (t, рамка) машин

    @property
    def present(self) -> bool:
        return self.since is not None

    def add_vehicles(self, t: float, dets: list[Det]) -> None:
        self.vehicles = [(tv, b) for tv, b in self.vehicles if t - tv <= LOITER_VEHICLE_S] + [
            (t, d.box) for d in dets if d.score >= LOITER_VEHICLE_MIN and d.h <= LOITER_VEHICLE_MAX_H]

    def _in_vehicle(self, t: float, box: Box) -> bool:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        for tv, (x1, y1, x2, y2) in self.vehicles:
            mx, my = 0.1 * (x2 - x1), 0.1 * (y2 - y1)
            if t - tv <= LOITER_VEHICLE_S and x1 - mx <= cx <= x2 + mx and y1 - my <= cy <= y2 + my:
                return True
        return False

    def _growing(self, t: float) -> bool:
        """Медиана высоты за последние 1,5 с больше, чем за 1,5 с до того, в LOITER_GROWING раз (ещё подходит)."""
        new = sorted(h for tp, _, h in self.pos if t - tp <= 1.5)
        old = sorted(h for tp, _, h in self.pos if 1.5 < t - tp <= 3.0)
        if len(new) < 2 or len(old) < 2:
            return False
        return new[len(new) // 2] > LOITER_GROWING * old[len(old) // 2]

    def _candidate(self, t: float, details, tracks: list[Track], near_line: float):
        def close(box: Box) -> bool:
            h = box[3] - box[1]
            cx = (box[0] + box[2]) / 2
            return h >= LOITER_MIN_H and box[3] >= near_line - LOITER_MARGIN and LOITER_X[0] <= cx <= LOITER_X[1]

        near = [tr for tr in tracks
                if t - tr.last_t <= LOITER_TRACK_AGE_S and close(tr.box) and _sideways(tr) <= SIDEWAYS]
        if near:
            return ("track", max(near, key=lambda tr: tr.h).box)
        blobs = []
        for b in details:
            w = (b.box[2] - b.box[0]) * ASPECT
            if not close(b.box) or b.fill < LOITER_FILL or not LOITER_ASPECT[0] <= w / b.h <= LOITER_ASPECT[1]:
                continue
            if b.bright >= LOITER_LIGHT[0] and b.level >= LOITER_LIGHT[1] or self._in_vehicle(t, b.box):
                continue
            if b.active >= LOITER_ACTIVE:
                self.alive_t = t
            blobs.append(b)
        if blobs and t - self.alive_t <= LOITER_ALIVE_S:
            b = max(blobs, key=lambda x: x.h * (x.box[2] - x.box[0]))
            return ("motion", b.box)
        return None

    def update(self, t: float, details, tracks: list[Track], near_line: float, hold_s: float) -> dict | None:
        dt = min(1.0, t - self.last_t) if self.last_t is not None else 0.0
        self.last_t = t
        cand = self._candidate(t, details, tracks, near_line)
        if cand is not None:
            box = cand[1]
            self.pos.append((t, (box[0] + box[2]) / 2, box[3] - box[1]))
            while self.pos and t - self.pos[0][0] > 3.0:
                self.pos.popleft()
            t0, x0, h0 = self.pos[0]
            if t - t0 >= 2.0:  # прохожий: за ~3 с заметно сместился вбок
                speed = abs((box[0] + box[2]) / 2 - x0) * ASPECT / (t - t0) / max(1e-3, (h0 + box[3] - box[1]) / 2)
                if speed > SIDEWAYS:
                    cand = None
        if cand is not None:
            if self.since is None or self.last_ev is None or t - self.last_ev > LOITER_GAP_S or self._growing(t):
                self.since, self.covered = t, 0.0  # пришёл (или ещё подходит — отсчёт заново)
            else:
                self.covered += dt
            self.last_ev = t
            self.source, self.box = cand
        else:
            self.box = None
            if self.since is not None and (self.last_ev is None or t - self.last_ev > LOITER_GAP_S):
                self.since = None
        if not self.armed and (self.last_ev is None or t - self.last_ev >= LOITER_EMPTY_S):
            self.armed = True  # у фургона давно никого
        if not self.armed or self.since is None or cand is None:
            return None
        held = t - self.since
        if held < hold_s or self.covered < LOITER_COVER * held:
            return None
        self.armed = False
        box = self.box
        assert box is not None
        return {"held_s": round(held, 1), "source": self.source, "height": round(box[3] - box[1], 3),
                "bottom": round(box[3], 3), "x": round((box[0] + box[2]) / 2, 3),
                "covered": round(self.covered / held, 2) if held > 0 else 1.0}
