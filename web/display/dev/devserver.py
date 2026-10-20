"""Временный dev-сервер для /display (агент B), пока нет настоящего fnav-core.

    python web/display/dev/devserver.py --port 8100
    FNAV_MOCK_SPEEDUP=4 python web/display/dev/devserver.py --port 8100 --orientation north_up

Что умеет:
  * /display/**             — статика web/display/;
  * WS /ws                  — {"type":"settings"} при подключении и после изменений, телеметрия 10 Гц из
                              MockSensorHub + упрощённая логика режима/зума (docs/contracts/api.md);
  * GET/PATCH /api/settings — чтение / частичное глубокое слияние (рассылается по WS);
  * GET /dev/set?display.orientation=north_up&display.force_mode=camera — то же через адресную строку;
  * POST /api/display/force {"mode": "auto"|"map"|"camera"};
  * /tiles/{layer}/{z}/{x}/{y} — прокси напрямую к Esri (без кэша; --offline → всегда 404);
  * /api/camera/mjpeg       — тестовый MJPEG (Pillow); --no-camera → 503.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import RedirectResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from fnav.mock.sensors import MockSensorHub  # noqa: E402
from fnav.models import CameraStatus, DisplayState, Settings, SystemStatus, Telemetry  # noqa: E402

LAYERS = {"imagery", "transport", "places", "street"}


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class NavLogic:
    """Упрощённая копия логики fnav-core: режим экрана с гистерезисом и ступени зума."""

    def __init__(self) -> None:
        self.mode = "camera"
        self.reason = "startup"
        self.zoom: float | None = None
        self._mode_cand: tuple[str, float] | None = None
        self._zoom_cand: tuple[float, float] | None = None

    def update(self, s: Settings, speed: float | None, has_fix: bool, now: float) -> DisplayState:
        nav = s.nav
        v = speed or 0.0
        steps = sorted(nav.zoom_table, key=lambda z: z.min_mps)
        want_zoom = steps[0].zoom if steps else 17
        for st in steps:
            if st.min_mps <= v:
                want_zoom = st.zoom
        if self.zoom is None:
            self.zoom = want_zoom
        elif want_zoom != self.zoom:
            if self._zoom_cand is None or self._zoom_cand[0] != want_zoom:
                self._zoom_cand = (want_zoom, now)
            elif now - self._zoom_cand[1] >= nav.zoom_delay_s:
                self.zoom, self._zoom_cand = want_zoom, None
        else:
            self._zoom_cand = None

        fm = s.display.force_mode
        if fm != "auto":
            self.mode, self.reason, self._mode_cand = fm, "forced", None
        elif not has_fix:
            self.mode, self.reason, self._mode_cand = nav.no_fix_mode, "no_fix", None
        else:
            if self.reason in ("forced", "no_fix", "startup"):
                self.reason = "speed"
                self.mode = "camera" if v < nav.camera_below_mps else "map"
            want = self.mode
            if self.mode == "map" and v < nav.camera_below_mps:
                want = "camera"
            elif self.mode == "camera" and v > nav.map_above_mps:
                want = "map"
            if want != self.mode:
                if self._mode_cand is None or self._mode_cand[0] != want:
                    self._mode_cand = (want, now)
                elif now - self._mode_cand[1] >= nav.switch_delay_s:
                    self.mode, self._mode_cand = want, None
            else:
                self._mode_cand = None
        return DisplayState(
            mode=self.mode, reason=self.reason, target_zoom=self.zoom, orientation=s.display.orientation
        )


class Dev:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        base = Settings().model_dump(mode="json")
        if args.settings:
            base = deep_merge(base, json.loads(Path(args.settings).read_text(encoding="utf-8")))
        if args.orientation:
            base["display"]["orientation"] = args.orientation
        if args.force_mode:
            base["display"]["force_mode"] = args.force_mode
        self.settings = Settings.model_validate(base)
        self.hub = MockSensorHub(self.settings)
        self.nav = NavLogic()
        self.clients: set[WebSocket] = set()
        self.http: httpx.AsyncClient | None = None
        self.task: asyncio.Task | None = None
        self.frozen = False

    def patch(self, patch: dict) -> Settings:
        merged = deep_merge(self.settings.model_dump(mode="json"), patch)
        self.settings = Settings.model_validate(merged)
        self.hub.apply_settings(self.settings)
        asyncio.get_running_loop().create_task(self.broadcast(self.settings_msg()))
        return self.settings

    def settings_msg(self) -> str:
        return json.dumps({"type": "settings", "settings": self.settings.model_dump(mode="json")})

    async def broadcast(self, text: str) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)

    def telemetry(self) -> str:
        snap = self.hub.snapshot()
        now = time.monotonic()
        gps = snap.gps
        has_fix = gps.fix != "none" and gps.lat is not None
        if self.args.no_fix:
            gps = gps.model_copy(update={"fix": "none", "lat": None, "lon": None})
            has_fix = False
        disp = self.nav.update(self.settings, gps.speed_mps, has_fix, now)
        tel = Telemetry(
            t=time.time(),
            gps=gps,
            heading=snap.heading,
            compass=snap.compass,
            display=disp,
            camera=CameraStatus(state="recording", recording=not self.args.no_camera),
            system=SystemStatus(internet=not self.args.offline, cpu_temp_c=48.0),
            errors=snap.errors,
        )
        return tel.model_dump_json()

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            if self.clients and not self.frozen:
                await self.broadcast(self.telemetry())


def build_app(args: argparse.Namespace) -> FastAPI:
    dev = Dev(args)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await dev.hub.start()
        dev.http = httpx.AsyncClient(
            timeout=5.0, headers={"User-Agent": "fallout.nav-devserver/0.1"}, follow_redirects=True
        )
        dev.task = asyncio.create_task(dev.loop())
        try:
            yield
        finally:
            dev.task.cancel()
            await dev.hub.stop()
            await dev.http.aclose()

    app = FastAPI(title="fnav display devserver", lifespan=lifespan)

    @app.get("/")
    async def _root() -> RedirectResponse:
        return RedirectResponse("/display/")

    @app.get("/display")
    async def _display_noslash(request: Request) -> RedirectResponse:
        q = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse("/display/" + q)

    @app.websocket("/ws")
    async def _ws(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(dev.settings_msg())
        dev.clients.add(ws)
        try:
            while True:
                await ws.receive_text()  # subscribe/unsubscribe — игнорируем
        except WebSocketDisconnect:
            pass
        finally:
            dev.clients.discard(ws)

    @app.get("/api/settings")
    async def _get_settings() -> dict:
        return dev.settings.model_dump(mode="json")

    @app.patch("/api/settings")
    async def _patch_settings(request: Request) -> dict:
        try:
            return dev.patch(await request.json()).model_dump(mode="json")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"Invalid settings: {e}") from e

    @app.get("/dev/set")
    async def _dev_set(request: Request) -> dict:
        """/dev/set?display.orientation=north_up&display.layers.street_map_instead=true"""
        patch: dict[str, Any] = {}
        for key, raw in request.query_params.items():
            if key == "freeze":  # заморозить телеметрию (проверка «НЕТ СВЯЗИ»)
                dev.frozen = raw in ("1", "true")
                continue
            try:
                val: Any = json.loads(raw)
            except ValueError:
                val = raw
            cur = patch
            parts = key.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = val
        try:
            s = dev.patch(patch) if patch else dev.settings
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"Invalid settings: {e}") from e
        return {"ok": True, "frozen": dev.frozen, "display": s.display.model_dump(mode="json")}

    @app.post("/api/display/force")
    async def _force(request: Request) -> dict:
        mode = (await request.json()).get("mode", "auto")
        if mode not in ("auto", "map", "camera"):
            raise HTTPException(422, "mode: auto|map|camera")
        dev.patch({"display": {"force_mode": mode}})
        return {"ok": True}

    @app.get("/tiles/{layer}/{z}/{x}/{y}")
    async def _tile(layer: str, z: int, x: int, y: int) -> Response:
        if layer not in LAYERS or not (0 <= z <= 23) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
            raise HTTPException(404, "No such tile")
        if args.offline:
            raise HTTPException(404, "Offline (--offline)")
        ts = dev.settings.tiles
        url = getattr(ts, f"{layer}_url").format(z=z, x=x, y=y)
        if ts.esri_api_key:
            url += ("&" if "?" in url else "?") + "token=" + ts.esri_api_key
        assert dev.http is not None
        try:
            r = await dev.http.get(url)
        except httpx.HTTPError as e:
            raise HTTPException(404, f"Esri unreachable: {e.__class__.__name__}") from e
        if r.status_code != 200 or not r.content:
            raise HTTPException(404, f"Esri returned {r.status_code}")
        return Response(
            r.content,
            media_type=r.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "max-age=86400"},
        )

    @app.get("/api/camera/mjpeg")
    async def _mjpeg() -> StreamingResponse:
        if args.no_camera:
            raise HTTPException(503, "Camera unavailable (--no-camera)")
        boundary = "frame"

        async def gen():
            i = 0
            while True:
                jpg = await asyncio.to_thread(_test_frame, i)
                yield (
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode()
                    + jpg
                    + b"\r\n"
                )
                i += 1
                await asyncio.sleep(1 / 10)

        return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")

    app.mount("/display", StaticFiles(directory=REPO / "web" / "display", html=True), name="display")
    if (REPO / "web" / "admin").is_dir():
        app.mount("/admin", StaticFiles(directory=REPO / "web" / "admin", html=True), name="admin")
    return app


_font_cache: dict[int, Any] = {}


def _font(size: int):
    from PIL import ImageFont

    if size not in _font_cache:
        f = None
        for name in ("DejaVuSansMono-Bold.ttf", "consolab.ttf", "arialbd.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        _font_cache[size] = f or ImageFont.load_default()
    return _font_cache[size]


def _test_frame(i: int) -> bytes:
    """Тестовый кадр 768×576: градиент «дорога», движущиеся полосы, время."""
    from PIL import Image, ImageDraw

    w, h = 768, 576
    img = Image.new("L", (w, h), 40)
    d = ImageDraw.Draw(img)
    for y in range(h // 2, h, 4):
        d.rectangle([0, y, w, y + 4], fill=int(40 + (y - h / 2) / (h / 2) * 90))
    d.rectangle([0, 0, w, h // 2], fill=150)
    vx = w // 2
    for k in range(8):
        p = ((i * 0.08 + k / 8) % 1.0) ** 2
        y = int(h / 2 + p * h / 2)
        half = int(4 + p * 40)
        d.rectangle([vx - half // 4, y, vx + half // 4, y + int(4 + p * 30)], fill=230)
    d.polygon([(vx - 30, h // 2), (vx + 30, h // 2), (w, h), (0, h)], outline=200)
    ang = i * 0.1
    d.ellipse([60, 60, 160, 160], outline=255, width=4)
    d.line([110, 110, 110 + 45 * math.cos(ang), 110 + 45 * math.sin(ang)], fill=255, width=4)
    d.text((200, 70), "CAMERA TEST  " + time.strftime("%H:%M:%S"), fill=255, font=_font(36))
    d.text((200, 120), f"frame {i}", fill=220, font=_font(28))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser(description="fnav /display dev-server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--orientation", choices=["heading_up", "north_up"])
    ap.add_argument("--force-mode", choices=["auto", "map", "camera"])
    ap.add_argument("--settings", help="JSON file with partial settings (deep-merged)")
    ap.add_argument("--offline", action="store_true", help="tiles always 404, internet=false")
    ap.add_argument("--no-camera", action="store_true", help="MJPEG always 503")
    ap.add_argument("--no-fix", action="store_true", help="GPS without fix")
    args = ap.parse_args()
    uvicorn.run(build_app(args), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
