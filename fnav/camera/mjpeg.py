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
FRESH_S = 0.3  # новому зрителю — не кадр старше этого (без зрителей кодер пишет 1 к/с): ждём следующий, свежий
SNAPSHOT_FRESH_S = 3.0  # снимок «сейчас» — не кадр старше (кодер пишет ≥ 1 к/с, пока видео идёт; нет — 503, а не стоп-кадр)
MATCH_S = 2.0  # снимок «N секунд назад» — кадр не дальше этого от того момента (кадров тогда не было — нет и снимка)

BOUNDARY = "frame"
# Chromium показывает часть multipart, только когда пришли заголовки следующей части (проверено на Chromium 152:
# кадр висел на экране до прихода следующего — задержка на кадр). Поэтому заголовки следующей части уходят сразу за
# кадром; Content-Length не нужен (браузеры ищут границу).
PART_HEAD = b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n\r\n"


def mjpeg_part(jpeg: bytes) -> bytes:
    """Кадр потока MJPEG и сразу заголовки следующей части (поток начинается с PART_HEAD)."""
    return jpeg + b"\r\n" + PART_HEAD


class MjpegHub:
    """Последний JPEG + уведомление asyncio-клиентов. Публикация — из любого потока, без блокировок."""

    def __init__(self) -> None:
        self.jpeg: bytes | None = None
        self.jpeg_t = 0.0  # monotonic публикации последнего кадра
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
        now = time.monotonic()
        self.jpeg = jpeg
        self.jpeg_t = now
        self.seq += 1
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
        """Кадр, ближайший к «seconds назад» (кодер пишет ≥ 1 кадра/с и без зрителей): (возраст, jpeg); None — кадра
        не дальше MATCH_S от того момента нет (видео тогда не шло: история хранит последние 12 с перед последним кадром,
        сколько бы ему ни было)."""
        now = time.monotonic()
        target = now - seconds
        with self._hist_lock:
            if not self._history:
                return None
            t, jpg = min(self._history, key=lambda e: abs(target - e[0]))
        if abs(target - t) > MATCH_S:
            return None
        return now - t, jpg

    def fresh_jpeg(self) -> bytes | None:
        """Последний кадр, если он не старше SNAPSHOT_FRESH_S (иначе видео сейчас нет — None)."""
        jpg = self.jpeg
        return jpg if jpg is not None and time.monotonic() - self.jpeg_t <= SNAPSHOT_FRESH_S else None

    def start_seq(self) -> int:
        """last_seq для next_frame() нового зрителя: последний кадр, если он свежий (≤ FRESH_S), иначе — ждать
        следующий (экран не покажет на миг кадр секундной давности: задний вид после нажатия кнопки)."""
        if self.jpeg is not None and time.monotonic() - self.jpeg_t > FRESH_S:
            return self.seq
        return -1

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

    def wants_frame(self, ts_us: int | None = None) -> bool:
        """Источнику: стоит ли вообще копировать lores-кадр сейчас. ts_us — метка кадра (монотонные мкс) от
        источника с неровным приходом кадров (IP-камера по Wi-Fi: кадры приходят пачками) — прореживание по ней,
        иначе по моменту прихода; по моменту прихода пачка теряла кадры (15 к/с камеры → 8,7 к/с на экране)."""
        if self.hub.demand:
            want = self.hub.max_rate()
            interval = 1.0 / (min(self.fps, want) if want else self.fps)
        else:
            interval = 1.0
        now = ts_us / 1e6 if ts_us else time.monotonic()
        if now < self._last:  # метки пошли заново (переподключение)
            self._last = now - interval
        if now - self._last >= interval * 0.8:  # не по концу кодирования
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
