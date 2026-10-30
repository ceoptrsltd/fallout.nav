"""Отметки движения — без тревоги: что двигалось в кадре, пока охрана смотрит (серые метки на шкале записи).

Эпизод — подтверждённое движение (MotionGate: 2 из 3 кадров) с паузами не дольше MARK_GAP_S. Отметка — когда эпизод
кончился, а у длинного — ещё каждые MARK_CHUNK_S: событие "mark", data {what, start, dur_s}; start — начало куска
(unix), с него метка на шкале. what — что нейросеть видела за кусок: "person" — человек (прохожий: тревогу дают
только правила approach / face / loiter), "vehicle" — машина / мотоцикл / автобус / грузовик на месте пятна
движения, иначе "motion" (свет фар, тени, ветки, дождь). Кусок, в котором была тревога, отметки не даёт: тревога и
так на шкале.
"""

from __future__ import annotations

from dataclasses import dataclass

from .detect import Box, Det

MARK_GAP_S = 10.0  # тишина дольше — эпизод кончился
MARK_CHUNK_S = 60.0  # длинный эпизод (ветер, дождь) — отметка раз в столько
VEHICLE_MIN = 0.5  # уверенность «машины» (ночью сеть видит машину во всём кадре с ~0,45 — не верим)
VEHICLE_MAX_H = 0.8  # рамка почти во весь кадр — это сам фургон / кабина


@dataclass
class _Chunk:
    t0: float  # время кадров (монотонное / PTS записи)
    wall0: float  # unix
    last_t: float  # последнее движение
    person: bool = False
    vehicle: bool = False
    alarm: bool = False


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class MarkRule:
    def __init__(self) -> None:
        self.cur: _Chunk | None = None

    def reset(self) -> None:
        self.cur = None

    def update(self, t: float, wall: float, moving: bool, blobs: list[Box], *, vehicles: list[Det] | None = None,
               people: bool = False, alarm: bool = False) -> dict | None:
        """Кадр: движение и его пятна, машины от нейросети (если она прогонялась на этом кадре), есть ли живые
        треки людей, была ли на этом кадре тревога. → data отметки или None."""
        out = None
        cur = self.cur
        if cur is not None and t - cur.last_t > MARK_GAP_S:
            out, cur = self._close(cur), None
        if moving:
            if cur is None:
                cur = _Chunk(t, wall, t)
            cur.last_t = t
        if cur is not None:
            cur.person = cur.person or people
            cur.alarm = cur.alarm or alarm
            if vehicles and blobs and not cur.vehicle:
                cur.vehicle = any(_overlap(v.box, b) for v in vehicles
                                  if v.score >= VEHICLE_MIN and v.h <= VEHICLE_MAX_H for b in blobs)
            if out is None and t - cur.t0 >= MARK_CHUNK_S:
                out = self._close(cur)
                cur = _Chunk(t, wall, t) if moving else None  # дальше — следующий кусок того же эпизода
        self.cur = cur
        return out

    @staticmethod
    def _close(c: _Chunk) -> dict | None:
        if c.alarm:
            return None
        what = "person" if c.person else "vehicle" if c.vehicle else "motion"
        return {"what": what, "start": round(c.wall0, 3), "dur_s": round(c.last_t - c.t0, 1)}
