"""HTTP API сервиса камеры (127.0.0.1:8081): Starlette + uvicorn. Тексты ответов — на английском."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .service import CameraService

BOUNDARY = "frame"


def create_app(service: CameraService, manage_service: bool = True) -> Starlette:
    hub = service.hub

    async def status(_: Request) -> JSONResponse:
        return JSONResponse(service.status().model_dump())

    async def mjpeg(_: Request) -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            hub.clients += 1
            try:
                seq = -1
                while True:
                    r = await hub.next_frame(seq, timeout=5.0)
                    if r is None:
                        continue
                    seq, jpg = r
                    yield (b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n"
                           + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
            finally:
                hub.clients -= 1

        return StreamingResponse(
            gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache", "X-Accel-Buffering": "no"},
        )

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
        if manage_service:
            service.start()
        try:
            yield
        finally:
            if manage_service:
                await asyncio.to_thread(service.stop)

    return Starlette(
        routes=[
            Route("/status", status, methods=["GET"]),
            Route("/mjpeg", mjpeg, methods=["GET"]),
            Route("/snapshot.jpg", snapshot, methods=["GET"]),
            Route("/protect-current", protect_current, methods=["POST"]),
            Route("/reload", reload, methods=["POST"]),
            Route("/restart", restart, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
