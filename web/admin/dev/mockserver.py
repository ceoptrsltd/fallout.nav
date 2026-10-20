"""Temporary dev server for the fallout.nav web UI (zone D).

Serves web/admin/ at /admin/ and web/display/ at /display/ and implements the subset of
docs/contracts/api.md that the UI needs, on fake data. It is NOT the real backend:
the goal is to exercise the UI, not to duplicate fnav-core.

    python web/admin/dev/mockserver.py --port 8300      # login password: fnav-dev

Telemetry comes from fnav.mock.sensors.MockSensorHub; settings live in memory (fnav.models.Settings).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import secrets
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from fnav.mock.sensors import MockSensorHub  # noqa: E402
from fnav.models import (  # noqa: E402
    CameraStatus,
    DiskStatus,
    DisplayState,
    Settings,
    SystemStatus,
    Telemetry,
)

WEB = REPO / "web"
PASSWORD = "fnav-dev"
COOKIE = "fnav_session"
STRICT_AUTH = False  # --strict-auth: do not exempt loopback clients from auth

settings = Settings()
settings_version = 1
hub = MockSensorHub(settings)
sessions: set[str] = set()
login_fails: dict[str, tuple[int, float]] = {}
events: list[dict] = []  # broadcast events; each gets an "id"
wifi = [
    {"ssid": "Home-5G", "priority": 10, "active": True, "signal_pct": 78},
    {"ssid": "Phone hotspot", "priority": 5, "active": False, "signal_pct": 42},
    {"ssid": "Garage", "priority": 0, "active": False, "signal_pct": 0},
]
tiles_state = {"size_mb": 312.4, "count": 18452, "limit_mb": 1024.0, "hit_rate": 0.87}
sample_ts: Path | None = None
T0 = time.time()


# ───────────────────────── helpers ─────────────────────────


def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def push_event(ev: dict) -> None:
    ev = {"type": "event", **ev, "id": (events[-1]["id"] + 1) if events else 1}
    events.append(ev)
    del events[:-50]


def err(status: int, detail) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def apply_settings(new: Settings) -> None:
    global settings, settings_version
    settings = new
    settings_version += 1
    hub.apply_settings(new)


# ───────────────────────── fake recordings ─────────────────────────


def build_recordings() -> list[dict]:
    now = datetime.now().replace(microsecond=0)
    out = []
    # the current in-progress segment
    start = now - timedelta(minutes=3, seconds=20)
    out.append(_rec(start, 3 * 60 + 20, in_progress=True))
    for day, n in ((0, 5), (1, 8), (3, 4)):
        base = (now - timedelta(days=day)).replace(hour=18, minute=30, second=0)
        for i in range(n):
            st = base - timedelta(minutes=10 * (i + (1 if day == 0 else 0)) + 10 * (day == 0))
            out.append(_rec(st, 600 if i != 2 else 412, protected=(i == 1 and day == 1)))
    out.append({"path": "nosync/boot-3fa9c2d1_0001.ts", "date": "nosync", "start": None, "size_bytes": 212_000_000, "duration_s": 600.0, "protected": False, "in_progress": False})
    seen, uniq = set(), []
    for r in out:
        if r["path"] not in seen:
            seen.add(r["path"])
            uniq.append(r)
    return sorted(uniq, key=lambda r: (r["start"] or "0"), reverse=True)


def _rec(start: datetime, dur: float, protected=False, in_progress=False) -> dict:
    name = start.strftime("%Y-%m-%d_%H-%M-%S")
    return {
        "path": f"{start:%Y-%m-%d}/{name}.ts",
        "date": f"{start:%Y-%m-%d}",
        "start": start.isoformat(timespec="seconds"),
        "size_bytes": int(dur * 6000 * 1000 / 8 * 1.02),
        "duration_s": None if in_progress else float(dur),
        "protected": protected,
        "in_progress": in_progress,
    }


recordings: list[dict] = []


def make_sample_ts(path: Path) -> bool:
    """A real playable MPEG-TS (H.264) made with PyAV, so the browser player can be tested."""
    try:
        from fractions import Fraction

        import av
        import numpy as np

        out = av.open(str(path), "w", format="mpegts")
        st = out.add_stream("libx264", rate=15)
        st.width, st.height, st.pix_fmt = 640, 480, "yuv420p"
        st.options = {"g": "15", "preset": "ultrafast", "bf": "0", "tune": "zerolatency"}
        xs = np.arange(640)[None, :]
        ys = np.arange(480)[:, None]
        for i in range(15 * 40):
            arr = np.zeros((480, 640, 3), dtype=np.uint8)
            arr[:, :, 0] = (xs + i * 3) % 256
            arr[:, :, 1] = (ys + i * 2) % 256
            x = (i * 8) % 640
            arr[:, max(0, x - 20) : x + 20, 2] = 255
            fr = av.VideoFrame.from_ndarray(arr, format="rgb24")
            fr.pts, fr.time_base = i, Fraction(1, 15)
            for p in st.encode(fr):
                out.mux(p)
        for p in st.encode():
            out.mux(p)
        out.close()
        return True
    except Exception as e:  # noqa: BLE001
        print("sample TS not generated:", e)
        return False


# ───────────────────────── telemetry ─────────────────────────


def zoom_for(speed: float) -> float:
    z = settings.nav.zoom_table[0].zoom if settings.nav.zoom_table else 17
    best = -1.0
    for s in settings.nav.zoom_table:
        if s.min_mps <= speed and s.min_mps > best:
            best, z = s.min_mps, s.zoom
    return z


def fake_sectors(cov: float, samples: int) -> list[int]:
    """Plausible per-sector counts: driving along a few road directions fills those sectors first."""
    n = round(cov * 36 / 100)
    order = [3, 21, 12, 30, 4, 22, 13, 31, 2, 20, 11, 29, 5, 23, 14, 32, 1, 19, 10, 28, 6, 24, 15, 33, 0, 18, 9, 27, 7, 25, 16, 34, 8, 26, 17, 35]
    out = [0] * 36
    for rank, sec in enumerate(order[:n]):
        out[sec] = max(1, int(samples / max(n, 1) * (1.6 - rank / max(n, 1))))
    return out


def build_telemetry() -> dict:
    snap = hub.snapshot()
    g = snap.gps
    spd = g.speed_mps or 0.0
    force = settings.display.force_mode
    if force != "auto":
        mode, reason = force, "forced"
    elif g.fix == "none":
        mode, reason = settings.nav.no_fix_mode, "no_fix"
    else:
        mode, reason = ("camera" if spd < settings.nav.camera_below_mps else "map"), "speed"
    cur = next((r for r in recordings if r["in_progress"]), None)
    t = time.time()
    cam = CameraStatus(
        state="recording" if cur else "stopped",
        recording=bool(cur),
        current_segment=cur["path"] if cur else None,
        segment_started_utc=None,
        fps_actual=14.9 + (t % 3) * 0.03,
        bitrate_kbps=settings.camera.bitrate_kbps * (0.96 + (t % 5) * 0.01),
        sensor_mode="2592x1944",
        output_size=f"{settings.camera.out_w}x{settings.camera.out_h}",
        frames_dropped=0,
        recordings_count=len(recordings),
        recordings_gb=round(sum(r["size_bytes"] for r in recordings) / 1e9, 2),
        free_gb=18.4,
        time_synced=True,
    )
    cal = snap.compass_cal
    if cal is not None and not cal.sectors:
        cal = cal.model_copy(update={"sectors": fake_sectors(cal.coverage_pct, cal.samples)})
    tel = Telemetry(
        t=t,
        gps=g,
        heading=snap.heading,
        compass=snap.compass,
        compass_cal=cal,
        display=DisplayState(mode=mode, reason=reason, target_zoom=zoom_for(spd), orientation=settings.display.orientation),
        camera=cam,
        disk=DiskStatus(total_gb=28.9, free_gb=18.4, recordings_gb=cam.recordings_gb, tiles_mb=tiles_state["size_mb"]),
        system=SystemStatus(
            cpu_temp_c=round(52 + 6 * __import__("math").sin(t / 20), 1),
            throttled="0x0",
            load_1m=0.62,
            mem_used_pct=38,
            uptime_s=t - T0 + 86400 * 2,
            ip="192.168.1.57",
            wifi_ssid="Home-5G",
            wifi_signal_pct=78,
            internet=True,
            time_synced=True,
        ),
        errors=snap.errors,
    )
    return tel.model_dump(mode="json")


# ───────────────────────── MJPEG stub ─────────────────────────


def make_frame(n: int) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (768, 576), (24, 40, 28))
    d = ImageDraw.Draw(im)
    for x in range(0, 768, 48):
        d.line([(x, 0), (x, 576)], fill=(40, 70, 46))
    for y in range(0, 576, 48):
        d.line([(0, y), (768, y)], fill=(40, 70, 46))
    bx = (n * 9) % 700
    d.rectangle([bx, 250, bx + 60, 320], fill=(255, 182, 46))
    d.text((20, 20), "MOCK CAMERA  " + datetime.now().strftime("%H:%M:%S.%f")[:-3], fill=(93, 255, 138))
    d.text((20, 40), f"frame {n}", fill=(200, 200, 200))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return buf.getvalue()


# ───────────────────────── app ─────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sample_ts, recordings
    recordings = build_recordings()
    tmp = Path(tempfile.gettempdir()) / "fnav_mock_sample.ts"
    if not tmp.exists() or tmp.stat().st_size < 100_000:
        make_sample_ts(tmp)
    sample_ts = tmp if tmp.exists() else None
    await hub.start()

    async def periodic():
        while True:
            await asyncio.sleep(40)
            push_event({"event": "toast", "level": "info", "text": "Mock event: GPS module is healthy"})

    task = asyncio.create_task(periodic())
    yield
    task.cancel()
    await hub.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

PUBLIC_API = {"/api/login", "/api/session"}
LOOPBACK_OK = ("/api/camera/mjpeg", "/api/settings", "/ws")


def is_local(request: Request) -> bool:
    return (not STRICT_AUTH) and request.client is not None and request.client.host in ("127.0.0.1", "::1")


@app.middleware("http")
async def auth_and_headers(request: Request, call_next):
    path = request.url.path
    authed = request.cookies.get(COOKIE) in sessions
    if path.startswith("/admin/dev"):
        return err(404, "Not found")
    if path.startswith("/api/") and path not in PUBLIC_API and not authed:
        local_ok = is_local(request) and request.method == "GET" and path in LOOPBACK_OK
        if not local_ok:
            return err(401, "Login required")
    if path in ("/admin/", "/admin/index.html") and not authed:
        return RedirectResponse("/admin/login.html")
    resp = await call_next(request)
    if path.startswith(("/admin", "/display")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
async def root():
    return RedirectResponse("/admin/")


# ── auth ──
@app.post("/api/login")
async def login(request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    n, last = login_fails.get(ip, (0, 0.0))
    if n >= 5 and time.time() - last < 30:
        return err(429, "Too many failed attempts, wait 30 s")
    if n >= 5:
        n = 0
    body = await request.json()
    if body.get("password") != PASSWORD:
        login_fails[ip] = (n + 1, time.time())
        return err(401, "Wrong password")
    login_fails.pop(ip, None)
    tok = secrets.token_urlsafe(24)
    sessions.add(tok)
    r = JSONResponse({"ok": True})
    r.set_cookie(COOKIE, tok, httponly=True, samesite="lax", max_age=30 * 86400)
    return r


@app.post("/api/logout")
async def logout(request: Request):
    sessions.discard(request.cookies.get(COOKIE, ""))
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/session")
async def session(request: Request):
    return {"authenticated": request.cookies.get(COOKIE) in sessions}


# ── settings ──
@app.get("/api/settings")
async def get_settings():
    return settings.model_dump(mode="json")


@app.get("/api/settings/defaults")
async def get_defaults():
    return Settings().model_dump(mode="json")


def _validation_response(e: ValidationError) -> JSONResponse:
    detail = [{"loc": ["body", *map(str, x["loc"])], "msg": x["msg"], "type": x["type"]} for x in e.errors()]
    return err(422, detail)


@app.patch("/api/settings")
async def patch_settings(request: Request):
    patch = await request.json()
    if not isinstance(patch, dict):
        return err(422, "Body must be a JSON object")
    data = settings.model_dump(mode="json")
    deep_merge(data, patch)
    try:
        new = Settings.model_validate(data)
    except ValidationError as e:
        return _validation_response(e)
    if " " in new.tiles.esri_api_key:  # extra server-side rule, to exercise 422 handling in the UI
        return err(422, [{"loc": ["body", "tiles", "esri_api_key"], "msg": "Key must not contain spaces", "type": "value_error"}])
    apply_settings(new)
    return new.model_dump(mode="json")


@app.put("/api/settings")
async def put_settings(request: Request):
    try:
        new = Settings.model_validate(await request.json())
    except ValidationError as e:
        return _validation_response(e)
    apply_settings(new)
    return new.model_dump(mode="json")


@app.post("/api/settings/reset")
async def reset_settings():
    apply_settings(Settings())
    return settings.model_dump(mode="json")


@app.post("/api/display/force")
async def display_force(request: Request):
    mode = (await request.json()).get("mode")
    if mode not in ("auto", "map", "camera"):
        return err(422, "mode must be auto, map or camera")
    data = settings.model_dump(mode="json")
    data["display"]["force_mode"] = mode
    apply_settings(Settings.model_validate(data))
    return {"ok": True, "mode": mode}


# ── tiles ──
@app.get("/api/tiles/stats")
async def tiles_stats():
    tiles_state["limit_mb"] = float(settings.storage.tile_cache_mb)
    return tiles_state


@app.delete("/api/tiles")
async def tiles_clear():
    tiles_state.update(size_mb=0.0, count=0, hit_rate=0.0)
    return {"ok": True}


_tile_cache: dict[str, bytes] = {}


def make_tile(layer: str, z: str, x: str, y: str) -> bytes:
    """Placeholder tile (grid + label), so the display preview has a visible map without internet."""
    from PIL import Image, ImageDraw

    key = f"{layer}/{z}/{x}/{y}"
    if key in _tile_cache:
        return _tile_cache[key]
    base = {"imagery": (52, 70, 48), "street": (70, 74, 66)}.get(layer)
    im = Image.new("RGBA", (256, 256), (*base, 255) if base else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if layer in ("imagery", "street"):
        d.rectangle([0, 0, 255, 255], outline=(95, 125, 90, 255))
        d.text((8, 8), f"{layer} {z}/{x}/{y}", fill=(200, 220, 190, 255))
    elif layer == "transport":
        d.line([(0, 128 + (int(x) % 5) * 10), (255, 90 + (int(y) % 7) * 12)], fill=(255, 220, 120, 255), width=4)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    if len(_tile_cache) > 2000:
        _tile_cache.clear()
    _tile_cache[key] = buf.getvalue()
    return _tile_cache[key]


@app.get("/tiles/{layer}/{z}/{x}/{y}")
async def tiles_stub(layer: str, z: str, x: str, y: str):
    if layer not in ("imagery", "transport", "places", "street"):
        return err(404, "Unknown layer")
    return Response(await asyncio.to_thread(make_tile, layer, z, x, y.split(".")[0]), media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ── recordings ──
def _find(path: str) -> dict:
    for r in recordings:
        if r["path"] == path:
            return r
    raise HTTPException(404, "No such recording")


@app.get("/api/recordings")
async def list_recordings():
    cur = next((r for r in recordings if r["in_progress"]), None)
    if cur:
        started = datetime.fromisoformat(cur["start"])
        cur["size_bytes"] = int((datetime.now() - started).total_seconds() * 6000 * 1000 / 8)
    return recordings


@app.get("/api/recordings/file/{path:path}")
async def get_recording(path: str, request: Request):
    _find(path)
    if not sample_ts or not sample_ts.exists():
        raise HTTPException(503, "Sample video not available")
    data = sample_ts.read_bytes()
    size = len(data)
    rng = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes", "Content-Type": "video/mp2t"}
    m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        end = min(end, size - 1)
        if start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(data[start : end + 1], status_code=206, headers=headers, media_type="video/mp2t")
    headers["Content-Disposition"] = f'inline; filename="{path.split("/")[-1]}"'
    return Response(data, headers=headers, media_type="video/mp2t")


@app.post("/api/recordings/protect")
async def protect(request: Request):
    b = await request.json()
    r = _find(b.get("path", ""))
    r["protected"] = bool(b.get("protected"))
    return {"ok": True}


@app.post("/api/recordings/protect-current")
async def protect_current():
    cur = next((r for r in recordings if r["in_progress"]), None)
    if not cur:
        return err(409, "Nothing is being recorded")
    cur["protected"] = True
    push_event({"event": "protected", "segment": cur["path"]})
    return {"ok": True, "segment": cur["path"]}


@app.delete("/api/recordings/file/{path:path}")
async def delete_recording(path: str, force: int = 0):
    r = _find(path)
    if r["in_progress"]:
        return err(409, "Cannot delete a recording in progress")
    if r["protected"] and not force:
        return err(409, "Recording is protected")
    recordings.remove(r)
    return {"ok": True}


# ── camera ──
@app.get("/api/camera/status")
async def camera_status():
    return build_telemetry()["camera"]


@app.get("/api/camera/mjpeg")
async def camera_mjpeg():
    async def gen():
        n = 0
        while True:
            jpg = await asyncio.to_thread(make_frame, n)
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            n += 1
            await asyncio.sleep(0.1)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/camera/snapshot.jpg")
async def camera_snapshot():
    return Response(await asyncio.to_thread(make_frame, 0), media_type="image/jpeg")


@app.post("/api/camera/whep")
async def camera_whep():
    return err(503, "WebRTC is not available in the mock server (MediaMTX is not running)")


# ── sensors ──
@app.get("/api/gps/satellites")
async def gps_satellites():
    return [s.model_dump(mode="json") for s in hub.snapshot().satellites]


@app.post("/api/gps/configure")
async def gps_configure():
    return await hub.gps_configure()


@app.get("/api/compass/calibration")
async def calib_status():
    return hub.calibration_status().model_dump(mode="json")


@app.post("/api/compass/calibration/start")
async def calib_start():
    hub.calibration_start()
    return {"ok": True}


@app.post("/api/compass/calibration/cancel")
async def calib_cancel():
    hub.calibration_cancel()
    return {"ok": True}


@app.post("/api/compass/calibration/reset-auto")
async def calib_reset_auto():
    hub.auto_calibration_reset()
    return {"ok": True}


@app.post("/api/compass/calibration/finish")
async def calib_finish():
    cal = hub.calibration_finish()
    data = settings.model_dump(mode="json")
    data["compass"]["calibration"] = cal.model_dump(mode="json")
    apply_settings(Settings.model_validate(data))
    return cal.model_dump(mode="json")


# ── system ──
@app.get("/api/system/status")
async def system_status():
    tel = build_telemetry()
    return {
        "system": tel["system"],
        "disk": tel["disk"],
        "services": {"fnav-core": "active", "fnav-camera": "active", "fnav-display": "active", "mediamtx": "active", "gpsd": "active", "chrony": "active"},
        "version": "0.1.0-mock",
    }


@app.get("/api/system/logs")
async def system_logs(unit: str = "fnav-core", lines: int = 200):
    if not (unit.startswith("fnav-") or unit in ("mediamtx", "gpsd")):
        return err(400, "Unit not allowed")
    now = datetime.now()
    msgs = ["INFO telemetry tick ok", "INFO ws client connected", "INFO settings saved", "WARNING tile fetch timeout (z=17)", "INFO segment closed, fsync ok", "DEBUG gps fix 3d sats=12"]
    out = []
    for i in range(min(lines, 500)):
        ts = now - timedelta(seconds=(min(lines, 500) - i) * 3)
        out.append(f"{ts:%b %d %H:%M:%S} fnav {unit}[812]: {msgs[(i * 7) % len(msgs)]}")
    return {"lines": out}


@app.post("/api/system/reboot")
async def reboot():
    return {"ok": True, "mock": True}


@app.post("/api/system/shutdown")
async def shutdown():
    return {"ok": True, "mock": True}


@app.get("/api/system/wifi")
async def wifi_list():
    return wifi


@app.post("/api/system/wifi")
async def wifi_add(request: Request):
    b = await request.json()
    ssid = (b.get("ssid") or "").strip()
    if not ssid:
        return err(422, "SSID is required")
    for w in wifi:
        if w["ssid"] == ssid:
            w["priority"] = int(b.get("priority", w["priority"]))
            return {"ok": True, "mock": True}
    wifi.append({"ssid": ssid, "priority": int(b.get("priority", 0)), "active": False, "signal_pct": 0})
    return {"ok": True, "mock": True}


@app.delete("/api/system/wifi/{ssid}")
async def wifi_del(ssid: str):
    for w in wifi:
        if w["ssid"] == ssid:
            wifi.remove(w)
            return {"ok": True, "mock": True}
    return err(404, "No such network")


@app.post("/api/system/password")
async def change_password(request: Request):
    global PASSWORD
    b = await request.json()
    if b.get("old") != PASSWORD:
        return err(400, "Current password is wrong")
    if len(b.get("new") or "") < 6:
        return err(422, "New password must be at least 6 characters")
    PASSWORD = b["new"]
    return {"ok": True}


# ── dev helpers ──
@app.post("/api/_mock/button")
async def mock_button(request: Request):
    b = await request.json() if (await request.body()) else {}
    push_event({"event": "button", "value": b.get("value", "press")})
    push_event({"event": "toast", "level": b.get("level", "warn"), "text": b.get("text", "Mock warning: compass not responding")})
    return {"ok": True}


# ── websocket ──
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    local = (not STRICT_AUTH) and ws.client is not None and ws.client.host in ("127.0.0.1", "::1")
    if ws.cookies.get(COOKIE) not in sessions and not local:
        await ws.close(code=4401)
        return
    await ws.accept()
    topics: set[str] = set()
    ver = -1
    last_ev = events[-1]["id"] if events else 0
    last_nmea = 0.0

    async def reader():
        try:
            while True:
                m = json.loads(await ws.receive_text())
                if m.get("type") == "subscribe":
                    topics.update(m.get("topics", []))
                elif m.get("type") == "unsubscribe":
                    topics.difference_update(m.get("topics", []))
        except (WebSocketDisconnect, RuntimeError):
            pass

    rtask = asyncio.create_task(reader())
    try:
        while not rtask.done():
            if settings_version != ver:
                ver = settings_version
                await ws.send_text(json.dumps({"type": "settings", "settings": settings.model_dump(mode="json")}))
            await ws.send_text(json.dumps(build_telemetry()))
            for ev in [e for e in events if e["id"] > last_ev]:
                last_ev = ev["id"]
                await ws.send_text(json.dumps(ev))
            if "nmea" in topics and time.time() - last_nmea >= 1.0:
                last_nmea = time.time()
                await ws.send_text(json.dumps({"type": "nmea", "lines": hub.nmea_tail(10)}))
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rtask.cancel()


# ── static ──
app.mount("/admin", StaticFiles(directory=WEB / "admin", html=True), name="admin")
if (WEB / "display").exists():
    app.mount("/display", StaticFiles(directory=WEB / "display", html=True), name="display")


def main() -> None:
    global STRICT_AUTH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--strict-auth", action="store_true", help="require a session even from loopback (to test the UI auth flow)")
    a = ap.parse_args()
    STRICT_AUTH = a.strict_auth
    print(f"fnav web UI mock: http://{a.host}:{a.port}/admin/   (password: {PASSWORD})")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
