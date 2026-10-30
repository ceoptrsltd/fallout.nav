"""Конвейер одного кадра: движение → (по расписанию) люди или лица → правила → события. Общий для сервиса и
офлайн-прогона записей (replay): время — только из кадров, поэтому прогон записи ведёт себя как живой.

Нейросеть — не больше одной на кадр: люди при движении 1 Гц (долго никого — 0,5 Гц), пока есть треки — 2 Гц;
лица 2 Гц, пока движение у центра или лицо уже видно. Перегрев — все частоты делятся на slow. Правило «стоит у
фургона» — на каждом кадре, по трекам и пятнам движения (без нейросети). Отметки движения (MarkRule, marks.py) —
тоже на каждом кадре: эпизоды движения без тревоги, «человек» / «машина» / «движение».

Один Watcher — одна камера (camera = "front" | "rear": своя ближняя линия, свои фон, треки и правила). Детекторы
общие: сервис сначала зовёт prepare() у каждой камеры, у которой новый кадр, — движение и что пора запустить (Plan),
потом отдаёт нейросеть одной камере (что сильнее просрочено) и зовёт finish(plan, run=…) у всех. process() — то же
для одной камеры (replay, тесты).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from fnav.models import VisionSettings

from .detect import Det, Face
from .frame import Frame
from .marks import MarkRule
from .motion import Motion, MotionGate
from .track import FaceRule, LoiterRule, Track, Tracker

log = logging.getLogger("fnav.watch.pipeline")

PERSON_IDLE_S = 1.0  # интервал детектора людей: движение, людей нет
PERSON_BACKOFF_S = 2.0  # …и уже BACKOFF_RUNS прогонов подряд пусто (деревья, дождь, блики)
BACKOFF_RUNS = 20
PERSON_TRACK_S = 0.5  # есть треки
FACE_S = 0.5
KEEP_PERSON_S = 3.0  # после последнего движения/трека детектор людей ещё работает (человек замер у капота)
BUSY_S = 10.0  # после последнего пятна движения / трека / присутствия камера «занята» (задней — полная частота)
HOOD_MARGIN = 0.1  # зона движения и лиц — до ближней линии + столько (ниже — капот, приборная панель, блики)
FACE_X = (0.2, 0.8)  # лицо «около центра» — по ширине
FACE_TOP = 0.05


class PersonDetector(Protocol):
    def detect(self, bgr: np.ndarray) -> list[Det]: ...


class FaceDetector(Protocol):
    region: tuple[float, float, float, float]

    def detect(self, bgr: np.ndarray) -> list[Face]: ...


@dataclass
class Event:
    kind: str
    ts: float  # unix
    data: dict


@dataclass
class Stats:
    frames: int = 0
    motion_frames: int = 0
    person_runs: int = 0
    face_runs: int = 0
    gate_ms: float = 0.0  # скользящие средние
    person_ms: float = 0.0
    face_ms: float = 0.0
    last_person_ms: float = 0.0
    last_face_ms: float = 0.0

    @staticmethod
    def ema(old: float, new: float, n: int) -> float:
        return new if n <= 1 else old + (new - old) * 0.1


@dataclass
class Snapshot:
    """Что показать на /debug.jpg (последний обработанный кадр)."""
    frame: Frame | None = None
    motion: Motion = field(default_factory=Motion)
    dets: list[Det] = field(default_factory=list)
    faces: list[Face] = field(default_factory=list)
    tracks: list[tuple[int, tuple, bool, float]] = field(default_factory=list)  # id, box, сработал, growth
    loiter: tuple[tuple, float, str] | None = None  # рамка, сколько стоит, источник (track | motion)
    ran: str = ""  # "person" | "face" | ""


@dataclass
class Plan:
    """Кадр после детектора движения: какую нейросеть пора запустить и насколько это просрочено."""
    frame: Frame
    snap: Snapshot
    net: str = ""  # "person" | "face" | "" — ничего не пора
    due: float = 0.0  # (сейчас − прошлый прогон) / интервал; ≥ 0.95 — пора


class Watcher:
    def __init__(self, person: PersonDetector | None, face: FaceDetector | None,
                 settings: VisionSettings | None = None, *, clock=time.time, camera: str = "front") -> None:
        self.person = person
        self.face = face
        self.clock = clock
        self.camera = camera
        self.near_line = 0.6
        self.face_region: tuple[float, float, float, float] = (FACE_X[0], FACE_TOP, FACE_X[1], 0.7)
        self.aspect = 4 / 3  # ширина/высота кадра — по кадрам (задняя 768×432 — 16:9)
        self.gate = MotionGate()
        self.tracker = Tracker()
        self.face_rule = FaceRule()
        self.loiter = LoiterRule()
        self.marks = MarkRule()
        self.stats = Stats()
        self.snap = Snapshot()
        self.slow = 1.0  # перегрев: частоты нейросетей делятся
        self._last_person = -1e18
        self._last_face = -1e18
        self._keep_person_until = -1e18
        self._empty_runs = 0  # прогонов детектора людей подряд без людей
        self._vehicles: list[Det] = []  # машины последнего прогона детектора людей (для отметок)
        self._busy_until = -1e18
        self._last_t = -1e18
        self.settings = settings or VisionSettings()
        self.apply(self.settings)

    def apply(self, s: VisionSettings) -> None:
        self.settings = s
        self.near_line = s.rear_near_line if self.camera == "rear" else s.near_line
        self.gate.set_bottom(self.near_line + HOOD_MARGIN)
        # снаружи лицо видно только сквозь стекло — выше приборной панели (у задней — так же от её линии)
        self.face_region = (FACE_X[0], FACE_TOP, FACE_X[1], min(0.95, self.near_line + HOOD_MARGIN))

    def reset(self) -> None:
        self.gate.reset()
        self.tracker.reset()
        self.face_rule.reset()
        self.loiter.reset()
        self.marks.reset()
        self._last_person = self._last_face = self._keep_person_until = -1e18
        self._busy_until = self._last_t = -1e18
        self._empty_runs = 0
        self.snap = Snapshot()

    @property
    def people(self) -> int:
        t = self.snap.frame.t if self.snap.frame is not None else 0.0
        return len(self.tracker.confirmed(t))

    @property
    def busy(self) -> bool:
        """За последние BUSY_S было пятно движения, трек или кто-то у фургона (задней камере — полная частота)."""
        return self._last_t < self._busy_until

    def _set_aspect(self, frame: Frame) -> None:
        a = frame.w / frame.h if frame.h else self.aspect
        if a != self.aspect:
            self.aspect = self.tracker.aspect = self.face_rule.aspect = self.loiter.aspect = a

    def process(self, frame: Frame) -> list[Event]:
        """Кадр одной камеры целиком: движение → нейросеть (если пора) → правила."""
        return self.finish(self.prepare(frame))

    def prepare(self, frame: Frame) -> Plan:
        """Детектор движения и расписание нейросетей: что пора запустить (сам прогон — в finish)."""
        s = self.settings
        st = self.stats
        t = frame.t
        self._set_aspect(frame)
        st.frames += 1
        t0 = time.perf_counter()
        m = self.gate.update(frame.y, t, frame.exposure_gain)
        st.gate_ms = st.ema(st.gate_ms, (time.perf_counter() - t0) * 1e3, st.frames)
        if m.moving:
            st.motion_frames += 1
            self._keep_person_until = t + KEEP_PERSON_S
        snap = Snapshot(frame=frame, motion=m)
        tracks_alive = bool(self.tracker.confirmed(t))
        if tracks_alive:
            self._keep_person_until = max(self._keep_person_until, t + 1.0)
        if m.blobs or tracks_alive or self.loiter.present or self.face_rule.active:
            self._busy_until = t + BUSY_S
        self._last_t = t
        person_due = face_due = 0.0  # насколько просрочено (>= 1 — пора)
        if s.approach and self.person is not None and t < self._keep_person_until:
            if tracks_alive:
                iv = PERSON_TRACK_S
            else:
                iv = PERSON_BACKOFF_S if self._empty_runs >= BACKOFF_RUNS else PERSON_IDLE_S
            iv *= self.slow
            person_due = (t - self._last_person) / iv
        if s.face and self.face is not None:
            near_centre = m.moving and m.touches(self.face_region)
            if near_centre or self.face_rule.active:
                face_due = (t - self._last_face) / (FACE_S * self.slow)
        if person_due >= 0.95 and person_due >= face_due:
            return Plan(frame, snap, "person", person_due)
        if face_due >= 0.95:
            return Plan(frame, snap, "face", face_due)
        return Plan(frame, snap)

    def finish(self, plan: Plan, run: bool = True) -> list[Event]:
        """Нейросеть из плана, если run (детектор общий — сервис отдаёт его одной камере за раз), и правила."""
        s = self.settings
        frame, snap = plan.frame, plan.snap
        t = frame.t
        events: list[Event] = []
        if run and plan.net == "person":
            events += self._run_person(frame, t, snap)
        elif run and plan.net == "face":
            events += self._run_face(frame, t, snap)
        confirmed = self.tracker.confirmed(t)
        if s.loiter:
            data = self.loiter.update(t, snap.motion.details, confirmed, self.near_line, s.loiter_s)
            if data:
                events.append(self._event("loiter", frame, data))
            lo = self.loiter
            if lo.box is not None and lo.since is not None:
                snap.loiter = (lo.box, t - lo.since, lo.source)
        if s.marks:
            vehicles = self._vehicles if snap.ran == "person" else None
            mark = self.marks.update(t, self._wall(frame), snap.motion.moving, snap.motion.blobs, vehicles=vehicles,
                                     people=bool(confirmed), alarm=bool(events))
            if mark:
                events.append(self._event("mark", frame, mark))
        snap.tracks = [(tr.id, tr.box, tr.fired_t is not None, tr.growth) for tr in confirmed]
        self.snap = snap
        return events

    def _wall(self, frame: Frame) -> float:
        return frame.wall if frame.wall is not None else self.clock()

    def _event(self, kind: str, frame: Frame, data: dict) -> Event:
        return Event(kind, self._wall(frame), {**data, "camera": self.camera})

    def _run_person(self, frame: Frame, t: float, snap: Snapshot) -> list[Event]:
        s = self.settings
        st = self.stats
        dt = t - self._last_person if self._last_person > -1e17 else None
        self._last_person = t
        t0 = time.perf_counter()
        dets = self.person.detect(frame.bgr)  # type: ignore[union-attr]
        ms = (time.perf_counter() - t0) * 1e3
        st.person_runs += 1
        st.last_person_ms = ms
        st.person_ms = st.ema(st.person_ms, ms, st.person_runs)
        self._empty_runs = 0 if dets else self._empty_runs + 1
        self.tracker.update(dets, t, dt)
        self._vehicles = list(getattr(self.person, "vehicles", None) or [])
        self.loiter.add_vehicles(t, self._vehicles)
        snap.dets, snap.ran = dets, "person"
        out = []
        for _tr, data in self.tracker.approach(t, self.near_line, s.approach_hold_s):
            out.append(self._event("approach", frame, data))
        return out

    def _run_face(self, frame: Frame, t: float, snap: Snapshot) -> list[Event]:
        s = self.settings
        st = self.stats
        self._last_face = t
        self.face.region = self.face_region  # type: ignore[union-attr]  # детектор общий у камер, зона — своя
        t0 = time.perf_counter()
        faces = self.face.detect(frame.bgr)  # type: ignore[union-attr]
        ms = (time.perf_counter() - t0) * 1e3
        st.face_runs += 1
        st.last_face_ms = ms
        st.face_ms = st.ema(st.face_ms, ms, st.face_runs)
        snap.faces, snap.ran = faces, "face"
        data = self.face_rule.update(faces, t, s.face_min, s.face_hold_s)
        return [self._event("face", frame, data)] if data else []

    # ── картинка для настройки ──
    def render(self, snap: Snapshot | None = None, quality: int = 80) -> bytes | None:
        import cv2

        snap = snap or self.snap
        fr = snap.frame
        if fr is None:
            return None
        img = fr.bgr.copy()
        h, w = img.shape[:2]

        def px(b):
            return int(b[0] * w), int(b[1] * h), int(b[2] * w), int(b[3] * h)

        ny = int(self.near_line * h)
        cv2.line(img, (0, ny), (w, ny), (0, 220, 255), 1)
        label = "near line (rear)" if self.camera == "rear" else "near line"
        cv2.putText(img, label, (4, ny - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 255), 1)
        if self.face is not None:
            x1, y1, x2, y2 = px(self.face_region)
            cv2.rectangle(img, (x1, y1), (x2, y2), (128, 128, 128), 1)
        for b in snap.motion.blobs:
            x1, y1, x2, y2 = px(b)
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 128, 0), 1)
        for d in snap.dets:
            x1, y1, x2, y2 = px(d.box)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 1)
        for tid, box, fired, growth in snap.tracks:
            x1, y1, x2, y2 = px(box)
            col = (0, 0, 255) if fired else (0, 255, 0)
            cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
            cv2.putText(img, f"#{tid} x{growth:.2f}", (x1, max(10, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)
        if snap.loiter is not None:
            box, held, src = snap.loiter
            x1, y1, x2, y2 = px(box)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 140, 255), 2)
            cv2.putText(img, f"loiter {held:.0f}s {src}", (x1, min(h - 24, y2 + 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (0, 140, 255), 1)
        for f in snap.faces:
            x1, y1, x2, y2 = px(f.box)
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 0, 255), 2)
            cv2.putText(img, f"{f.score:.2f} c{f.contrast:.0f}", (x1, y2 + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (255, 0, 255), 1)
        m = snap.motion
        st = self.stats
        text = (f"thr {m.thr:.0f} noise {m.noise:.1f} chg {m.frac * 100:.1f}%{' MOTION' if m.moving else ''}"
                f"{' RESET' if m.reset else ''}  person {st.last_person_ms:.0f}ms face {st.last_face_ms:.0f}ms"
                f"{'  [' + snap.ran + ']' if snap.ran else ''}")
        cv2.putText(img, text, (4, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
        cv2.putText(img, text, (4, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def tracks_info(self) -> list[dict]:
        out = []
        t = self.snap.frame.t if self.snap.frame is not None else 0.0
        for tr in self.tracker.confirmed(t):
            out.append(_track_dict(tr, t))
        return out


def _track_dict(tr: Track, t: float) -> dict:
    return {"id": tr.id, "box": [round(v, 3) for v in tr.box], "hits": len(tr.hist), "growth": round(tr.growth, 2),
            "grew": tr.grew, "sideways": round(tr.sideways, 2), "fired": tr.fired_t is not None,
            "holding_s": round(t - tr.hold_since, 1) if tr.hold_since is not None else None}
