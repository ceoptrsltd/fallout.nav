"""HTTP API fnav-watch (127.0.0.1:8082): Starlette + uvicorn, как у fnav-camera. Тексты ответов — на английском."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .service import WatchService

NO_CACHE = {"Cache-Control": "no-cache, no-store"}


def create_app(service: WatchService, manage_service: bool = True) -> Starlette:
    async def status(_: Request) -> JSONResponse:
        return JSONResponse(service.status())

    async def arm(request: Request) -> JSONResponse:
        try:
            watching = (await request.json())["watching"]
            if not isinstance(watching, bool):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False, "error": 'expected {"watching": true | false}'}, status_code=400)
        await asyncio.to_thread(service.set_watching, watching)
        return JSONResponse({"watching": service.watching})

    async def reload(_: Request) -> JSONResponse:
        s = await asyncio.to_thread(service.reload)
        return JSONResponse({"ok": True, "settings": s.model_dump()})

    async def debug_jpg(_: Request) -> Response:
        jpg = await asyncio.to_thread(service.debug_jpeg)
        if jpg is None:
            return JSONResponse({"ok": False, "error": "No processed frame yet (not watching?)"}, status_code=503)
        return Response(jpg, media_type="image/jpeg", headers=NO_CACHE)

    async def debug_json(_: Request) -> JSONResponse:
        return JSONResponse(service.debug_info(), headers=NO_CACHE)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
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
            Route("/arm", arm, methods=["POST"]),
            Route("/reload", reload, methods=["POST"]),
            Route("/debug.jpg", debug_jpg, methods=["GET"]),
            Route("/debug.json", debug_json, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
