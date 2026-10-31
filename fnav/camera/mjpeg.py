"""Раздача lores-кадров: поток JPEG-кодера + хаб для нескольких HTTP-клиентов."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque

from .jpeg import LoresFrame, encode_jpeg

log = logging.getLogger("fnav.camera.mjpeg")

HISTORY_S = 12.0  # недавние кадры: снимок «за 5 с до события» для охраны (/snapshot.jpg?before=5)
HISTORY_STEP_S = 0.5  # в историю — не чаще раза в столько (при клиентах MJPEG кадров больше); ~1 МБ памяти


class MjpegHub:
    """Последний JPEG + уведомление asyncio-клиентов. Публикация — из любого потока, без блокировок."""

    def __init__(self) -> None:
        self.jpeg: bytes | None = None
        self.seq = 0
        self.clients = 0
        self.rates: dict[object, float | None] = {}  # клиент → нужная частота (None — полная)
        self.last_snapshot_request = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
        self._history: deque[tuple[float, bytes]] = deque()  # (monotonic, jpeg) за HISTORY_S
        self._hist_lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()

    def publish(self, jpeg: bytes) -> None:
        self.jpeg = jpeg
        self.seq += 1
        now = time.monotonic()
        with self._hist_lock:
            h = self._history
            if not h or now - h[-1][0] >= HISTORY_STEP_S:
                h.append((now, jpeg))
            while h and now - h[0][0] > HISTORY_S:
                h.popleft()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._notify)
            except RuntimeError:
                pass

    def _notify(self) -> None:
        ev = self._event
        self._event = asyncio.Event()
        if ev is not None:
            ev.set()

    def frame_before(self, seconds: float) -> tuple[float, bytes] | None:
        """Кадр, ближайший к «seconds назад» (кодер пишет ≥ 1 кадра/с и без зрителей): (возраст, jpeg); None — пусто."""
        now = time.monotonic()
        with self._hist_lock:
            if not self._history:
                return None
            t, jpg = min(self._history, key=lambda e: abs(now - seconds - e[0]))
        return now - t, jpg

    async def next_frame(self, last_seq: int, timeout: float = 5.0) -> tuple[int, bytes] | None:
        """Дождаться кадра новее last_seq."""
        if self.jpeg is not None and self.seq != last_seq:
            return self.seq, self.jpeg
        if self._event is None:
            self.bind_loop(asyncio.get_running_loop())
        ev = self._event
        assert ev is not None
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            return None
        if self.jpeg is None:
            return None
        return self.seq, self.jpeg

    def max_rate(self) -> float | None:
        """Наибольшая частота, нужная клиентам MJPEG; None — полная (или клиентов с ограничением нет)."""
        rates = list(self.rates.values())
        if not rates or any(r is None for r in rates):
            return None
        return max(rates)  # type: ignore[type-var]

    @property
    def demand(self) -> bool:
        """Нужны ли кадры с полной частотой (есть клиенты или недавно просили снимок)."""
        return self.clients > 0 or time.monotonic() - self.last_snapshot_request < 3.0


class JpegWorker:
    """Берёт самый свежий lores-кадр (слот на 1 кадр, старые выбрасываются) и кодирует в JPEG.

    Частота: lores.fps при наличии клиентов (или наибольшая из запрошенных ?fps), иначе 1 кадр/с (для /snapshot.jpg).
    """

    def __init__(self, hub: MjpegHub, fps: float, quality: int) -> None:
        self.hub = hub
        self.fps = max(1.0, float(fps))
        self.quality = quality
        self._slot: LoresFrame | None = None
        self._cv = threading.Condition()
        self._stop = False
        self._last = 0.0
        self._thr = threading.Thread(target=self._loop, name="jpeg", daemon=True)

    def start(self) -> None:
        self._thr.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thr.join(3)

    def wants_frame(self) -> bool:
        """Источнику: стоит ли вообще копировать lores-кадр сейчас."""
        if self.hub.demand:
            want = self.hub.max_rate()
            interval = 1.0 / (min(self.fps, want) if want else self.fps)
        else:
            interval = 1.0
        now = time.monotonic()
        if now - self._last >= interval * 0.8:  # по моменту прихода кадра (не по концу кодирования)
            self._last = now
            return True
        return False

    def submit(self, frame: LoresFrame) -> None:
        with self._cv:
            self._slot = frame
            self._cv.notify()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while self._slot is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                frame, self._slot = self._slot, None
            try:
                self.hub.publish(encode_jpeg(frame, self.quality))
            except Exception:
                log.exception("jpeg encode failed")
