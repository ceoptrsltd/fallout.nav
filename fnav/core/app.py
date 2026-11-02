"""FastAPI-приложение fnav-core: REST, WebSocket, тайлы, статика (контракт: docs/contracts/api.md)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from fnav import __version__, paths, tz
from fnav.core import secretsfile as sf
from fnav.core.auth import COOKIE_NAME, SESSION_TTL_S, AuthMiddleware
from fnav.core.bluetooth import BluetoothError
from fnav.core.camera import CameraUnavailable, ClosingStreamingResponse, rewrite_whep_location
from fnav.core.config import SettingsError
from fnav.core.controls import controls_info
from fnav.core.games import GamesError
from fnav.core.level import LevelError
from fnav.core.marks import CLIP_RE, CLIPS_DIR
from fnav.core.radio import GENRES, RadioError
from fnav.core.recordings import (
    RecordingError,
    attach_events,
    attach_marks,
    iter_file,
    parse_range,
    recording_at,
)
from fnav.core.runtime import REMOTE_TELEMETRY_EVERY, Core, CoreOptions
from fnav.core.system import SystemError_
from fnav.core.tiles import TileError
from fnav.models import RadioStation, Settings

log = logging.getLogger("fnav.core.app")


# ───────────────────────── тела запросов ─────────────────────────


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class ForceBody(BaseModel):
    mode: Literal["auto", "map", "camera"]


class ProtectBody(BaseModel):
    path: str
    protected: bool = True


class WifiBody(BaseModel):
    ssid: str
    password: str = ""
    priority: int = 0


class PasswordBody(BaseModel):
    old: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


class ButtonBody(BaseModel):
    event: Literal["press", "long_press"] = "press"


class RearCredentialsBody(BaseModel):
    username: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=256)  # "" — удалить; None — не менять


class CameraViewBody(BaseModel):
    camera: Literal["front", "rear"] | None = None  # None — переключить


class NoCacheStatic(StaticFiles):
    """Статика других агентов меняется часто — браузер всегда перепроверяет (ETag/Last-Modified).
    Метка версии админки (version.json, пишет деплой: tools/web-version.sh) не кэшируется вовсе."""

    NO_STORE = frozenset({"version.json"})

    def file_response(self, full_path, *args, **kwargs) -> Response:  # type: ignore[override]
        resp = super().file_response(full_path, *args, **kwargs)
        no_store = os.path.basename(full_path) in self.NO_STORE
        resp.headers["Cache-Control"] = "no-store" if no_store else "no-cache"
        return resp


def _err(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


async def _json_body(request: Request):
    try:
        return await request.json()
    except ValueError:
        raise SettingsError("Invalid JSON in request body") from None


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True, samesite="lax", path="/")


# ───────────────────────── приложение ─────────────────────────


def create_app(opts: CoreOptions | None = None) -> FastAPI:
    opts = opts or CoreOptions()
    core = Core(opts)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await core.start()
        try:
            yield
        finally:
            await core.stop()

    app = FastAPI(title="fnav-core", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")
    app.state.core = core
    app.add_middleware(AuthMiddleware, get_auth=lambda: core.auth)

    # ── ошибки → {"detail": "по-русски"} ──
    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, e: RequestValidationError):
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return _err(422, "Invalid request: " + "; ".join(parts))

    @app.exception_handler(SettingsError)
    async def _settings_err(_r: Request, e: SettingsError):
        status = 400 if e.detail.startswith("Invalid JSON") else 422
        return JSONResponse({"detail": e.detail, "errors": e.errors}, status_code=status)

    @app.exception_handler(RecordingError)
    async def _rec_err(_r: Request, e: RecordingError):
        return _err(e.status, e.detail)

    @app.exception_handler(SystemError_)
    async def _sys_err(_r: Request, e: SystemError_):
        return _err(e.status, e.detail)

    @app.exception_handler(CameraUnavailable)
    async def _cam_err(_r: Request, e: CameraUnavailable):
        return _err(503, str(e))

    _register_auth(app, core)
    _register_settings(app, core)
    _register_ws(app, core)
    _register_tiles(app, core)
    _register_recordings(app, core)
    _register_camera(app, core)
    _register_sensors(app, core)
    _register_system(app, core)
    _register_tracking(app, core)
    _register_radio(app, core)
    _register_bluetooth(app, core)
    _register_games(app, core)
    _register_alarm(app, core)
    _register_level(app, core)
    _register_static(app)
    return app


# ───────────────────────── авторизация ─────────────────────────


def _register_auth(app: FastAPI, core: Core) -> None:
    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        if not core.auth.password_hash:
            return _err(503, "No password is set on the device: run python -m fnav.core.passwd")
        ip = core.auth.client_ip(request)
        ok, wait = await core.auth.try_password(ip, body.password)
        if wait > 0:
            return _err(429, f"Too many attempts. Wait {int(wait) + 1} s",
                        headers={"Retry-After": str(int(wait) + 1)})
        if not ok:
            log.warning("failed login from %s", ip)
            return _err(401, "Wrong password")
        resp = JSONResponse({"ok": True})
        _set_session_cookie(resp, core.auth.issue_token())
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/session")
    async def session(request: Request):
        authed = core.auth.is_authenticated(request)
        out = {"authenticated": authed, "local": core.auth.is_local(request),
               "remote": core.auth.is_remote(request), "mock": core.mock}
        if authed:  # пояс устройства (≈ страна, где машина) — только своим; админка переводит по нему время записей
            out["timezone"] = tz.zone_name()
        return out

    @app.post("/api/system/password")
    async def change_password(body: PasswordBody, request: Request):
        ok, wait = await core.auth.try_password(core.auth.client_ip(request), body.old)
        if wait > 0:
            return _err(429, f"Too many attempts. Wait {int(wait) + 1} s",
                        headers={"Retry-After": str(int(wait) + 1)})
        if not ok:
            return _err(400, "Current password is wrong")
        if len(body.new) < sf.MIN_PASSWORD_LEN:
            return _err(400, f"New password must be at least {sf.MIN_PASSWORD_LEN} characters")
        try:
            await core.auth.change_password(body.new)
        except (OSError, ValueError) as e:
            return _err(500, f"Failed to save password: {e}")
        core.note_secrets_written()
        resp = JSONResponse({"ok": True})
        _set_session_cookie(resp, core.auth.issue_token())  # старые сессии больше не действуют
        return resp


# ───────────────────────── настройки и экран ─────────────────────────


def _register_settings(app: FastAPI, core: Core) -> None:
    def dump(s: Settings) -> dict:
        return s.model_dump(mode="json")

    @app.get("/api/settings")
    async def get_settings():
        return dump(core.store.get())

    @app.get("/api/settings/defaults")
    async def get_defaults():
        return dump(Settings())

    @app.patch("/api/settings")
    async def patch_settings(request: Request):
        return dump(await core.store.patch(await _json_body(request)))

    @app.put("/api/settings")
    async def put_settings(request: Request):
        return dump(await core.store.replace(await _json_body(request)))

    @app.post("/api/settings/reset")
    async def reset_settings():
        return dump(await core.store.reset())

    @app.post("/api/display/force")
    async def display_force(body: ForceBody):
        s = await core.store.patch({"display": {"force_mode": body.mode}})
        return {"ok": True, "force_mode": s.display.force_mode}

    @app.get("/api/display/output")
    async def video_output_get():
        try:
            name, on = await core.video.state()
        except Exception as e:  # noqa: BLE001
            return _err(503, f"Video output: {e}")
        return {"output": name, "on": on}

    @app.post("/api/display/output")
    async def video_output_set(body: dict | None = None):
        on = None if not body or body.get("on") is None else bool(body.get("on"))
        res = await core.toggle_video_output(on)
        if res is None:
            return _err(503, "Video output could not be switched")
        return res

    @app.get("/api/telemetry")
    async def telemetry():
        return core.build_telemetry().model_dump(mode="json")


# ───────────────────────── WebSocket ─────────────────────────


def _register_ws(app: FastAPI, core: Core) -> None:
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        hello = [core.settings_message(), core.build_telemetry().model_dump_json()]
        if (weather := core.weather.status()) is not None:
            hello.append(weather.model_dump_json())
        await core.ws.serve(ws, hello, REMOTE_TELEMETRY_EVERY if core.auth.is_remote(ws) else 1)


# ───────────────────────── тайлы ─────────────────────────


async def _client_gone(request: Request) -> None:
    """Вернуться, когда клиент закроет соединение (для GET без тела — других сообщений нет)."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _unless_client_gone(request: Request, aw):
    """Дождаться aw; если клиент ушёл раньше — отменить и вернуть None.

    Сам Starlette обработчик при обрыве не отменяет: брошенный экраном тайл качался бы до конца
    и держал соединение пула Esri (TileService.get отпускает загрузку, когда её никто не ждёт)."""
    job = asyncio.ensure_future(aw)
    gone = asyncio.ensure_future(_client_gone(request))
    try:
        await asyncio.wait((job, gone), return_when=asyncio.FIRST_COMPLETED)
    finally:
        gone.cancel()
        if not job.done():
            job.cancel()
    return job.result() if job.done() else None


