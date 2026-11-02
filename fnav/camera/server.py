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
from .mjpeg import BOUNDARY, HISTORY_S, PART_HEAD, mjpeg_part
from .service import CameraService

NO_CACHE = {"Cache-Control": "no-cache, no-store", "Pragma": "no-cache", "X-Accel-Buffering": "no"}
LIVE_IDLE_S = 15.0  # столько ждём данных живого видео, потом закрываем поток (клиент переподключится)
MAX_BEFORE_S = 3600.0  # ?before= — не дальше (камера пропала: охрана просит её последние кадры, video_loss_s + 5)
NO_VIDEO = "No video from the camera"


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
                seq = hub.start_seq()
                sent = 0.0
                yield PART_HEAD
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
                    yield mjpeg_part(jpg)
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

    async def snapshot(request: Request) -> Response:
        """Текущий кадр — только свежий (видео нет — 503, а не стоп-кадр); ?before=N — кадр N секунд назад (охрана:
        что было перед событием; камера пропала — её последние кадры), заголовок X-Frame-Age — сколько ему на самом
        деле секунд. Кадра из того времени нет: N ≤ HISTORY_S — текущий (камера только что запустилась), иначе 404."""
        try:
            before = min(max(float(request.query_params.get("before") or 0), 0.0), MAX_BEFORE_S)
        except ValueError:
            before = 0.0
        if before > 0:
            r = hub.frame_before(before)
            if r is not None:
                return Response(r[1], media_type="image/jpeg",
                                headers={"Cache-Control": "no-cache, no-store", "X-Frame-Age": f"{r[0]:.1f}"})
            if before > HISTORY_S:
                return JSONResponse({"ok": False, "error": "No video from the camera at that moment"}, status_code=404)
        hub.last_snapshot_request = time.monotonic()
        jpg = hub.fresh_jpeg()
        if jpg is None:
            seq = hub.seq if hub.jpeg is not None else -1
            r = await hub.next_frame(seq, timeout=3.0)
            jpg = r[1] if r else None
        if jpg is None:
            return JSONResponse({"ok": False, "error": NO_VIDEO}, status_code=503)
        age = max(0.0, time.monotonic() - hub.jpeg_t)
        return Response(jpg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store", "X-Frame-Age": f"{age:.1f}"})

    async def protect_current(request: Request) -> JSONResponse:
        """Защитить текущий сегмент; ?recent=1 — его нет (камера пропала), тогда последний закрытый (≤ 10 мин)."""
        recent = request.query_params.get("recent") in ("1", "true")
        seg = await asyncio.to_thread(service.protect_current, recent)
        if seg is None:
            return JSONResponse({"ok": False, "error": "No segment is being recorded"}, status_code=409)
        return JSONResponse({"ok": True, "segment": seg})

    async def reload(_: Request) -> JSONResponse:
        restarted = await asyncio.to_thread(service.reload)
        return JSONResponse({"ok": True, "restarted": restarted})

    async def restart(_: Request) -> JSONResponse:
        service.restart()
        return JSONResponse({"ok": True})

    async def motion(request: Request) -> JSONResponse:
        try:
            moving = (await request.json())["moving"]
            if moving is not None and not isinstance(moving, bool):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False, "error": 'expected {"moving": true | false | null}'}, status_code=400)
        await asyncio.to_thread(service.set_moving, moving)
        return JSONResponse({"ok": True, "max_exposure_ms": service.status().max_exposure_ms})

    async def sound(request: Request) -> JSONResponse:
        """Ядро: стоит ли охрана — {"armed": bool}; громкие звуки пишутся клипами только на охране."""
        try:
            armed = (await request.json())["armed"]
            if not isinstance(armed, bool):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False, "error": 'expected {"armed": true | false}'}, status_code=400)
        if service.sound is None:
            return JSONResponse({"ok": False, "error": "No microphone on this camera"}, status_code=409)
        service.set_sound_armed(armed)
        return JSONResponse({"ok": True, "armed": service.sound.armed})

    async def sound_events(request: Request) -> JSONResponse:
        """Громкие звуки новее ?after=N (seq), старые первыми: {seq, start, dur_s, peak_db, floor_db, clip, …}."""
        try:
            after = int(request.query_params.get("after") or 0)
        except ValueError:
            after = 0
        snd = service.sound
        return JSONResponse({"events": snd.events_after(after) if snd else [], "seq": snd.seq if snd else 0})

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
            Route("/motion", motion, methods=["POST"]),
            Route("/sound", sound, methods=["POST"]),
            Route("/sound/events", sound_events, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
