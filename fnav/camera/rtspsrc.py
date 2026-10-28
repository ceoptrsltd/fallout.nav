"""Источник «задняя камера»: IP-камера по RTSP (H.264) через процесс fnav.camera.rtsp_pull.

Основной поток идёт в конвейер камеры как есть (``on_video``: запись, MediaMTX) — без перекодирования. Малые кадры
(``on_lores``: MJPEG для экрана и снимков, кольцо для fnav-watch) — декодированные: со второго потока камеры
(``lores_url``, отдельный процесс), иначе с основного. Пока малые кадры никому не нужны часто, декодируются только
ключевые (~1 к/с: свежий снимок для тревоги дёшево); есть зрители или fnav-watch — полная частота lores.

Метки времени: часы камеры привязываются к монотонным часам при подключении; уход больше чем на 2 с (или скачок
меток) — привязка заново; метки только растут. Камеру нельзя попросить о ключевом кадре — ``request_keyframe`` пуст.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import quote, urlsplit, urlunsplit

import numpy as np

from fnav.models import RearCameraSettings

from .jpeg import LoresFrame
from .rtsp_pull import F_KEY, HEADER, LORES_HEAD, T_ERROR, T_INFO, T_LORES, T_VIDEO, URL_ENV, redact
from .sources import FrameSink

log = logging.getLogger("fnav.camera.rear")

RESYNC_US = 2_000_000  # часы камеры ушли от наших дальше — привязать заново
DEMAND_POLL_S = 0.25
SUB_IDLE_S = 10.0


def with_credentials(url: str, username: str, password: str) -> str:
    """rtsp://host/path + логин/пароль → rtsp://user:pass@host/path (уже указанные в адресе — не трогаем)."""
    if not url or not username:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return urlunsplit(parts._replace(netloc=f"{auth}@{parts.netloc}"))


def url_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


class Clock:
    """Метки камеры (мкс, свои часы; -1 — нет) → монотонные мкс нашего процесса, только возрастающие."""

    def __init__(self, mono: Callable[[], float] = time.monotonic) -> None:
        self.mono = mono
        self.offset: int | None = None
        self.last = -1

    def map(self, cam_us: int) -> int:
        now = int(self.mono() * 1e6)
        if cam_us < 0:
            ts = now
        else:
            if self.offset is None or abs(cam_us + self.offset - now) > RESYNC_US:
                self.offset = now - cam_us
            ts = cam_us + self.offset
        if ts <= self.last:
            ts = self.last + 1
        self.last = ts
        return ts


def default_cmd(*, video: bool, transport: str, lores_w: int) -> list[str]:
    cmd = [sys.executable, "-m", "fnav.camera.rtsp_pull", "--transport", transport, "--lores-w", str(lores_w)]
    if not video:
        cmd.append("--no-video")
    return cmd


class _Helper:
    """Один процесс rtsp_pull: запуск, чтение сообщений в потоке, команды в stdin."""

    def __init__(self, name: str, url: str, cmd: list[str], on_msg: Callable[[int, int, int, bytes], None],
                 on_exit: Callable[[str, str | None], None]) -> None:
        self.name = name
        self.url = url
        self.cmd = cmd
        self.on_msg = on_msg
        self.on_exit = on_exit
        self.proc: subprocess.Popen | None = None
        self.stderr_tail: deque[str] = deque(maxlen=8)
        self.error: str | None = None
        self.sent_fps: float | None = None
        self._stopping = False

    def start(self) -> None:
        env = dict(os.environ, **{URL_ENV: self.url})
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, bufsize=0)
        threading.Thread(target=self._read, name=f"rear-{self.name}", daemon=True).start()
        threading.Thread(target=self._read_err, name=f"rear-{self.name}-err", daemon=True).start()

    def set_lores(self, fps: float) -> None:
        if self.sent_fps == fps or self.proc is None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(f"lores {fps:g}\n".encode())
            self.proc.stdin.flush()
            self.sent_fps = fps
        except (BrokenPipeError, OSError, ValueError):
            pass

    def stop(self) -> None:
        self._stopping = True
        p = self.proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()  # помощник выходит по концу stdin
        except OSError:
            pass
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()

    def _read(self) -> None:
        p = self.proc
        assert p is not None and p.stdout is not None
        out = p.stdout
        try:
            while True:
                head = _read_exact(out, HEADER.size)
                if head is None:
                    break
                typ, flags, n, ts = HEADER.unpack(head)
                payload = _read_exact(out, n) if n else b""
                if payload is None:
                    break
                if typ == T_ERROR:
                    self.error = payload.decode("utf-8", "replace")
                    continue
                self.on_msg(typ, flags, ts, payload)
        except Exception:
            log.exception("%s: reader failed", self.name)
        finally:
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
            if not self._stopping:
                tail = " | ".join(self.stderr_tail)
                self.on_exit(self.name, self.error or (redact_text(tail, self.url) if tail else None))

    def _read_err(self) -> None:
        p = self.proc
        assert p is not None and p.stderr is not None
        for raw in p.stderr:
            line = redact_text(raw.decode("utf-8", "replace").rstrip(), self.url)
            if line:
                self.stderr_tail.append(line[-300:])
                log.debug("%s: %s", self.name, line)


def redact_text(text: str, url: str) -> str:
    return text.replace(url, redact(url)) if url else text