def _register_tiles(app: FastAPI, core: Core) -> None:
    @app.get("/tiles/{layer}/{z}/{x}/{y}")
    async def tile(request: Request, layer: str, z: str, x: str, y: str):
        try:
            zi, xi, yi = int(z), int(x), int(y.split(".", 1)[0])
        except ValueError:
            return _err(400, "Invalid tile coordinates")
        try:
            t = await _unless_client_gone(request, core.tiles.get(layer, zi, xi, yi))
        except TileError as e:
            headers = {"Cache-Control": "no-store"}
            if e.retry_after:
                headers["Retry-After"] = str(e.retry_after)
            return JSONResponse({"detail": e.detail}, status_code=e.status, headers=headers)
        if t is None:
            return Response(status_code=499, headers={"Cache-Control": "no-store"})  # ответ никто не прочтёт
        return Response(t.data, media_type=t.content_type,
                        headers={"Cache-Control": "max-age=86400", "X-Cache": "HIT" if t.cached else "MISS"})

    @app.get("/api/tiles/stats")
    async def tile_stats():
        return core.tiles.stats()

    @app.delete("/api/tiles")
    async def tile_clear():
        await core.tiles.clear()
        return {"ok": True}


# ───────────────────────── записи ─────────────────────────


def _register_recordings(app: FastAPI, core: Core) -> None:
    @app.get("/api/recordings")
    async def rec_list():
        """Записи + метки событий охраны в них (переход к моменту срабатывания) и отметки движения без тревоги."""
        items = attach_events(await core.recordings.list(core.current_segment()), core.alarm.events)
        return attach_marks(items, core.marks.between)

    @app.get("/api/recordings/item/{path:path}")
    async def rec_item(path: str):
        """Одна запись с метками событий и отметками — плееру со страницы охраны (кнопка Play у события)."""
        items = attach_events([await core.recordings.item(path, core.current_segment())], core.alarm.events)
        return attach_marks(items, core.marks.between)[0]

    @app.api_route("/api/recordings/file/{path:path}", methods=["GET", "HEAD"])
    async def rec_file(path: str, request: Request, download: int = 0):
        f = core.recordings.file(path)
        size = f.stat().st_size
        rng = parse_range(request.headers.get("range"), size) if size else None
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
        if download:
            headers["Content-Disposition"] = f"attachment; filename=\"{f.name}\"; filename*=UTF-8''{quote(f.name)}"
        if rng:
            start, end = rng
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        else:
            start, end, status = 0, size - 1, 200
        length = end - start + 1 if size else 0
        headers["Content-Length"] = str(length)
        if request.method == "HEAD" or length == 0:
            return Response(b"", status_code=status, headers=headers, media_type="video/mp2t")
        return StreamingResponse(iter_file(f, start, length), status_code=status, headers=headers,
                                 media_type="video/mp2t")

    # HLS по записи: куски EXT-X-BYTERANGE от ключевого кадра → перемотка в любое место без докачки
    @app.get("/api/recordings/hls/{path:path}")
    async def rec_hls(path: str):
        if not path.endswith(".m3u8"):
            raise RecordingError(404, "Not found")
        text = await core.recordings.hls(path[:-5], core.current_segment())
        return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"})

    @app.delete("/api/recordings/file/{path:path}")
    async def rec_delete(path: str, force: int = 0):
        await core.recordings.delete(path, current=core.current_segment(), force=bool(force))
        return {"ok": True}

    @app.post("/api/recordings/protect")
    async def rec_protect(body: ProtectBody):
        p = await core.recordings.protect(body.path, body.protected)
        return {"ok": True, "path": body.path, "protected": p}

    @app.post("/api/recordings/protect-current")
    async def rec_protect_current():
        return await core.protect_current()


