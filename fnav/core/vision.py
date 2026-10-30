"""Клиент fnav-watch (127.0.0.1:8082): камера на охране — человек подходит / лицо у стекла / стоит у фургона
(docs/contracts/vision.md).

Ядро раз в секунду читает /status: сообщает «смотреть / не смотреть» (охрана + settings.alarm.vision.enabled) через
POST /arm, если сервис думает иначе, и передаёт новые события в AlarmService.vision_event (с data.camera — "front" /
"rear"), а отметки движения без тревоги ("mark") — в on_mark (fnav/core/marks.py). Пока распознавание выключено и сервис уже не смотрит — не опрашиваем. Заднюю камеру сервис смотрит сам по
настройкам; её состояние (cameras.rear в /status) — в AlarmVision.rear.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from fnav.core.alarm import VISION_KINDS
from fnav.core.util import cancel_tasks
from fnav.models import AlarmVision, AlarmVisionCamera, VisionSettings

log = logging.getLogger("fnav.core.vision")

WATCH_URL = "http://127.0.0.1:8082"
POLL_S = 1.0
STALE_S = 30.0  # события старше — не тревога (ядро перезапустилось, а сервис помнит старое)
UNAVAILABLE = "Vision service (fnav-watch) is not responding"


class VisionClient:
    def __init__(self, *, get_settings: Callable[[], VisionSettings], get_armed: Callable[[], bool],
                 on_event: Callable[[str, dict], Awaitable[Any]], base_url: str = WATCH_URL,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time,
                 on_mark: Callable[[dict, float], Any] | None = None) -> None:
        self.get_settings = get_settings
        self.get_armed = get_armed
        self.on_event = on_event
        self.on_mark = on_mark
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2.0, connect=1.0))
        self._task: asyncio.Task | None = None
        self.available = False
        self.watching = False  # что сообщил сервис
        self._may_watch = False  # сервис мог остаться смотреть (не отвечал) — сказать «не смотри», когда ответит
        self.people = 0
        self.error: str | None = None
        self.rear: AlarmVisionCamera | None = None  # cameras.rear из /status (сервис смотрит и заднюю)
        self._seq: int | None = None  # последнее обработанное событие (None — ещё не читали)
        self._logged_down = False

    def status(self) -> AlarmVision:
        enabled = self.get_settings().enabled
        error = self.error if enabled else None
        if enabled and not self.available:
            error = UNAVAILABLE
        rear = self.rear if enabled and self.available else None
        if rear is not None and not self.watching:
            rear = rear.model_copy(update={"watching": False, "people": 0})
        return AlarmVision(enabled=enabled, available=self.available, watching=self.watching,
                           people=self.people if self.watching else 0, error=error, rear=rear)

    async def start(self, background: bool = True) -> None:
        if background:
            self._task = asyncio.create_task(self._loop(), name="vision-poll")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        await self._client.aclose()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await self.poll()
            except Exception:
                log.exception("vision poll")

    async def poll(self) -> None:
        enabled = self.get_settings().enabled
        want = enabled and self.get_armed()
        if not enabled and not self._may_watch:
            self.available = False
            return
        try:
            r = await self._client.get(f"{self.base_url}/status", timeout=1.0)
            r.raise_for_status()
            st = r.json()
        except (httpx.HTTPError, ValueError) as e:
            if not self._logged_down:
                log.info("vision: %s (%s)", UNAVAILABLE, type(e).__name__)
                self._logged_down = True
            self.available = self.watching = False
            self.rear = None
            return
        if self._logged_down or not self.available:
            log.info("vision: fnav-watch is available (%s)", self.base_url)
        self._logged_down = False
        self.available = True
        self.watching = self._may_watch = bool(st.get("watching"))
        self.people = int(st.get("people") or 0)
        self.error = st.get("error") or None
        self.rear = _camera(st, "rear")
        await self._take_events(st)
        if self.watching != want and await self._send_arm(want):
            self.watching = self._may_watch = want

    async def _take_events(self, st: dict) -> None:
        seq = int(st.get("seq") or 0)
        if self._seq is not None and seq < self._seq:
            self._seq = 0  # сервис перезапустился — счёт заново
        last, self._seq = self._seq, seq
        now = self.clock()
        for ev in sorted(st.get("events") or [], key=lambda e: e.get("seq", 0)):
            n, kind, ts = ev.get("seq", 0), ev.get("kind"), ev.get("ts") or 0
            if last is not None and n <= last or now - ts > STALE_S:
                continue
            if kind == "mark":  # отметка без тревоги: только на шкалу записи
                if self.on_mark is not None and isinstance(ev.get("data"), dict):
                    try:
                        self.on_mark(dict(ev["data"]), float(ts))
                    except Exception:
                        log.exception("vision mark")
                continue
            if kind not in VISION_KINDS:
                continue
            log.warning("vision: %s %s", kind, ev.get("data") or {})
            try:
                await self.on_event(kind, dict(ev.get("data") or {}))
            except Exception:
                log.exception("vision event")

    async def _send_arm(self, watching: bool) -> bool:
        self._may_watch = self._may_watch or watching
        try:
            r = await self._client.post(f"{self.base_url}/arm", json={"watching": watching}, timeout=2.0)
            r.raise_for_status()
            log.info("vision: %s", "watching" if watching else "stopped")
            return True
        except httpx.HTTPError as e:
            log.warning("vision: cannot send arm state (%s)", type(e).__name__)
            return False


def _camera(st: dict, name: str) -> AlarmVisionCamera | None:
    """cameras.<name> из /status fnav-watch; нет (камеру не смотрит, старый сервис) или кривое — None."""
    cams = st.get("cameras")
    cam = cams.get(name) if isinstance(cams, dict) else None
    if not isinstance(cam, dict):
        return None
    try:
        return AlarmVisionCamera(watching=bool(cam.get("watching")), people=int(cam.get("people") or 0),
                                 error=cam.get("error") or None)
    except (TypeError, ValueError):
        return None
