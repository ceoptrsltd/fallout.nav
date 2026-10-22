"""Живое видео по требованию: отдельный лёгкий H.264 (lores) → MPEG-TS → HTTP-клиенты (/live.ts).

* ``LiveMux`` — превращает access unit второго энкодера в TS-пакеты (вызывается из потока энкодера,
  только быстрые операции, без блокировок: поток опроса V4L2 не должен ждать — иначе камера встанет).
  PTS непрерывны и при перезапуске энкодера (смена настроек, перезапуск конвейера).
* ``LiveHub`` — клиенты в asyncio: у каждого своя очередь; первый клиент включает энкодер, после ухода
  последнего он выключается через ``grace_s``. Медленный клиент теряет хвост и ждёт следующий ключевой кадр.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from .h264 import AccessUnitFixer
from .tsmux import PTS_START, TsMuxer, pts_from_us

log = logging.getLogger("fnav.camera.live")


class LiveUnavailable(Exception):
    pass


class LiveMux:
    def __init__(self, fps: int) -> None:
        self.fps = max(1, int(fps))
        self._fixer = AccessUnitFixer()
        self._mux = TsMuxer()
        self._t0: int | None = None
        self._base = PTS_START
        self._last_pts: int | None = None

    def new_session(self, fps: int | None = None) -> None:
        """Энкодер (пере)запускается: ждём его первый ключевой кадр, время продолжаем с того же места."""
        if fps:
            self.fps = max(1, int(fps))
        self._fixer = AccessUnitFixer()
        self._t0 = None

    def push(self, au: bytes, keyframe: bool, ts_us: int) -> tuple[bytes, bool] | None:
        au, keyframe = self._fixer.fix(au, keyframe)
        if self._t0 is None:
            if not keyframe or not self._fixer.has_params:
                return None
            self._t0 = ts_us
            if self._last_pts is not None:
                self._base = self._last_pts + 90000 // self.fps
        pts = self._base + pts_from_us(ts_us - self._t0)
        if self._last_pts is not None and pts <= self._last_pts:
            pts = self._last_pts + 1
        self._last_pts = pts
        return self._mux.mux(au, pts, keyframe=keyframe), keyframe


@dataclass(eq=False)
class LiveClient:
    queue: asyncio.Queue
    need_key: bool = True
    dropped: int = 0
    closed: bool = False


class LiveHub:
    """demand(True/False) — блокирующая функция (выполняется в потоке): включить/выключить энкодер.
    Возвращает текст ошибки или None."""

    def __init__(self, demand: Callable[[bool], str | None], *, grace_s: float = 5.0,
                 queue_max: int = 90) -> None:
        self.demand = demand
        self.grace_s = grace_s
        self.queue_max = queue_max
        self.running = False
        self.clients: set[LiveClient] = set()
        self.chunks = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._alock: asyncio.Lock | None = None
        self._stop_handle: asyncio.TimerHandle | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._alock = asyncio.Lock()

    # ── из потока энкодера ──
    def publish(self, chunk: bytes, keyframe: bool) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not self.clients:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, chunk, keyframe)
        except RuntimeError:
            pass

    def _dispatch(self, chunk: bytes, keyframe: bool) -> None:
        self.chunks += 1
        for c in list(self.clients):
            if c.need_key:
                if not keyframe:
                    continue
                c.need_key = False
            try:
                c.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # клиент не успевает (медленный канал): выбрасываем хвост, продолжим с ключевого кадра
                while not c.queue.empty():
                    c.queue.get_nowait()
                c.need_key = True
                c.dropped += 1

    # ── asyncio ──
    async def subscribe(self) -> LiveClient:
        if self._loop is None:
            self.bind_loop(asyncio.get_running_loop())
        assert self._alock is not None
        async with self._alock:
            if self._stop_handle is not None:
                self._stop_handle.cancel()
                self._stop_handle = None
            if not self.running:
                err = await asyncio.to_thread(self.demand, True)
                if err:
                    raise LiveUnavailable(err)
                self.running = True
            c = LiveClient(asyncio.Queue(self.queue_max))
            self.clients.add(c)
            log.info("live client connected (%d)", len(self.clients))
            return c

    def unsubscribe(self, c: LiveClient) -> None:
        if c.closed:
            return
        c.closed = True
        self.clients.discard(c)
        log.info("live client disconnected (%d left, %d resyncs)", len(self.clients), c.dropped)
        if not self.clients and self.running and self._loop is not None and self._stop_handle is None:
            self._stop_handle = self._loop.call_later(
                self.grace_s, lambda: asyncio.ensure_future(self._maybe_stop()))

    async def _maybe_stop(self) -> None:
        assert self._alock is not None
        async with self._alock:
            self._stop_handle = None
            if self.clients or not self.running:
                return
            self.running = False
            await asyncio.to_thread(self.demand, False)

    async def close(self) -> None:
        if self._stop_handle is not None:
            self._stop_handle.cancel()
            self._stop_handle = None
        self.clients.clear()
        if self.running:
            self.running = False
            await asyncio.to_thread(self.demand, False)