# ───────────────────────── камера ─────────────────────────


def _register_camera(app: FastAPI, core: Core) -> None:
    """Одни и те же маршруты для передней (/api/camera/…) и задней (/api/camera/rear/…) камеры."""
    _register_one_camera(app, core, "/api/camera", lambda: core.camera)
    _register_one_camera(app, core, "/api/camera/rear", lambda: core.camera_rear)

    @app.post("/api/camera/rear/credentials")
    async def rear_credentials(body: RearCredentialsBody):
        """Логин и пароль RTSP задней камеры: пароль — в secrets.env, наружу не отдаётся (только password_set)."""
        return await core.set_rear_credentials(body.username, body.password)

    @app.get("/api/camera/view")
    async def camera_view_get():
        return {"camera": core.view_camera, "rear_enabled": core.camera_rear.enabled}

    @app.post("/api/camera/view")
    async def camera_view_set(body: CameraViewBody):
        """Экран в машине: передняя ↔ задняя камера (как кнопка «камеры»)."""
        if body.camera is None or body.camera != core.view_camera:
            res = await core.switch_camera()
            if res and not res.get("ok"):
                return _err(409, res.get("error") or "Rear camera is off")
        return {"camera": core.view_camera, "rear_enabled": core.camera_rear.enabled}


def _register_one_camera(app: FastAPI, core: Core, prefix: str, cam_of) -> None:
    whep_api = prefix + "/whep"

    @app.get(prefix + "/status")
    async def cam_status():
        cam = cam_of()
        st = await cam.poll()
        return {**st.model_dump(mode="json"), "mock": cam.using_mock, "enabled": cam.enabled}

    @app.get(prefix + "/mjpeg")
    async def cam_mjpeg(request: Request):
        try:  # ?fps=N (1…30) — прореживание (удалённый просмотр), иначе полная частота
            fps = min(30, max(1, int(request.query_params["fps"])))
        except (KeyError, ValueError):
            fps = None
        ct, stream = await cam_of().open_mjpeg(fps)
        return StreamingResponse(stream, media_type=ct,
                                 headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})

    @app.get(prefix + "/live.ts")
    async def cam_live():
        # живое видео по требованию: второй энкодер камеры работает, только пока этот поток открыт
        stream, close = await cam_of().open_live()
        return ClosingStreamingResponse(stream, close, media_type="video/mp2t",
                                        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})

    @app.get(prefix + "/snapshot.jpg")
    async def cam_snapshot():
        return Response(await cam_of().snapshot(), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post(prefix + "/restart")
    async def cam_restart():
        return await cam_of().restart()

    async def _whep(request: Request, subpath: str) -> Response:
        cam = cam_of()
        body = await request.body()
        r = await cam.whep(request.method, subpath, body, dict(request.headers))
        headers = {}
        for h in ("Location", "ETag", "Link", "Accept-Patch", "Content-Type", "Access-Control-Expose-Headers"):
            v = r.headers.get(h)
            if v is not None:
                headers[h] = rewrite_whep_location(v, cam.whep_url, whep_api) if h == "Location" else v
        if "link" in r.headers:
            headers["Link"] = ", ".join(r.headers.get_list("link"))
        return Response(r.content, status_code=r.status_code, headers=headers)

    @app.api_route(whep_api, methods=["POST", "OPTIONS"])
    async def whep_root(request: Request):
        return await _whep(request, "")

    @app.api_route(whep_api + "/{subpath:path}", methods=["PATCH", "DELETE", "GET", "OPTIONS"])
    async def whep_sub(subpath: str, request: Request):
        if ".." in subpath.split("/"):
            return _err(400, "Invalid path")
        return await _whep(request, subpath)


# ───────────────────────── датчики ─────────────────────────


def _register_sensors(app: FastAPI, core: Core) -> None:
    @app.get("/api/gps/satellites")
    async def satellites():
        return [s.model_dump(mode="json") for s in core.hub.snapshot().satellites]

    @app.get("/api/gps/nmea")
    async def nmea(n: int = 50):
        return {"lines": core.hub.nmea_tail(max(1, min(n, 500)))}

    @app.post("/api/gps/configure")
    async def gps_configure():
        return await core.hub.gps_configure()

    @app.get("/api/compass/calibration")
    async def calib_status():
        return core.hub.calibration_status().model_dump(mode="json")

    @app.post("/api/compass/calibration/start")
    async def calib_start():
        core.hub.calibration_start()
        return core.hub.calibration_status().model_dump(mode="json")

    @app.post("/api/compass/calibration/cancel")
    async def calib_cancel():
        core.hub.calibration_cancel()
        return core.hub.calibration_status().model_dump(mode="json")

    @app.post("/api/compass/calibration/reset-auto")
    async def calib_reset_auto():
        core.hub.auto_calibration_reset()
        return {"ok": True}

    @app.post("/api/compass/calibration/finish")
    async def calib_finish():
        try:
            cal = core.hub.calibration_finish()
        except Exception as e:  # noqa: BLE001
            return _err(409, f"Calibration failed: {e}")
        await core.store.patch({"compass": {"calibration": cal.model_dump(mode="json")}})
        return cal.model_dump(mode="json")

    if core.mock:
        @app.post("/api/mock/button")
        async def mock_button(body: ButtonBody):
            """Только --mock: имитация кнопки на GPS-модуле."""
            res = await core.handle_button(body.event)
            return {"ok": True, "result": res}


# ───────────────────────── система ─────────────────────────


def _register_system(app: FastAPI, core: Core) -> None:
    @app.get("/api/system/status")
    async def sys_status():
        st = await core.system.refresh(internet=core.tiles.internet)
        return {
            "system": st.model_dump(mode="json"),
            "disk": core.system.disk.model_dump(mode="json"),
            "services": await core.system.services(),
            "version": __version__,
            "mock": core.mock,
        }

    @app.get("/api/system/logs")
    async def sys_logs(unit: str = "fnav-core", lines: int = 200):
        return {"lines": await core.system.logs(unit, lines)}

    @app.post("/api/system/reboot")
    async def sys_reboot():
        log.warning("reboot requested")
        return await core.system.power("reboot")

    @app.post("/api/system/shutdown")
    async def sys_shutdown():
        log.warning("shutdown requested")
        return await core.system.power("shutdown")

    @app.get("/api/system/wifi")
    async def wifi_list():
        return await core.system.wifi_list()

    @app.post("/api/system/wifi")
    async def wifi_add(body: WifiBody):
        return await core.system.wifi_add(body.ssid, body.password, body.priority)

    @app.delete("/api/system/wifi/{ssid}")
    async def wifi_delete(ssid: str):
        return await core.system.wifi_delete(ssid)


# ───────────────────────── журнал позиций, удалённый доступ ─────────────────────────


def _register_tracking(app: FastAPI, core: Core) -> None:
    @app.get("/api/remote/status")
    async def remote_status(request: Request):
        return {**await core.tracking.status(), "remote_request": core.auth.is_remote(request)}

    @app.get("/api/tracking/{path:path}")
    async def tracking_proxy(path: str, request: Request):
        # журнал хранится на сервере (fnav-relay); удалённо nginx отдаёт /api/tracking/* сам, без устройства
        if ".." in path.split("/"):
            return _err(400, "Invalid path")
        status, body, ctype = await core.tracking.proxy_get(path, request.url.query)
        return Response(body, status_code=status, media_type=ctype, headers={"Cache-Control": "no-store"})


# ───────────────────────── радио ─────────────────────────


class PlayBody(BaseModel):
    station: RadioStation
    genre: str | None = Field(None, max_length=40)


class VolumeBody(BaseModel):
    volume: int = Field(..., ge=0, le=100)


class StepBody(BaseModel):
    delta: Literal[-1, 1]


class StationBody(BaseModel):
    station: RadioStation


def _register_radio(app: FastAPI, core: Core) -> None:
    @app.exception_handler(RadioError)
    async def _radio_err(_r: Request, e: RadioError):
        return _err(e.status, e.detail)

    @app.get("/api/radio/genres")
    async def radio_genres():
        return {"genres": GENRES}

    @app.get("/api/radio/stations")
    async def radio_stations(tag: str | None = None, q: str | None = None, top: str | None = None):
        given = [x for x in (tag, q, top) if x]
        if len(given) != 1 or len(given[0]) > 100:
            return _err(400, "Give exactly one of: tag, q, top")
        d = core.radio.directory
        if tag:
            res = await d.by_tag(tag.strip().lower())
        elif q:
            res = await d.by_name(q.strip())
        else:
            res = await d.top(None if top.strip().lower() == "world" else top)
        if res is None:
            return _err(503, "Radio directory is unreachable")
        return {"stations": [st.model_dump() for st in res]}

    @app.get("/api/radio/state")
    async def radio_state():
        return core.radio.state().model_dump()

    @app.post("/api/radio/play")
    async def radio_play(body: PlayBody):
        return (await core.radio.play(body.station, body.genre)).model_dump()

    @app.post("/api/radio/stop")
    async def radio_stop():
        return (await core.radio.stop_playback()).model_dump()

    @app.post("/api/radio/resume")
    async def radio_resume():
        return (await core.radio.resume()).model_dump()

    @app.post("/api/radio/step")
    async def radio_step(body: StepBody):
        """Соседняя станция — как клавиши 1 / 2 мини-клавиатуры (режим settings.radio.mode)."""
        return (await core.radio.step(body.delta)).model_dump()

    @app.post("/api/radio/volume")
    async def radio_volume(body: VolumeBody):
        return (await core.radio.set_volume(body.volume)).model_dump()

    @app.post("/api/radio/favorites/add")
    async def radio_favorite_add(body: StationBody):
        await core.radio.add_favorite(body.station)
        return core.radio.state().model_dump()

    @app.post("/api/radio/favorites/remove")
    async def radio_favorite_remove(body: StationBody):
        return (await core.radio.remove_favorite(body.station)).model_dump()


# ───────────────────────── Bluetooth ─────────────────────────


class PowerBody(BaseModel):
    on: bool


class ScanBody(BaseModel):
    seconds: int = Field(15, ge=1, le=60)


def _register_bluetooth(app: FastAPI, core: Core) -> None:
    @app.exception_handler(BluetoothError)
    async def _bt_err(_r: Request, e: BluetoothError):
        return _err(e.status, e.detail)

    @app.get("/api/bluetooth/state")
    async def bt_state():
        return core.bt.state().model_dump()

    @app.post("/api/bluetooth/power")
    async def bt_power(body: PowerBody):
        return (await core.bt.set_power(body.on)).model_dump()

    @app.post("/api/bluetooth/scan")
    async def bt_scan(body: ScanBody | None = None):
        return (await core.bt.scan(body.seconds if body else 15)).model_dump()

    @app.post("/api/bluetooth/devices/{mac}/connect")
    async def bt_connect(mac: str):
        return (await core.bt.connect(mac)).model_dump()

    @app.post("/api/bluetooth/devices/{mac}/disconnect")
    async def bt_disconnect(mac: str):
        return (await core.bt.disconnect(mac)).model_dump()

    @app.delete("/api/bluetooth/devices/{mac}")
    async def bt_remove(mac: str):
        return (await core.bt.remove(mac)).model_dump()


# ───────────────────────── игры ─────────────────────────


class GameLaunchBody(BaseModel):
    id: str = Field(min_length=1, max_length=80)


def _register_games(app: FastAPI, core: Core) -> None:
    @app.exception_handler(GamesError)
    async def _games_err(_r: Request, e: GamesError):
        return _err(e.status, e.detail)

    @app.get("/api/games")
    async def games_state():
        return await asyncio.to_thread(core.games.state)

    @app.post("/api/games/launch")
    async def games_launch(body: GameLaunchBody):
        return await core.games.launch(body.id)

    @app.post("/api/games/stop")
    async def games_stop():
        return await core.games.stop("stopped from the web UI")

    @app.get("/api/weather")
    async def weather():
        st = core.weather.status()
        return {"weather": st.model_dump() if st else None, "error": core.weather.error}

    @app.get("/api/controls")
    async def controls():
        return controls_info(bool(core.keypad.fds), core.store.get().keypad)


# ───────────────────────── охрана (docs/contracts/alarm.md) ─────────────────────────


class SimulateBody(BaseModel):
    kind: Literal["impact", "tilt", "move", "approach", "face", "loiter", "video_lost"]
    g: float = Field(0.5, gt=0, le=16)
    camera: Literal["front", "rear"] = "front"  # события камеры: какая их «увидела»; video_lost — у какой пропадёт
    seconds: float = Field(60.0, ge=1, le=600)  # video_lost: на сколько пропадёт видео mock-камеры


class SimulateMarkBody(BaseModel):
    kind: Literal["motion", "vehicle", "person", "sound"] = "motion"
    camera: Literal["front", "rear"] = "front"
    dur_s: float = Field(6.0, ge=0, le=600)


def _clear_sound_clips() -> None:
    d = paths.data_dir() / CLIPS_DIR
    try:
        for p in d.iterdir():
            if CLIP_RE.match(p.name):
                p.unlink(missing_ok=True)
    except OSError:
        pass


def _mock_sound_clip(start: float, dur_s: float) -> dict:
    """--mock: клип громкого звука (WAV 8 кГц: 5 с тишины, гудок dur_s, 3 с тишины) и поля отметки к нему."""
    import math
    import struct
    import wave

    rate, pre, post = 8000, 5.0, 3.0
    d = paths.data_dir() / CLIPS_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(start)) + f"-{int(start * 1000) % 1000:03d}.wav"
    n = int((pre + dur_s + post) * rate)
    loud = range(int(pre * rate), int((pre + dur_s) * rate))
    frames = b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 660 * i / rate)) if i in loud else 0)
                      for i in range(n))
    with wave.open(str(d / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return {"peak_db": -8.7, "clip": name, "clip_start": start - pre, "clip_dur_s": round(n / rate, 1)}


def _register_alarm(app: FastAPI, core: Core) -> None:
    async def status() -> dict:
        await core.alarm.poll()
        return core.alarm.status().model_dump(mode="json")

    @app.get("/api/alarm")
    async def alarm_status(events: int | None = None):
        """?events=N — только N последних событий журнала (дашборд: меньше трафика с телефона)."""
        st = await status()
        if events is not None:
            st["events"] = st["events"][:max(0, events)]
        return st

    @app.post("/api/alarm/arm")
    async def alarm_arm():
        await core.alarm.arm()
        return await status()

    @app.post("/api/alarm/disarm")
    async def alarm_disarm():
        await core.alarm.disarm()
        return await status()

    @app.post("/api/alarm/silence")
    async def alarm_silence():
        await core.alarm.silence()
        return await status()

    @app.post("/api/alarm/test")
    async def alarm_test():
        await core.alarm.test()
        return await status()

    @app.post("/api/alarm/hazard/test")
    async def alarm_hazard_test():
        """Мигнуть аварийкой 3 с текущими настройками (реле не открылось — 503 с причиной)."""
        why = await core.alarm.hazard_test()
        if why:
            return _err(503, f"Hazard relay: {why}")
        return await status()

    @app.get("/api/alarm/snapshot/{event_id}.jpg")
    async def alarm_snapshot(event_id: str, thumb: int = 0):
        """Снимок камеры в момент события журнала (если камера работала); `<id>-rear.jpg` — задней;
        `?thumb=1` — миниатюра для списков (~5 КБ)."""
        camera = "front"
        if event_id.endswith("-rear"):
            event_id, camera = event_id[:-5], "rear"
        p = await core.alarm.snapshot_thumb(event_id, camera) if thumb else core.alarm.snapshot_path(event_id, camera)
        if p is None:
            return _err(404, "No snapshot for this event")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/alarm/events")
    async def alarm_clear():
        await core.alarm.clear_events()
        return await status()

    @app.get("/api/alarm/marks")
    async def alarm_marks(limit: int = 50):
        """Отметки движения без тревоги (новые первыми) с записью, где их смотреть, и счётчики за 24 ч."""
        now = time.time()
        items = await core.recordings.list(core.current_segment())
        marks = [{**m, "rec": recording_at(items, m["ts"], m["camera"], now)}
                 for m in core.marks.latest(max(0, min(limit, 500)))]
        return {"marks": marks, "total": len(core.marks.items), "last_24h": core.marks.counts(now - 86400)}

    @app.delete("/api/alarm/marks")
    async def alarm_marks_clear():
        core.marks.clear()
        await core.marks.flush()
        await asyncio.to_thread(_clear_sound_clips)
        return {"marks": [], "total": 0, "last_24h": core.marks.counts(time.time())}

    @app.get("/api/alarm/sound/{name}")
    async def alarm_sound(name: str):
        """Клип громкого звука у задней камеры (отметка «sound»): WAV, PCM 8 кГц моно, из data_dir/sound_clips."""
        if not CLIP_RE.match(name):
            return _err(400, "Bad clip name")
        p = paths.data_dir() / CLIPS_DIR / name
        if not p.is_file():
            return _err(404, "The sound clip is gone")
        return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})

    if core.mock:
        @app.post("/api/alarm/simulate")
        async def alarm_simulate(body: SimulateBody):
            """Только --mock: событие как от датчика / GPS (если охрана включена). video_lost — у mock-камеры пропадает
            видео на seconds: событие — обычным путём, через video_loss_s (если охрана включена)."""
            if body.kind == "video_lost":
                if not await core.drop_camera_video(body.camera, body.seconds):
                    return _err(409, "That camera is off or is not the mock camera")
            else:
                await core.alarm.simulate(body.kind, body.g, camera=body.camera)
            return await status()

        @app.post("/api/alarm/simulate/mark")
        async def alarm_simulate_mark(body: SimulateMarkBody):
            """Только --mock: отметка движения, как от fnav-watch (кусок кончился только что)."""
            now = time.time()
            extra = {}
            if body.kind == "sound":  # клип: гудок посреди тишины, как настоящий (за 5 с до звука, 3 с после)
                extra = await asyncio.to_thread(_mock_sound_clip, now - body.dur_s, body.dur_s)
            return core.marks.add({"what": body.kind, "camera": body.camera, "start": now - body.dur_s,
                                   "dur_s": body.dur_s, **extra}, now)


