"""Публикация того же H.264-потока в MediaMTX (RTSP push) — всегда в отдельном процессе.

Конвейер → неблокирующий ``push()`` в ограниченную очередь → поток-писатель → MPEG-TS (наш муксер,
метки времени сохраняются) → stdin подпроцесса:
  * ``ffmpeg -f mpegts -i pipe:0 -c copy -f rtsp`` (на устройстве ставится из apt);
  * если ffmpeg нет — ``python -m fnav.camera.rtsp_relay`` (PyAV в отдельном процессе).
Почему не в процессе: падение/зависание libav или отсутствие MediaMTX не должны затронуть регистратор
(PyAV при сетевом I/O держит GIL — проверено: процесс замирает целиком).
При переполнении очереди кадры выбрасываются до следующего ключевого; переподключение — каждые 5 с.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from fnav.paths import REPO_ROOT

from .h264 import AccessUnitFixer
from .tsmux import PTS_START, TsMuxer, pts_from_us

log = logging.getLogger("fnav.camera.rtsp")

DEFAULT_URL = "rtsp://127.0.0.1:8554/cam"
RETRY_S = 5.0
STALL_S = 10.0


def ffmpeg_cmd(url: str) -> list[str]:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    return [exe, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-probesize", "500000", "-analyzeduration", "1000000",
            "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0", "-c", "copy",
            "-f", "rtsp", "-rtsp_transport", "tcp", url]


def relay_cmd(url: str) -> list[str]:
    return [sys.executable, "-m", "fnav.camera.rtsp_relay", url]


def default_cmd(url: str) -> list[str]:
    return ffmpeg_cmd(url) if shutil.which("ffmpeg") else relay_cmd(url)


def _write_all(f, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = f.write(mv)
        if not n:
            raise BrokenPipeError("short write")
        mv = mv[n:]


class RtspPublisher:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        fps: float = 15,
        width: int = 1440,
        height: int = 1080,
        cmd_factory: Callable[[str], list[str]] = default_cmd,
        queue_frames: int = 45,
        retry_s: float = RETRY_S,
    ) -> None:
        self.url = url
        self.fps = fps
        self.width = width
        self.height = height
        self.cmd_factory = cmd_factory
        self.retry_s = retry_s
        self._q: queue.Queue[tuple[bytes, bool, int] | None] = queue.Queue(maxsize=queue_frames)
        self._need_key = True
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._full_since: float | None = None
        self.connected = False
        self.connects = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._last_logged: str | None = None
        self._stderr_tail: str | None = None

    # ── API конвейера (неблокирующий) ──
    def push(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        if self._stop.is_set() or (self._need_key and not keyframe):
            return
        try:
            self._q.put_nowait((au, keyframe, ts_us))
            self._need_key = False
            self._full_since = None
        except queue.Full:
            self.dropped += 1
            self._need_key = True
            now = time.monotonic()
            if self._full_since is None:
                self._full_since = now
            elif now - self._full_since > STALL_S:
                log.warning("rtsp: output stalled for %.0f s, restarting", now - self._full_since)
                self._full_since = None
                self._kill()

    def start(self) -> None:
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="rtsp", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thr:  # поток закроет stdin и даст процессу завершиться штатно
            self._thr.join(4)
        self._kill()
        if self._thr:
            self._thr.join(2)

    # ── внутреннее ──
    def _kill(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _drain_queue(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _get(self, key_only: bool = False) -> tuple[bytes, bool, int] | None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                p = self._proc
                if p is not None and p.poll() is not None:
                    raise RuntimeError(f"publisher exited with code {p.returncode}")
                continue
            if item is None:
                return None
            if key_only and not item[1]:
                continue
            return item
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.last_error = msg
                if msg != self._last_logged:
                    log.warning("rtsp: session ended: %s (retry every %.0f s)", msg, self.retry_s)
                    self._last_logged = msg
                else:
                    log.debug("rtsp: session ended: %s", msg)
            self.connected = False
            self._kill()
            if self._stop.wait(self.retry_s):
                break
            self._drain_queue()
            self._need_key = True

    def _session(self) -> None:
        self._proc = None
        self._stderr_tail = None
        first = self._get(key_only=True)  # каждое подключение начинается с ключевого кадра
        if first is None:
            return
        cmd = self.cmd_factory(self.url)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, bufsize=0, cwd=str(REPO_ROOT))
        self._proc = proc
        threading.Thread(target=self._log_stderr, args=(proc,), daemon=True).start()
        self.connects += 1
        self.connected = True
        log.info("rtsp: publisher started (%s) -> %s", cmd[0], self.url)
        mux = TsMuxer()
        fixer = AccessUnitFixer()
        base = first[2]
        item: tuple[bytes, bool, int] | None = first
        assert proc.stdin is not None
        try:
            while item is not None and not self._stop.is_set():
                au, key = fixer.fix(item[0], item[1])
                _write_all(proc.stdin, mux.mux(au, PTS_START + pts_from_us(item[2] - base), keyframe=key))
                if proc.poll() is not None:
                    raise RuntimeError(f"publisher exited with code {proc.returncode}")
                item = self._get()
        except OSError as e:
            raise RuntimeError(f"publisher pipe closed (code {proc.poll()}): {self._stderr_tail or e}") from e
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
            if self._last_logged is None and proc.returncode == 0:
                log.info("rtsp: publisher finished")

    def _log_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            txt = line.decode(errors="replace").rstrip()
            if txt:
                self._stderr_tail = txt[-300:]
                log.debug("publisher: %s", txt)
