"""Клиент/прокси сервиса камеры (fnav-camera, 127.0.0.1:8081) и MediaMTX WHEP (127.0.0.1:8889).

В режиме --mock, если fnav-camera не отвечает, используется встроенная mock-камера.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
from pydantic import ValidationError
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from fnav.core.mockcam import BOUNDARY, MockCamera
from fnav.core.util import cancel_tasks
from fnav.models import CameraStatus

log = logging.getLogger("fnav.core.camera")

CAMERA_URL = "http://127.0.0.1:8081"
WHEP_URL = "http://127.0.0.1:8889/cam/whep"
POLL_S = 1.0
UNAVAILABLE = "Camera service (fnav-camera) is not responding"
LIVE_UNAVAILABLE = "Live stream is unavailable"


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
    ):
        self.mock = mock
        self.base_url = base_url.rstrip("/")
        self.whep_url = whep_url.rstrip("/")
        self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2.0, connect=1.0))
        self.available: bool | None = None
        self.status: CameraStatus = CameraStatus(state="starting" if not mock else "stopped")
        self.mockcam = MockCamera(recordings_root) if mock else None
        self._listeners: list = []
        self._task: asyncio.Task | None = None

    @property
    def using_mock(self) -> bool:
        return self.mockcam is not None and not self.available

    def on_status(self, cb) -> None:
        self._listeners.append(cb)

    async def start(self, *, background: bool = True) -> None:
        if self.mockcam is not None:
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
                st = CameraStatus(state="error", recording=False, error=UNAVAILABLE)
            if prev_avail is not False:
                log.info("camera: %s (%s)%s", UNAVAILABLE, type(e).__name__,
                         " - using mock camera" if self.mockcam is not None else "")
        if self.available and prev_avail is not True:
            log.info("camera: fnav-camera is available (%s)", self.base_url)
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
    async def reload(self) -> None:
        """Уведомить камеру о новых настройках. Ошибки игнорируются."""
        try:
            await self._client.post(f"{self.base_url}/reload", timeout=2.0)
        except httpx.HTTPError:
            pass

    async def restart(self) -> dict:
        try:
            r = await self._client.post(f"{self.base_url}/restart", timeout=3.0)
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": True}
        except (httpx.HTTPError, ValueError):
            if self.mockcam is not None:
                return {"ok": True, "mock": True}
            raise CameraUnavailable(UNAVAILABLE) from None

    async def protect_current(self) -> dict:
        try:
            r = await self._client.post(f"{self.base_url}/protect-current", timeout=3.0)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            if self.mockcam is not None and not self.available:
                return self.mockcam.protect_current()
            raise CameraUnavailable(UNAVAILABLE) from None

    # ── видео ──
    async def open_mjpeg(self, fps: int | None = None) -> tuple[str, AsyncIterator[bytes]]:
        """→ (content-type, поток байт). Камеры нет → CameraUnavailable (или mock). fps — прореживание."""
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
        raise CameraUnavailable(UNAVAILABLE)

    async def open_live(self) -> tuple[AsyncIterator[bytes], Callable[[], Awaitable[None]]]:
        """Живое видео MPEG-TS (второй энкодер камеры, по требованию) → (поток, закрыть)."""
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
        raise CameraUnavailable(UNAVAILABLE if self.mockcam is None else LIVE_UNAVAILABLE)

    async def snapshot(self) -> bytes:
        if self.available is not False:
            try:
                r = await self._client.get(f"{self.base_url}/snapshot.jpg", timeout=3.0)
                if r.status_code == 200 and r.content:
                    return r.content
            except httpx.HTTPError:
                pass
        if self.mockcam is not None:
            return await self.mockcam.snapshot()
        raise CameraUnavailable(UNAVAILABLE)

    async def whep(self, method: str, subpath: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        url = self.whep_url + (f"/{subpath}" if subpath else "")
        fwd = {k: v for k, v in headers.items() if k.lower() in ("content-type", "if-match", "accept")}
        try:
            return await self._client.request(method, url, content=body or None, headers=fwd, timeout=5.0)
        except httpx.HTTPError:
            raise CameraUnavailable("MediaMTX (WebRTC) is unavailable") from None


def rewrite_whep_location(loc: str | None, whep_url: str = WHEP_URL) -> str | None:
    """`/cam/whep/<id>` (или абсолютный URL MediaMTX) → `/api/camera/whep/<id>`."""
    if not loc:
        return loc
    from urllib.parse import urlsplit

    base_path = urlsplit(whep_url).path.rstrip("/")  # /cam/whep
    path = urlsplit(loc).path if "://" in loc else loc
    if path.startswith(base_path):
        rest = path[len(base_path):].lstrip("/")
        return "/api/camera/whep" + (f"/{rest}" if rest else "")
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
