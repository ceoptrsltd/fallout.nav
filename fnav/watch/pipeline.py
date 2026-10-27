"""Конвейер одного кадра: движение → (по расписанию) люди или лица → правила → события. Общий для сервиса и
офлайн-прогона записей (replay): время — только из кадров, поэтому прогон записи ведёт себя как живой.

Нейросеть — не больше одной на кадр: люди при движении 1 Гц (долго никого — 0,5 Гц), пока есть треки — 2 Гц;
лица 2 Гц, пока движение у центра или лицо уже видно. Перегрев — все частоты делятся на slow.
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
from .motion import Motion, MotionGate
from .track import FaceRule, Track, Tracker

log = logging.getLogger("fnav.watch.pipeline")

PERSON_IDLE_S = 1.0  # интервал детектора людей: движение, людей нет
PERSON_BACKOFF_S = 2.0  # …и уже BACKOFF_RUNS прогонов подряд пусто (деревья, дождь, блики)
BACKOFF_RUNS = 20
PERSON_TRACK_S = 0.5  # есть треки
FACE_S = 0.5
KEEP_PERSON_S = 3.0  # после последнего движения/трека детектор людей ещё работает (человек замер у капота)
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
    ran: str = ""  # "person" | "face" | ""


class Watcher:
    def __init__(self, person: PersonDetector | None, face: FaceDetector | None,
                 settings: VisionSettings | None = None, *, clock=time.time) -> None:
        self.person = person
        self.face = face
        self.clock = clock
        self.gate = MotionGate()
        self.tracker = Tracker()
        self.face_rule = FaceRule()
        self.stats = Stats()
        self.snap = Snapshot()
        self.slow = 1.0  # перегрев: частоты нейросетей делятся
        self._last_person = -1e18
        self._last_face = -1e18
        self._keep_person_until = -1e18
        self._empty_runs = 0  # прогонов детектора людей подряд без людей
        self.settings = settings or VisionSettings()
        self.apply(self.settings)

    def apply(self, s: VisionSettings) -> None:
        self.settings = s
        self.gate.set_bottom(s.near_line + HOOD_MARGIN)
        if self.face is not None:  # снаружи лицо видно только сквозь стекло — выше приборной панели
            self.face.region = (FACE_X[0], FACE_TOP, FACE_X[1], min(0.95, s.near_line + HOOD_MARGIN))

    def reset(self) -> None:
        self.gate.reset()
        self.tracker.reset()
        self.face_rule.reset()
        self._last_person = self._last_face = self._keep_person_until = -1e18
        self._empty_runs = 0
        self.snap = Snapshot()

    @property
    def people(self) -> int:
        t = self.snap.frame.t if self.snap.frame is not None else 0.0
        return len(self.tracker.confirmed(t))

    def process(self, frame: Frame) -> list[Event]:
        s = self.settings
        st = self.stats
        t = frame.t
        st.frames += 1
        t0 = time.perf_counter()
        m = self.gate.update(frame.y, t, frame.exposure_gain)
        st.gate_ms = st.ema(st.gate_ms, (time.perf_counter() - t0) * 1e3, st.frames)
        if m.moving:
            st.motion_frames += 1
            self._keep_person_until = t + KEEP_PERSON_S
        snap = Snapshot(frame=frame, motion=m)
        events: list[Event] = []
        tracks_alive = bool(self.tracker.confirmed(t))
        if tracks_alive:
            self._keep_person_until = max(self._keep_person_until, t + 1.0)

        person_due = face_due = 0.0  # насколько просрочено (>= 1 — пора)
        if s.approach and self.person is not None and t < self._keep_person_until:
            if tracks_alive:
                iv = PERSON_TRACK_S
            else:
                iv = PERSON_BACKOFF_S if self._empty_runs >= BACKOFF_RUNS else PERSON_IDLE_S
            iv *= self.slow
            person_due = (t - self._last_person) / iv
        if s.face and self.face is not None:
            near_centre = m.moving and m.touches(self.face.region)
            if near_centre or self.face_rule.active:
                face_due = (t - self._last_face) / (FACE_S * self.slow)
        if person_due >= 0.95 and person_due >= face_due:
            events += self._run_person(frame, t, snap)
        elif face_due >= 0.95:
            events += self._run_face(frame, t, snap)
        snap.tracks = [(tr.id, tr.box, tr.fired_t is not None, tr.growth) for tr in self.tracker.confirmed(t)]
        self.snap = snap
        return events

    def _wall(self, frame: Frame) -> float:
        return frame.wall if frame.wall is not None else self.clock()

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
        snap.dets, snap.ran = dets, "person"
        out = []
        for _tr, data in self.tracker.approach(t, s.near_line, s.approach_hold_s):
            out.append(Event("approach", self._wall(frame), data))
        return out

    def _run_face(self, frame: Frame, t: float, snap: Snapshot) -> list[Event]:
        s = self.settings
        st = self.stats
        self._last_face = t
        t0 = time.perf_counter()
        faces = self.face.detect(frame.bgr)  # type: ignore[union-attr]
        ms = (time.perf_counter() - t0) * 1e3
        st.face_runs += 1
        st.last_face_ms = ms
        st.face_ms = st.ema(st.face_ms, ms, st.face_runs)
        snap.faces, snap.ran = faces, "face"
        data = self.face_rule.update(faces, t, s.face_min, s.face_hold_s)
        return [Event("face", self._wall(frame), data)] if data else []

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

        s = self.settings
        ny = int(s.near_line * h)
        cv2.line(img, (0, ny), (w, ny), (0, 220, 255), 1)
        cv2.putText(img, "near line", (4, ny - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 255), 1)
        if self.face is not None:
            x1, y1, x2, y2 = px(self.face.region)
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