# ───────────────────────── уровень (docs/contracts/level.md) ─────────────────────────


class ReferenceBody(BaseModel):
    """Углы эталонного уровня на полу машины: > 0 — перед выше / правый борт выше."""

    pitch_deg: float = Field(ge=-45, le=45)
    roll_deg: float = Field(ge=-45, le=45)


class ForwardBody(BaseModel):
    axis: Literal["x+", "x-", "y+", "y-", "z+", "z-"] | None = None
    learn: bool | None = None


def _register_level(app: FastAPI, core: Core) -> None:
    @app.exception_handler(LevelError)
    async def _level_err(_r: Request, e: LevelError):
        return _err(e.status, e.detail)

    @app.get("/api/level")
    async def level_status():
        return core.level.status().model_dump(mode="json")

    @app.post("/api/level/calibration/reference")
    async def level_reference(body: ReferenceBody):
        return (await core.level.calibrate_reference(body.pitch_deg, body.roll_deg)).model_dump(mode="json")

    @app.post("/api/level/calibration/forward")
    async def level_forward(body: ForwardBody):
        if (body.axis is None) == (body.learn is None):
            return _err(422, "Invalid request: give either axis or learn")
        if body.axis is not None:
            return (await core.level.set_forward_axis(body.axis)).model_dump(mode="json")
        return (await core.level.set_learning(bool(body.learn))).model_dump(mode="json")

    @app.delete("/api/level/calibration")
    async def level_reset():
        return (await core.level.reset()).model_dump(mode="json")


# ───────────────────────── статика ─────────────────────────


def _register_static(app: FastAPI) -> None:
    web = paths.web_dir()

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin/", status_code=302)

    @app.get("/admin", include_in_schema=False)
    async def admin_noslash():
        return RedirectResponse("/admin/", status_code=307)

    @app.get("/display", include_in_schema=False)
    async def display_noslash(request: Request):
        qs = request.url.query
        return RedirectResponse("/display/" + (f"?{qs}" if qs else ""), status_code=307)

    for name in ("admin", "display"):
        d = web / name
        if d.is_dir():
            app.mount(f"/{name}", NoCacheStatic(directory=str(d), html=True), name=name)
        else:
            log.warning("static directory missing: %s", d)
