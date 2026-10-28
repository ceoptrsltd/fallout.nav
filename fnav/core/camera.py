"""Клиент/прокси сервиса камеры (fnav-camera, 127.0.0.1:8081; задняя — fnav-camera-rear, 8083) и MediaMTX WHEP
(127.0.0.1:8889, пути cam / rear).

В режиме --mock, если fnav-camera не отвечает, используется встроенная mock-камера.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
from pydantic import ValidationError
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from fnav.core.mockcam import BOUNDARY, MockCamera
from fnav.core.util import cancel_tasks
from fnav.models import CameraId, CameraStatus

log = logging.getLogger("fnav.core.camera")

CAMERA_URL = "http://127.0.0.1:8081"
WHEP_URL = "http://127.0.0.1:8889/cam/whep"
REAR_CAMERA_URL = "http://127.0.0.1:8083"
REAR_WHEP_URL = "http://127.0.0.1:8889/rear/whep"
POLL_S = 1.0
UNAVAILABLE = "Camera service (fnav-camera) is not responding"
REAR_UNAVAILABLE = "Rear camera service (fnav-camera-rear) is not responding"
REAR_OFF = "Rear camera is off (Camera → Rear)"
LIVE_UNAVAILABLE = "Live stream is unavailable"
# едем / стоим — для предела выдержки ночью (на стоянке длиннее: светлее, но меньше кадров/с)
MOVING_MPS = 1.5  # быстрее ~5 км/ч — едем, сразу
PARKED_MPS = 0.5  # медленнее ~2 км/ч…
PARKED_AFTER_S = 10.0  # …дольше 10 с — стоим


class MotionState:
    """Едем / стоим по скорости GPS с гистерезисом. Нет скорости — прежнее решение (вначале None)."""

    def __init__(self) -> None:
        self.moving: bool | None = None
        self._slow_since: float | None = None

    def update(self, speed_mps: float | None, now: float) -> bool | None:
        if speed_mps is None:
            return self.moving
        if speed_mps >= MOVING_MPS:
            self.moving, self._slow_since = True, None
        elif speed_mps < PARKED_MPS:
            if self._slow_since is None:
                self._slow_since = now
            if self.moving is None or now - self._slow_since >= PARKED_AFTER_S:
                self.moving = False
        else:
            self._slow_since = None
        return self.moving


class CameraUnavailable(Exception):
    pass


class CameraService:
    def __init__(
        self,
        *,
        mock: bool,
        recordings_root: Path,
        base_url: str = CAMERA_URL,
        whep_url: str = WHEP_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        get_speed: Callable[[], float | None] | None = None,
        camera_id: CameraId = "front",
        get_enabled: Callable[[], bool] | None = None,
    ):
        self.mock = mock
        self.camera_id: CameraId = camera_id
        self.get_enabled = get_enabled or (lambda: True)
        self.base_url = base_url.rstrip("/")
        self.whep_url = whep_url.rstrip("/")
        self.unavailable_text = REAR_UNAVAILABLE if camera_id == "rear" else UNAVAILABLE
        self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2.0, connect=1.0))
        self.available: bool | None = None
        self.status: CameraStatus = CameraStatus(state="starting" if not mock else "stopped", camera=camera_id)
        self.mockcam = MockCamera(recordings_root, camera=camera_id) if mock else None
        self._listeners: list = []
        self._task: asyncio.Task | None = None
        self.get_speed = get_speed
        self.motion = MotionState()

    @property
    def using_mock(self) -> bool:
        return self.mockcam is not None and not self.available

    @property
    def enabled(self) -> bool:
        return bool(self.get_enabled())

    def _check_enabled(self) -> None:
        if not self.enabled:
            raise CameraUnavailable(REAR_OFF)

    def on_status(self, cb) -> None:
        self._listeners.append(cb)

    async def start(self, *, background: bool = True) -> None:
        if self.mockcam is not None and self.enabled:  # задняя выключена — демо-записей ей не нужно
            await asyncio.to_thread(self.mockcam.ensure_demo_recordings)
        await self.poll()
        if background:
            self._task = asyncio.create_task(self._loop(), name="camera-poll")

    async def stop(self) -> None:
        await cancel_tasks(self._task)
        await self._client.aclose()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await self.poll()
            except Exception:
                log.exception("camera poll")

    async def poll(self) -> CameraStatus:
        prev_avail = self.available
        if not self.enabled:  # задняя выключена в настройках: сервис не спрашиваем
            self.available = None
            return self._set_status(CameraStatus(state="disabled", camera=self.camera_id))
        try:
            r = await self._client.get(f"{self.base_url}/status", timeout=1.0)
            r.raise_for_status()
            st = CameraStatus.model_validate(r.json())
            self.available = True
        except (httpx.HTTPError, ValueError, ValidationError) as e:
            self.available = False
            if self.mockcam is not None:
                st = self.mockcam.status()
            else:
                st = CameraStatus(state="error", camera=self.camera_id, recording=False,
                                  error=self.unavailable_text)
            if prev_avail is not False:
                log.info("camera %s: %s (%s)%s", self.camera_id, self.unavailable_text, type(e).__name__,
                         " - using mock camera" if self.mockcam is not None else "")
        if self.available and prev_avail is not True:
            log.info("camera %s: available (%s)", self.camera_id, self.base_url)
        if self.available and self.get_speed is not None:
            moving = self.motion.update(self.get_speed(), time.monotonic())
            if moving != st.moving and await self._send_motion(moving):
                st = st.model_copy(update={"moving": moving})
        return self._set_status(st)

    def _set_status(self, st: CameraStatus) -> CameraStatus:
        old = self.status
        self.status = st
        if old != st:
            for cb in list(self._listeners):
                try:
                    cb(st)
                except Exception:
                    log.exception("camera status listener")
        return st

    # ── управление ──
    async def _send_motion(self, moving: bool | None) -> bool:
        """Сообщить камере «едем/стоим». Не вышло — повтор при следующем опросе (камера сообщает своё в /status)."""
        try:
            r = await self._client.post(f"{self.base_url}/motion", json={"moving": moving}, timeout=2.0)
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            log.warning("camera: cannot send motion state (%s)", type(e).__name__)
            return False

    async def reload(self) -> None:
        """Уведомить камеру о новых настройках. Ошибки игнорируются."""
        try:
            await self._client.post(f"{self.base_url}/reload", timeout=2.0)
        except httpx.HTTPError:
            pass

    async def restart(self) -> dict:
        self._check_enabled()
        try:
            r = await self._client.post(f"{self.base_url}/restart", timeout=3.0)
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": True}
        except (httpx.HTTPError, ValueError):
            if self.mockcam is not None:
                return {"ok": True, "mock": True}
            raise CameraUnavailable(self.unavailable_text) from None

    async def protect_current(self) -> dict:
        self._check_enabled()
        try:
            r = await self._client.post(f"{self.base_url}/protect-current", timeout=3.0)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if self.mockcam is not None and not self.available:
                return self.mockcam.protect_current()
            raise CameraUnavailable(self.unavailable_text) from None

    # ── видео ──
    async def open_mjpeg(self, fps: int | None = None) -> tuple[str, AsyncIterator[bytes]]:
        """→ (content-type, поток байт). Камеры нет → CameraUnavailable (или mock). fps — прореживание."""
        self._check_enabled()
        params = {"fps": str(fps)} if fps else None
        if self.available is not False:
            try:
                req = self._client.build_request("GET", f"{self.base_url}/mjpeg", params=params,
                                                 timeout=httpx.Timeout(10.0, connect=1.0))
                resp = await self._client.send(req, stream=True)
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")

                    async def gen() -> AsyncIterator[bytes]:
                        try:
                            async for chunk in resp.aiter_raw():
                                yield chunk
                        except httpx.HTTPError:
                            pass
                        finally:
                            await resp.aclose()

                    return ct, gen()
                await resp.aclose()
            except httpx.HTTPError:
                pass
        if self.mockcam is not None:
            return f"multipart/x-mixed-replace; boundary={BOUNDARY}", self.mockcam.mjpeg(fps)
        raise CameraUnavailable(self.unavailable_text)

    async def open_live(self) -> tuple[AsyncIterator[bytes], Callable[[], Awaitable[None]]]:
        """Живое видео MPEG-TS (второй энкодер камеры, по требованию) → (поток, закрыть)."""
        self._check_enabled()
        if self.available is not False:
            try:
                req = self._client.build_request("GET", f"{self.base_url}/live.ts",
                                                 timeout=httpx.Timeout(30.0, connect=1.0))
                resp = await self._client.send(req, stream=True)
            except httpx.HTTPError:
                resp = None
            if resp is not None:
                if resp.status_code == 200:
                    async def gen() -> AsyncIterator[bytes]:
                        try:
                            async for chunk in resp.aiter_raw():
                                yield chunk
                        except httpx.HTTPError:
                            pass

                    return gen(), resp.aclose
                detail = LIVE_UNAVAILABLE
                try:
                    await resp.aread()
                    detail = resp.json().get("error") or detail
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
                finally:
                    await resp.aclose()
                raise CameraUnavailable(detail)
        if self.mockcam is not None and self.mockcam.live_available():
            stream = self.mockcam.live_ts()
            return stream, stream.aclose
        raise CameraUnavailable(self.unavailable_text if self.mockcam is None else LIVE_UNAVAILABLE)

    async def snapshot(self) -> bytes:
        self._check_enabled()
        if self.available is not False:
            try:
                r = await self._client.get(f"{self.base_url}/snapshot.jpg", timeout=3.0)
                if r.status_code == 200 and r.content:
                    return r.content
            except httpx.HTTPError:
                pass
        if self.mockcam is not None:
            return await self.mockcam.snapshot()
        raise CameraUnavailable(self.unavailable_text)

    async def whep(self, method: str, subpath: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        self._check_enabled()
        url = self.whep_url + (f"/{subpath}" if subpath else "")
        fwd = {k: v for k, v in headers.items() if k.lower() in ("content-type", "if-match", "accept")}
        try:
            return await self._client.request(method, url, content=body or None, headers=fwd, timeout=5.0)
        except httpx.HTTPError:
            raise CameraUnavailable("MediaMTX (WebRTC) is unavailable") from None


def rewrite_whep_location(loc: str | None, whep_url: str = WHEP_URL, api_base: str = "/api/camera/whep") -> str | None:
    """`/cam/whep/<id>` (или абсолютный URL MediaMTX) → `/api/camera/whep/<id>` (задняя: `/rear/whep/<id>` →
    `/api/camera/rear/whep/<id>`)."""
    if not loc:
        return loc
    from urllib.parse import urlsplit

    base_path = urlsplit(whep_url).path.rstrip("/")  # /cam/whep
    path = urlsplit(loc).path if "://" in loc else loc
    if path.startswith(base_path):
        rest = path[len(base_path):].lstrip("/")
        return api_base + (f"/{rest}" if rest else "")
    return loc


class ClosingStreamingResponse(StreamingResponse):
    """StreamingResponse, который гарантированно вызывает on_close (обрыв клиента до первого байта — тоже):
    иначе соединение с камерой осталось бы открытым и второй энкодер не выключился бы."""

    def __init__(self, content: AsyncIterator[bytes], on_close: Callable[[], Awaitable[None]], **kw) -> None:
        super().__init__(content, **kw)
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self._on_close()
            except Exception:
                log.debug("stream close failed", exc_info=True)
