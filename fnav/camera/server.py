"""HTTP API сервиса камеры (127.0.0.1:8081): Starlette + uvicorn. Тексты ответов — на английском."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .live import LiveClient, LiveHub, LiveUnavailable
from .service import CameraService

BOUNDARY = "frame"
NO_CACHE = {"Cache-Control": "no-cache, no-store", "Pragma": "no-cache", "X-Accel-Buffering": "no"}
LIVE_IDLE_S = 15.0  # столько ждём данных живого видео, потом закрываем поток (клиент переподключится)


def fps_param(request: Request) -> float | None:
    """?fps=N (1…30) — прореживание MJPEG; некорректное значение = без ограничения."""
    try:
        v = float(request.query_params.get("fps", ""))
    except ValueError:
        return None
    return min(30.0, max(1.0, v)) if math.isfinite(v) else None


class LiveResponse(StreamingResponse):
    """Поток /live.ts; клиент снимается с хаба при любом завершении — даже если генератор не успел стартовать."""

    def __init__(self, hub: LiveHub, client: LiveClient) -> None:
        self.hub = hub
        self.client = client

        async def gen() -> AsyncIterator[bytes]:
            while True:
                try:
                    chunk = await asyncio.wait_for(client.queue.get(), LIVE_IDLE_S)
                except TimeoutError:
                    return
                yield chunk

        super().__init__(gen(), media_type="video/mp2t", headers=NO_CACHE)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.hub.unsubscribe(self.client)


def create_app(service: CameraService, manage_service: bool = True) -> Starlette:
    hub = service.hub

    async def status(_: Request) -> JSONResponse:
        return JSONResponse(service.status().model_dump())

    async def mjpeg(request: Request) -> StreamingResponse:
        fps = fps_param(request)
        min_dt = 0.9 / fps if fps else 0.0

        async def gen() -> AsyncIterator[bytes]:
            token = object()
            hub.clients += 1
            hub.rates[token] = fps
            try:
                seq = -1
                sent = 0.0
                while True:
                    r = await hub.next_frame(seq, timeout=5.0)
                    if r is None:
                        continue
                    seq, jpg = r
                    if min_dt:
                        now = time.monotonic()
                        if now - sent < min_dt:
                            continue
                        sent = now
                    yield (b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n"
                           + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
            finally:
                hub.clients -= 1
                hub.rates.pop(token, None)

        return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                                 headers=NO_CACHE)

    async def live(_: Request) -> Response:
        try:
            client = await service.live.subscribe()
        except LiveUnavailable as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
        return LiveResponse(service.live, client)

    async def snapshot(_: Request) -> Response:
        hub.last_snapshot_request = time.monotonic()
        jpg = hub.jpeg
        if jpg is None:
            r = await hub.next_frame(-1, timeout=3.0)
            jpg = r[1] if r else None
        if jpg is None:
            return JSONResponse({"ok": False, "error": "No frame available"}, status_code=503)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-cache, no-store"})

    async def protect_current(_: Request) -> JSONResponse:
        seg = await asyncio.to_thread(service.protect_current)
        if seg is None:
            return JSONResponse({"ok": False, "error": "No segment is being recorded"}, status_code=409)
        return JSONResponse({"ok": True, "segment": seg})

    async def reload(_: Request) -> JSONResponse:
        restarted = await asyncio.to_thread(service.reload)
        return JSONResponse({"ok": True, "restarted": restarted})

    async def restart(_: Request) -> JSONResponse:
        service.restart()
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        hub.bind_loop(asyncio.get_running_loop())
        service.live.bind_loop(asyncio.get_running_loop())
        if manage_service:
            service.start()
        try:
            yield
        finally:
            await service.live.close()
            if manage_service:
                await asyncio.to_thread(service.stop)

    return Starlette(
        routes=[
            Route("/status", status, methods=["GET"]),
            Route("/mjpeg", mjpeg, methods=["GET"]),
            Route("/live.ts", live, methods=["GET"]),
            Route("/snapshot.jpg", snapshot, methods=["GET"]),
            Route("/protect-current", protect_current, methods=["POST"]),
            Route("/reload", reload, methods=["POST"]),
            Route("/restart", restart, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