def _read_exact(f, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class RtspCameraSource:
    def __init__(self, rear: RearCameraSettings, sink: FrameSink, *, password: str = "",
                 cmd_factory: Callable[..., list[str]] = default_cmd,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.rear = rear
        self.sink = sink
        self.url = with_credentials(rear.url, rear.username, password)
        self.lores_url = with_credentials(rear.lores_url, rear.username, password) if rear.lores_url else ""
        self.cmd_factory = cmd_factory
        self.mono = mono
        host = url_host(rear.url)
        self.sensor_mode = f"RTSP {host}" if host else "RTSP"
        self.output_size: str | None = None
        self.codec: str | None = None
        self.stream_fps: float | None = None
        self.lores_size: tuple[int, int] | None = None
        self.exposure = None
        self._clock = Clock(mono)
        self._lores_clock = Clock(mono)
        self._main: _Helper | None = None
        self._sub: _Helper | None = None
        self._sub_used = 0.0
        self._live: Callable[[bytes, bool, int], None] | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._demand_thr: threading.Thread | None = None

    # ── Source ──
    def start(self) -> None:
        if not self.rear.url:
            raise RuntimeError("the camera address (rtsp://…) is not set")
        if not self.rear.url.lower().startswith(("rtsp://", "rtsps://")) and not os.path.exists(self.rear.url):
            raise RuntimeError("the camera address must start with rtsp://")
        self._stop.clear()
        lores_main = not self.lores_url
        self._main = _Helper("main", self.url, self.cmd_factory(video=True, transport=self.rear.transport,
                                                                 lores_w=self.rear.lores.w),
                             self._on_main_msg, self._on_exit)
        self._main.start()
        if lores_main:
            self._main.set_lores(1)
        log.info("rear camera: connecting to %s%s", redact(self.url),
                 f" (small frames from {redact(self.lores_url)})" if self.lores_url else "")
        self._demand_thr = threading.Thread(target=self._demand_loop, name="rear-demand", daemon=True)
        self._demand_thr.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            helpers = [h for h in (self._main, self._sub) if h is not None]
            self._main = self._sub = None
        for h in helpers:
            h.stop()
        if self._demand_thr is not None:
            self._demand_thr.join(2)

    def request_keyframe(self) -> None:
        """IP-камеру о ключевом кадре не попросить: регистратор дождётся очередного."""

    # живое видео (/live.ts): тот же основной поток, без второго кодирования
    def start_live(self, bitrate_kbps: int, on_video) -> None:
        self._live = on_video

    def stop_live(self) -> None:
        self._live = None

    # ── сообщения помощников ──
    def _on_main_msg(self, typ: int, flags: int, ts: int, payload: bytes) -> None:
        if typ == T_VIDEO:
            key = bool(flags & F_KEY)
            t = self._clock.map(ts)
            self.sink.on_video(payload, key, t)
            live = self._live
            if live is not None:
                live(payload, key, t)
        elif typ == T_INFO:
            self._on_info(payload)
        elif typ == T_LORES:
            self._on_lores(payload, ts, self._clock)

    def _on_sub_msg(self, typ: int, flags: int, ts: int, payload: bytes) -> None:
        if typ == T_LORES:
            self._on_lores(payload, ts, self._lores_clock)

    def _on_info(self, payload: bytes) -> None:
        try:
            info = json.loads(payload)
        except ValueError:
            return
        w, h = info.get("width"), info.get("height")
        if w and h:
            self.output_size = f"{w}x{h}"
        self.codec = info.get("codec")
        fps = info.get("fps")
        self.stream_fps = round(float(fps), 2) if fps else None
        log.info("rear camera: %s %s @ %s fps", self.codec, self.output_size, self.stream_fps or "?")

    def _on_lores(self, payload: bytes, ts: int, clock: Clock) -> None:
        if len(payload) < LORES_HEAD.size or not self.sink.wants_lores():
            return
        w, h = LORES_HEAD.unpack_from(payload)
        data = np.frombuffer(payload, np.uint8, offset=LORES_HEAD.size)
        if data.size != w * h * 3 // 2:
            return
        self.lores_size = (w, h)
        self.sink.on_lores(LoresFrame("yuv420", data.reshape(h * 3 // 2, w), w, h, clock.map(ts)))

    def _on_exit(self, name: str, error: str | None) -> None:
        if self._stop.is_set():
            return
        if name == "sub":  # второй поток — не повод рвать запись: переподключится по требованию
            log.warning("rear camera: second stream stopped: %s", error or "process exited")
            with self._lock:
                self._sub = None
            return
        self.sink.on_error(f"Rear camera: {error or 'the connection was closed'}")

    # ── сколько малых кадров нужно ──
    def _wanted_fps(self) -> float:
        fn = getattr(self.sink, "lores_demand_fps", None)
        try:
            return float(fn()) if fn else float(self.rear.lores.fps)
        except Exception:  # noqa: BLE001
            return 1.0

    def _demand_loop(self) -> None:
        while not self._stop.wait(DEMAND_POLL_S):
            fps = self._wanted_fps()
            if not self.lores_url:
                main = self._main
                if main is not None:
                    main.set_lores(fps)
                continue
            now = self.mono()
            with self._lock:
                if fps > 0:
                    self._sub_used = now
                    if self._sub is None:
                        self._sub = _Helper("sub", self.lores_url,
                                            self.cmd_factory(video=False, transport=self.rear.transport,
                                                             lores_w=self.rear.lores.w),
                                            self._on_sub_msg, self._on_exit)
                        self._sub.start()
                    self._sub.set_lores(fps)
                elif self._sub is not None and now - self._sub_used > SUB_IDLE_S:
                    sub, self._sub = self._sub, None
                    threading.Thread(target=sub.stop, name="rear-sub-stop", daemon=True).start()
