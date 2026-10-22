"""Temporary dev server for the fallout.nav web UI (zone D).

Serves web/admin/ at /admin/ and web/display/ at /display/ and implements the subset of
docs/contracts/api.md that the UI needs, on fake data. It is NOT the real backend:
the goal is to exercise the UI, not to duplicate fnav-core.

    python web/admin/dev/mockserver.py --port 8300      # login password: fnav-dev

Remote access (docs/contracts/remote.md) is emulated under /fnadmin/: the prefix is stripped, loopback
clients get no auth exemption there, and with --offline (or POST /api/_mock/offline) only what the
server itself answers works (api/tracking/*, login/logout/session with "offline": true).
The tracking journal is a few weeks of simulated driving; POST /api/_mock/tracking-down → 503.

Telemetry comes from fnav.mock.sensors.MockSensorHub; settings live in memory (fnav.models.Settings).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import itertools
import json
import queue
import random
import re
import secrets
import sys
import tempfile
import threading
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
REMOTE_PREFIX = "/fnadmin"  # remote access through the server (docs/contracts/remote.md)
OFFLINE = False  # --offline: under /fnadmin/ the device is "not connected"
TRACK_DOWN = False  # api/tracking/* → 503 (tracking server unreachable)
RADIO_DIR_DOWN = False  # api/radio/stations → 503 (radio-browser unreachable)
# what the server answers itself when the device is offline
SERVER_PATHS = ("/api/tracking/", "/api/login", "/api/logout", "/api/session", "/api/_mock/")

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


# ───────────────────────── tracking journal (fake, docs/contracts/remote.md §2) ─────────────────────────
#
# A few weeks of plausible driving (home ↔ work along the mock route, shop/fuel stops, traffic lights) are
# sampled once a minute and merged exactly like fnav-relay does: same place within 50 m → the visit goes on,
# a known place within 50 m → a new visit there, otherwise a new place.

TRACK_RADIUS_M = 50.0
TRACK_STOP_MIN_S = 300


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


class FakeJournal:
    def __init__(self) -> None:
        self.places: list[dict] = []
        self.visits: list[dict] = []
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.last: dict = {}

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return int(lat * 1000 // 1), int(lon * 600 // 1)  # ~110 m × ~105 m at 55°N

    def _near(self, lat: float, lon: float) -> int | None:
        cy, cx = self._cell(lat, lon)
        best, bd = None, TRACK_RADIUS_M
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for pid in self.grid.get((cy + dy, cx + dx), ()):
                    p = self.places[pid]
                    d = _dist_m(lat, lon, p["lat"], p["lon"])
                    if d <= bd:
                        best, bd = pid, d
        return best

    def add(self, ts: float, lat: float, lon: float, speed: float = 0.0, course: float | None = None) -> None:
        self.last = {"ts": ts, "lat": lat, "lon": lon, "speed_mps": speed, "course_deg": course, "received_ts": ts + 2}
        cur = self.visits[-1] if self.visits else None
        if cur is not None:
            p = self.places[cur["place_id"]]
            if _dist_m(lat, lon, p["lat"], p["lon"]) <= TRACK_RADIUS_M:
                cur["end_ts"] = ts
                cur["samples"] += 1
                return
        pid = self._near(lat, lon)
        if pid is None:
            pid = len(self.places)
            self.places.append({"id": pid + 1, "lat": round(lat, 6), "lon": round(lon, 6), "created_ts": ts})
            self.grid.setdefault(self._cell(lat, lon), []).append(pid)
        self.visits.append({"place_id": pid, "start_ts": ts, "end_ts": ts, "samples": 1})

    def _in_range(self, frm: float | None, to: float | None):
        for v in self.visits:
            if (frm is None or v["end_ts"] >= frm) and (to is None or v["start_ts"] <= to):
                yield v

    def places_json(self, frm: float | None, to: float | None) -> dict:
        agg: dict[int, dict] = {}
        for v in self._in_range(frm, to):
            a = agg.setdefault(v["place_id"], {"visits": 0, "first_ts": v["start_ts"], "last_ts": v["end_ts"], "longest_s": 0.0, "total_s": 0.0})
            d = v["end_ts"] - v["start_ts"]
            a["visits"] += 1
            a["first_ts"] = min(a["first_ts"], v["start_ts"])
            a["last_ts"] = max(a["last_ts"], v["end_ts"])
            a["longest_s"] = max(a["longest_s"], d)
            a["total_s"] += d
        out = []
        for pid, a in agg.items():
            p = self.places[pid]
            out.append({"id": p["id"], "lat": p["lat"], "lon": p["lon"], "kind": "stop" if a["longest_s"] >= TRACK_STOP_MIN_S else "transit", **a})
        return {"places": out, "stop_min_s": TRACK_STOP_MIN_S, "radius_m": TRACK_RADIUS_M}

    def place_json(self, place_id: int) -> dict | None:
        pid = place_id - 1
        if not (0 <= pid < len(self.places)):
            return None
        vs = [v for v in self.visits if v["place_id"] == pid]
        p = dict(self.places[pid])
        p["visits"] = len(vs)
        p["longest_s"] = max((v["end_ts"] - v["start_ts"] for v in vs), default=0.0)
        p["total_s"] = sum(v["end_ts"] - v["start_ts"] for v in vs)
        p["kind"] = "stop" if p["longest_s"] >= TRACK_STOP_MIN_S else "transit"
        visits = [{"start_ts": v["start_ts"], "end_ts": v["end_ts"], "duration_s": v["end_ts"] - v["start_ts"], "samples": v["samples"]} for v in vs]
        visits.sort(key=lambda v: v["start_ts"], reverse=True)
        return {"place": p, "visits": visits}

    def track_json(self, frm: float | None, to: float | None) -> dict:
        pts = []
        for v in self._in_range(frm, to):
            p = self.places[v["place_id"]]
            pts.append([v["start_ts"], p["lat"], p["lon"]])
        return {"points": pts}


def _polyline_at(pts: list[tuple[float, float]], dist: float) -> tuple[float, float]:
    dist = max(0.0, dist)
    for a, b in itertools.pairwise(pts):
        seg = _dist_m(a[0], a[1], b[0], b[1])
        if dist <= seg:
            k = dist / seg if seg else 0.0
            return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        dist -= seg
    return pts[-1]


def _polyline_len(pts: list[tuple[float, float]]) -> float:
    return sum(_dist_m(a[0], a[1], b[0], b[1]) for a, b in itertools.pairwise(pts))


def build_journal(now: float | None = None, days: int = 24) -> FakeJournal:
    import random

    from fnav.mock.sensors import ROUTE

    rnd = random.Random(7)
    j = FakeJournal()
    out_route = ROUTE[: len(ROUTE) // 2 + 1]
    home, work = out_route[0], out_route[-1]
    shop_d, fuel_d = 1900.0, 3300.0  # metres along the route
    length = _polyline_len(out_route)
    now = now or time.time()
    day0 = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)

    def jit() -> float:  # ≈ 7 m of GPS scatter while parked
        return rnd.gauss(0, 6e-5)

    def park(t: float, until: float, pos: tuple[float, float], every: float = 60.0) -> float:
        while t < until and t < now:
            j.add(t, pos[0] + jit(), pos[1] + jit())
            t += every
        return t

    def drive(t: float, a: float, b: float, stops: dict[float, float]) -> float:
        """From a to b metres along the route (either direction) at ~40 km/h, one sample a minute."""
        sign = 1 if b >= a else -1
        pending = sorted(stops.items(), key=lambda kv: kv[0] * sign)
        d = a
        while (b - d) * sign > 0 and t < now:
            lat, lon = _polyline_at(out_route, d)
            j.add(t, lat + rnd.gauss(0, 1e-5), lon + rnd.gauss(0, 1e-5), 11.0)
            t += 60
            if rnd.random() < 0.12:  # red light: the same point again a minute later
                j.add(t, lat + jit(), lon + jit(), 0.0)
                t += 60
            nd = d + sign * (11.0 * 60 + rnd.uniform(-120, 120))
            if pending and (pending[0][0] - d) * sign > 0 and (nd - pending[0][0]) * sign >= 0:
                sd, dur = pending.pop(0)
                t = park(t, t + dur, _polyline_at(out_route, sd))
                nd = sd + sign * 200
            d = nd
        return t

    for i in range(days + 1):
        day = day0 + timedelta(days=i)
        if day.timestamp() > now:
            break
        weekend = day.weekday() >= 5
        t = day.timestamp() + (9.5 if weekend else 8) * 3600 + rnd.uniform(0, 1500)
        j.add(t, home[0] + jit(), home[1] + jit())  # Pi wakes up at home (the night visit goes on)
        t += 60
        if weekend:
            t = drive(t, 0, shop_d, {})
            t = park(t, t + rnd.uniform(1500, 3600), _polyline_at(out_route, shop_d))
            t = drive(t, shop_d, 0, {})
        else:
            stops = {fuel_d: rnd.uniform(300, 520)} if i % 5 == 2 else {}
            t = drive(t, 0, length, stops)
            t = park(t, day.timestamp() + 17.5 * 3600 + rnd.uniform(0, 2400), work)
            back = {shop_d: rnd.uniform(900, 1500)} if i % 2 else {}
            t = drive(t, length, 0, back)
        park(t, t + 300, home)
    return j


journal: FakeJournal | None = None


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
    out = tel.model_dump(mode="json")
    out["radio"] = radio.compact()  # radio.md §5 (added as a dict: the model may not have the field yet)
    return out


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
    global sample_ts, recordings, journal
    recordings = build_recordings()
    journal = await asyncio.to_thread(build_journal)
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


def is_remote(conn) -> bool:
    return bool(conn.scope.get("fnav_remote"))


def is_local(request: Request) -> bool:
    return (not STRICT_AUTH) and not is_remote(request) and request.client is not None and request.client.host in ("127.0.0.1", "::1")


@app.middleware("http")
async def auth_and_headers(request: Request, call_next):
    path = request.url.path
    authed = request.cookies.get(COOKIE) in sessions
    remote = is_remote(request)
    if path.startswith("/admin/dev"):
        return err(404, "Not found")
    if remote and OFFLINE and path.startswith(("/api/", "/tiles/")) and not path.startswith(SERVER_PATHS):
        return err(503, "Device is offline")
    if path.startswith("/api/") and path not in PUBLIC_API and not authed:
        local_ok = is_local(request) and request.method == "GET" and path in LOOPBACK_OK
        if not local_ok:
            return err(401, "Login required")
    # remotely the static files come from the server without a redirect: the UI checks the session itself
    if path in ("/admin/", "/admin/index.html") and not authed and not remote:
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
    out = {"authenticated": request.cookies.get(COOKIE) in sessions}
    if is_remote(request) and OFFLINE:
        out.update(local=False, mock=False, offline=True)  # answered by the server instead of the device
    return out


# ── settings ──
@app.get("/api/settings")
async def get_settings():
    return settings_json()


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
    if (r := split_extra(patch)) is not None:
        return r
    data = settings.model_dump(mode="json")
    deep_merge(data, patch)
    try:
        new = Settings.model_validate(data)
    except ValidationError as e:
        return _validation_response(e)
    if " " in new.tiles.esri_api_key:  # extra server-side rule, to exercise 422 handling in the UI
        return err(422, [{"loc": ["body", "tiles", "esri_api_key"], "msg": "Key must not contain spaces", "type": "value_error"}])
    apply_settings(new)
    return settings_json(new)


@app.put("/api/settings")
async def put_settings(request: Request):
    try:
        new = Settings.model_validate(await request.json())
    except ValidationError as e:
        return _validation_response(e)
    apply_settings(new)
    return settings_json(new)


@app.post("/api/settings/reset")
async def reset_settings():
    extra_settings.clear()
    extra_settings.update(json.loads(json.dumps(EXTRA_DEFAULTS)))
    apply_settings(Settings())
    return settings_json()


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
async def camera_mjpeg(fps: float = 10.0):
    period = 1.0 / max(1.0, min(30.0, fps))

    async def gen():
        n = 0
        while True:
            jpg = await asyncio.to_thread(make_frame, n)
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            n += 1
            await asyncio.sleep(period)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/camera/snapshot.jpg")
async def camera_snapshot():
    return Response(await asyncio.to_thread(make_frame, 0), media_type="image/jpeg")


@app.post("/api/camera/whep")
async def camera_whep():
    return err(503, "WebRTC is not available in the mock server (MediaMTX is not running)")


# ── tracking (on the device: a proxy to fnav-relay; through /fnadmin/: fnav-relay itself) ──
def _track_guard():
    if TRACK_DOWN:
        return err(503, "Tracking server is unreachable")
    if journal is None:
        return err(503, "Tracking server is starting")
    return None


def _num(q: str | None) -> float | None:
    try:
        return float(q) if q not in (None, "") else None
    except ValueError:
        return None


@app.get("/api/tracking/places")
async def tracking_places(request: Request):
    if (r := _track_guard()) is not None:
        return r
    q = request.query_params
    return journal.places_json(_num(q.get("from")), _num(q.get("to")))  # type: ignore[union-attr]


@app.get("/api/tracking/places/{place_id}")
async def tracking_place(place_id: int):
    if (r := _track_guard()) is not None:
        return r
    d = journal.place_json(place_id)  # type: ignore[union-attr]
    return d if d is not None else err(404, "No such place")


@app.get("/api/tracking/track")
async def tracking_track(request: Request):
    if (r := _track_guard()) is not None:
        return r
    q = request.query_params
    return journal.track_json(_num(q.get("from")), _num(q.get("to")))  # type: ignore[union-attr]


@app.get("/api/tracking/last")
async def tracking_last():
    if (r := _track_guard()) is not None:
        return r
    if OFFLINE:
        return journal.last  # type: ignore[union-attr]
    # the device is online: its live position is also the server's last point
    g = hub.snapshot().gps
    if g.lat is None:
        return journal.last  # type: ignore[union-attr]
    t = time.time()
    return {"ts": t - 20, "lat": g.lat, "lon": g.lon, "speed_mps": g.speed_mps, "course_deg": g.course_deg, "received_ts": t - 18}


@app.post("/api/_mock/offline")
async def mock_offline(request: Request):
    """{"offline": true} — /fnadmin/ behaves as if the device dropped off (only the server answers)."""
    global OFFLINE
    b = await request.json() if (await request.body()) else {}
    OFFLINE = bool(b.get("offline", not OFFLINE))
    return {"ok": True, "offline": OFFLINE}


@app.post("/api/_mock/tracking-down")
async def mock_tracking_down(request: Request):
    global TRACK_DOWN
    b = await request.json() if (await request.body()) else {}
    TRACK_DOWN = bool(b.get("down", not TRACK_DOWN))
    return {"ok": True, "down": TRACK_DOWN}


# ── radio and Bluetooth (docs/contracts/radio.md): fake directory, fake player, fake BlueZ ──
GENRES = [
    ("relax", "🌊", "Calm", "mood"), ("sleep", "🌙", "Sleep", "mood"), ("lofi", "📚", "Study", "mood"),
    ("workout", "💪", "Workout", "mood"), ("party", "🎉", "Upbeat", "mood"), ("romantic", "💞", "Romantic", "mood"),
    ("meditation", "🧘", "Meditation", "mood"),
    ("pop", "🎤", "Pop", "genre"), ("rock", "🎸", "Rock", "genre"), ("jazz", "🎷", "Jazz", "genre"),
    ("classical", "🎻", "Classical", "genre"), ("electronic", "🎛️", "Electronic", "genre"),
    ("hip hop", "🎧", "Hip-hop", "genre"), ("chanson", "🪗", "Chanson", "genre"), ("lounge", "🍸", "Lounge", "genre"),
    ("dance", "💃", "Dance", "genre"), ("blues", "🎺", "Blues", "genre"), ("folk", "🪕", "Folk", "genre"),
    ("kids", "🧸", "Kids", "genre"),
    ("hits", "🔥", "Hits", "era"), ("oldies", "📻", "Oldies", "era"), ("80s", "📼", "80s", "era"),
    ("90s", "💿", "90s", "era"), ("news", "📰", "News", "era"),
]
GENRE_LABEL = {t: lab for t, _, lab, _ in GENRES}
_COUNTRIES = [("Poland", "PL", "polish"), ("Germany", "DE", "german"), ("United States", "US", "english"),
              ("United Kingdom", "GB", "english"), ("France", "FR", "french"), ("Netherlands", "NL", "dutch"),
              ("Ukraine", "UA", "ukrainian"), ("Italy", "IT", "italian")]
_NAME_PARTS = ["Wasteland", "Vault", "Desert", "Neon", "Atomic", "Radio", "Groove", "Night", "Highway", "Sunset",
               "Rust", "Echo", "Signal", "Mojave", "Midnight", "Coast"]
# made-up artists and titles for the fake ICY StreamTitle
_ARTISTS = ["The Rad Roaches", "Mojave Sisters", "Vault 13 Orchestra", "Dust Devils", "Neon Coyote",
            "Lola & the Geigers", "Brahmin Boys", "Static Kings"]
_TITLES = ["Highway to Nowhere", "Glowing in the Dark", "Last Gas Station", "Atomic Sunrise", "Rusty Heart",
           "Radio Silence", "Long Road Home", "Chrome & Sand", "Fallout Shelter Blues", "Midnight Convoy"]


def _station(tag: str, i: int) -> dict:
    rnd = random.Random(f"{tag}-{i}")
    country, cc, lang = rnd.choice(_COUNTRIES)
    label = GENRE_LABEL.get(tag, tag.title())
    name = f"{rnd.choice(_NAME_PARTS)} {label} {rnd.choice(['FM', 'Radio', '24/7', 'One', 'Live'])}"
    codec = rnd.choice(["MP3", "MP3", "AAC", "AAC+", "OGG"])
    tags = ",".join([tag, *rnd.sample(["music", "hits", "chill", "live", "classic", "indie", "talk", "local"], 2)])
    slug = tag.replace(" ", "")
    return {"uuid": f"{rnd.randrange(10**8):08d}-mock-{slug}-{i}", "name": name,
            "url": f"https://stream.example.invalid/{slug}/{i}" + ("/broken" if i == 11 else ""),
            "homepage": "", "favicon": "", "tags": tags, "country": country, "countrycode": cc, "language": lang,
            "codec": codec, "bitrate": rnd.choice([0, 64, 96, 128, 128, 192, 320]), "votes": rnd.randint(5, 9000),
            "clickcount": rnd.randint(50, 30000) // (i + 1), "hls": codec == "AAC+" and i % 5 == 0}


STATIONS = {t: [_station(t, i) for i in range(12)] for t, *_ in GENRES}


class FakeRadio:
    """Player state per the contract: stopped / connecting / playing / waiting_output / error."""

    def __init__(self) -> None:
        self.want = False  # a station is chosen and should be playing
        self.station: dict | None = None
        self.genre: str | None = None
        self.started = 0.0
        self.error: str | None = None
        self.recent: list[dict] = [STATIONS["jazz"][0], STATIONS["rock"][2], STATIONS["lofi"][1]]

    def volume(self) -> int:
        return int(extra_get("radio", "volume", 60))

    def state(self) -> str:
        if self.error:
            return "error"
        if not self.want or not self.station:
            return "stopped"
        if not bt.output_connected():
            return "waiting_output"
        return "connecting" if time.time() - self.started < 1.8 else "playing"

    def track(self) -> str | None:
        if self.state() != "playing" or not self.station:
            return None
        seed = int(self.station["uuid"][:8])
        if "news" in self.station["tags"] or seed % 4 == 0:
            return None  # this station sends no StreamTitle
        n = int((time.time() - self.started) // 25) + seed
        return f"{_ARTISTS[n % len(_ARTISTS)]} - {_TITLES[(n * 7) % len(_TITLES)]}"

    def play(self, st: dict, genre: str | None) -> None:
        self.station, self.genre, self.want, self.error = st, genre, True, None
        self.started = time.time()
        self.recent = [st] + [r for r in self.recent if r.get("uuid") != st.get("uuid")][:11]
        if str(st.get("url", "")).endswith("/broken"):
            self.error = "Stream could not be opened: HTTP 404"

    def full(self) -> dict:
        state = self.state()
        return {"state": state, "station": self.station, "genre": self.genre, "track": self.track(),
                "volume": self.volume(), "output": bt.output(), "error": self.error if state == "error" else None,
                "recent": self.recent}

    def compact(self) -> dict:
        out = bt.output()
        first_tag = ((self.station or {}).get("tags") or "").split(",")[0] or None
        return {"state": self.state(), "station": self.station["name"] if self.station else None,
                "track": self.track(), "genre": GENRE_LABEL.get(self.genre or "") or first_tag,
                "volume": self.volume(), "output": out["name"] if out else None,
                "output_connected": bool(out and out["connected"])}


class FakeBt:
    def __init__(self) -> None:
        self.available = True
        self.powered = True
        self.scan_until = 0.0
        self.audio_device: str | None = "41:42:1A:2B:3C:4D"
        self.devices: dict[str, dict] = {}
        self._add("41:42:1A:2B:3C:4D", "JBL Go 3", "audio-card", paired=True, trusted=True, rssi=-58)
        self.devices["41:42:1A:2B:3C:4D"]["connected"] = True
        self._add("C8:7B:23:AA:10:02", "Pixel 7", "phone", paired=True, trusted=True, rssi=-71)
        # these appear during a scan
        self._hidden = [("00:1A:7D:DA:71:13", "Car Kit BT", "audio-headset", -66),
                        ("F4:4E:FD:00:12:9A", "Soundcore Mini", "audio-card", -49),
                        ("7C:2E:0D:55:44:01", None, None, -88),
                        ("D0:03:4B:22:9F:10", "Living room TV", "video-display", -80),
                        ("E8:EC:A3:12:34:56", "Old speaker (fails)", "audio-card", -75),
                        ("5A:11:22:33:44:55", None, None, -93)]
        self.busy: dict[str, str] = {}

    def _add(self, mac, name, icon, paired=False, trusted=False, rssi=None) -> None:
        self.devices[mac] = {"address": mac, "name": name, "icon": icon, "paired": paired, "trusted": trusted,
                             "connected": False, "rssi": rssi, "audio": bool(icon and icon.startswith("audio"))}

    def discovering(self) -> bool:
        return self.available and self.powered and time.time() < self.scan_until

    def tick(self) -> None:
        if self.discovering() and self._hidden:
            left = self.scan_until - time.time()
            while self._hidden and len(self._hidden) > left / 2.5:
                mac, name, icon, rssi = self._hidden.pop(0)
                self._add(mac, name, icon, rssi=rssi)

    def output(self) -> dict | None:
        d = self.devices.get(self.audio_device or "")
        if not d:
            return None
        return {"address": d["address"], "name": d["name"] or d["address"],
                "connected": bool(d["connected"] and self.powered)}

    def output_connected(self) -> bool:
        o = self.output()
        return bool(o and o["connected"])

    def state(self) -> dict:
        self.tick()
        if not self.available:
            return {"available": False, "powered": False, "discovering": False, "audio_device": None, "devices": []}
        return {"available": True, "powered": self.powered, "discovering": self.discovering(),
                "audio_device": self.audio_device, "devices": list(self.devices.values())}


radio = FakeRadio()
bt = FakeBt()
BT_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

# fields the backend models may not have yet (radio.md §4): kept here until fnav.models grows them
EXTRA_DEFAULTS = {"radio": {"volume": 60, "resume_on_boot": True},
                  "bluetooth": {"enabled": True, "auto_connect": True},
                  "display": {"radio_panel": True}}
extra_settings: dict[str, dict] = json.loads(json.dumps(EXTRA_DEFAULTS))


def _model_has(section: str, key: str) -> bool:
    f = Settings.model_fields.get(section)
    sub = getattr(f.annotation, "model_fields", None) if f else None
    return bool(sub and key in sub)


def extra_get(section: str, key: str, default=None):
    if _model_has(section, key):
        return getattr(getattr(settings, section), key)
    return extra_settings.get(section, {}).get(key, default)


def settings_json(s: Settings | None = None) -> dict:
    d = (s or settings).model_dump(mode="json")
    for sec, vals in extra_settings.items():
        tgt = d.setdefault(sec, {})
        for k, v in vals.items():
            if not _model_has(sec, k):
                tgt[k] = v
    return d


def split_extra(patch: dict) -> JSONResponse | None:
    """Pull keys the model does not know out of a settings patch into extra_settings (validated here)."""
    for sec, vals in EXTRA_DEFAULTS.items():
        p = patch.get(sec)
        if not isinstance(p, dict):
            continue
        for k, dflt in vals.items():
            if k not in p or _model_has(sec, k):
                continue
            v = p.pop(k)
            if isinstance(dflt, bool):
                if not isinstance(v, bool):
                    return err(422, [{"loc": ["body", sec, k], "msg": "Input should be a valid boolean", "type": "bool_type"}])
            elif not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
                return err(422, [{"loc": ["body", sec, k], "msg": "Input should be between 0 and 100", "type": "int_range"}])
            extra_settings[sec][k] = v
        if not p and sec not in Settings.model_fields:
            patch.pop(sec)
    return None


@app.get("/api/radio/genres")
async def radio_genres():
    return {"genres": [{"tag": t, "emoji": e, "label": lab, "kind": k} for t, e, lab, k in GENRES]}


@app.get("/api/radio/stations")
async def radio_stations(tag: str | None = None, q: str | None = None, top: str | None = None):
    await asyncio.sleep(0.35)  # the directory is on the internet
    if RADIO_DIR_DOWN:
        return err(503, "Radio directory is unreachable")
    if tag:
        return {"stations": STATIONS.get(tag, [])}
    allst = [s for lst in STATIONS.values() for s in lst]
    if q:
        ql = q.strip().lower()
        return {"stations": [s for s in allst if ql in s["name"].lower()][:100]}
    pool = allst if not top or top == "world" else [s for s in allst if s["countrycode"] == top.upper()]
    return {"stations": sorted(pool, key=lambda s: -s["clickcount"])[:30]}


@app.get("/api/radio/state")
async def radio_state():
    return radio.full()


@app.post("/api/radio/play")
async def radio_play(request: Request):
    b = await request.json()
    st = b.get("station") if isinstance(b, dict) else None
    if not isinstance(st, dict) or not st.get("url") or not st.get("name"):
        return err(422, "station with name and url is required")
    radio.play(st, b.get("genre"))
    return radio.full()


@app.post("/api/radio/stop")
async def radio_stop():
    radio.want, radio.error = False, None
    return radio.full()


@app.post("/api/radio/resume")
async def radio_resume():
    if not radio.station:
        return err(409, "No station has been played yet")
    radio.play(radio.station, radio.genre)
    return radio.full()


@app.post("/api/radio/volume")
async def radio_volume(request: Request):
    v = (await request.json()).get("volume")
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
        return err(422, "volume must be an integer 0..100")
    if _model_has("radio", "volume"):
        data = settings.model_dump(mode="json")
        data["radio"]["volume"] = v
        apply_settings(Settings.model_validate(data))
    else:
        extra_settings["radio"]["volume"] = v
    return radio.full()


def _bt_guard(mac: str | None = None):
    if not bt.available:
        return err(503, "Bluetooth adapter not available")
    if mac is not None:
        if not BT_MAC.match(mac):
            return err(422, "Invalid Bluetooth address")
        if mac not in bt.devices:
            return err(404, "Device not found")
    return None


@app.get("/api/bluetooth/state")
async def bt_state():
    return bt.state()


@app.post("/api/bluetooth/power")
async def bt_power(request: Request):
    if (r := _bt_guard()) is not None:
        return r
    bt.powered = bool((await request.json()).get("on"))
    if not bt.powered:
        bt.scan_until = 0.0
        for d in bt.devices.values():
            d["connected"] = False
    return bt.state()


@app.post("/api/bluetooth/scan")
async def bt_scan(request: Request):
    if (r := _bt_guard()) is not None:
        return r
    if not bt.powered:
        return err(409, "Bluetooth is off")
    secs = (await request.json()).get("seconds", 15)
    if not isinstance(secs, (int, float)) or not 1 <= secs <= 60:
        return err(422, "seconds must be 1..60")
    bt.scan_until = time.time() + float(secs)
    return bt.state()


@app.post("/api/bluetooth/devices/{mac}/connect")
async def bt_connect(mac: str):
    mac = mac.upper()
    if (r := _bt_guard(mac)) is not None:
        return r
    if not bt.powered:
        return err(409, "Bluetooth is off")
    if mac in bt.busy:
        return err(409, "Already in progress")
    d = bt.devices[mac]
    try:
        if not d["paired"]:
            bt.busy[mac] = "pairing"
            await asyncio.sleep(4.0)
            if "fails" in (d["name"] or ""):
                return err(502, "Pairing failed: Authentication Rejected (is the speaker in pairing mode?)")
            d["paired"] = d["trusted"] = True
        bt.busy[mac] = "connecting"
        await asyncio.sleep(2.0)
        d["connected"] = True
        if d["audio"]:
            old = bt.devices.get(bt.audio_device or "")
            if old is not None and old is not d:
                old["connected"] = False  # one speaker at a time
            bt.audio_device = mac
            if radio.want:
                radio.started = time.time()
    finally:
        bt.busy.pop(mac, None)
    return bt.state()


@app.post("/api/bluetooth/devices/{mac}/disconnect")
async def bt_disconnect(mac: str):
    mac = mac.upper()
    if (r := _bt_guard(mac)) is not None:
        return r
    await asyncio.sleep(0.6)
    bt.devices[mac]["connected"] = False
    return bt.state()


@app.delete("/api/bluetooth/devices/{mac}")
async def bt_forget(mac: str):
    mac = mac.upper()
    if (r := _bt_guard(mac)) is not None:
        return r
    bt.devices.pop(mac)
    if bt.audio_device == mac:
        bt.audio_device = None
    return bt.state()


@app.post("/api/_mock/bt")
async def mock_bt(request: Request):
    """{"available": false} — no adapter; {"dir_down": true} — the radio directory answers 503."""
    global RADIO_DIR_DOWN
    b = await request.json() if (await request.body()) else {}
    if "available" in b:
        bt.available = bool(b["available"])
    if "dir_down" in b:
        RADIO_DIR_DOWN = bool(b["dir_down"])
    return {"ok": True, "available": bt.available, "dir_down": RADIO_DIR_DOWN}


# ── live MPEG-TS (fnav-camera /live.ts: a separate low-bitrate encode, only while someone watches) ──
class _TsPipe(io.RawIOBase):
    def __init__(self, q: queue.Queue[bytes]) -> None:
        self.q = q

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:  # type: ignore[override]
        try:
            self.q.put(bytes(b), timeout=2)
        except queue.Full:
            pass
        return len(b)


def _live_encoder(q: queue.Queue[bytes], stop: threading.Event) -> None:
    """libx264 768×576 at 15 fps in real time → MPEG-TS packets into the queue."""
    from fractions import Fraction

    import av
    import numpy as np

    out = av.open(_TsPipe(q), "w", format="mpegts")
    st = out.add_stream("libx264", rate=15)
    st.width, st.height, st.pix_fmt = 768, 576, "yuv420p"
    st.bit_rate = 800_000
    st.options = {"g": "15", "preset": "ultrafast", "bf": "0", "tune": "zerolatency"}
    xs = np.arange(768)[None, :]
    ys = np.arange(576)[:, None]
    t0 = time.monotonic()
    i = 0
    try:
        while not stop.is_set():
            arr = np.zeros((576, 768, 3), dtype=np.uint8)
            arr[:, :, 1] = ((xs // 48 + ys // 48) % 2 * 40 + 30).astype(np.uint8)
            x = (i * 9) % 768
            arr[250:330, max(0, x - 30) : x + 30, 0] = 255
            arr[250:330, max(0, x - 30) : x + 30, 1] = 182
            arr[:40, : (i % 15 + 1) * 51, 2] = 200  # a 1-second "clock" bar
            fr = av.VideoFrame.from_ndarray(arr, format="rgb24")
            fr.pts, fr.time_base = i, Fraction(1, 15)
            for p in st.encode(fr):
                out.mux(p)
            i += 1
            delay = t0 + i / 15 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    finally:
        with contextlib.suppress(Exception):  # the client is gone, the pipe may be full
            out.close()


@app.get("/api/camera/live.ts")
async def camera_live_ts():
    try:
        import av  # noqa: F401
    except ImportError:
        return err(503, "Live stream is not available in the mock server (PyAV is not installed)")
    q: queue.Queue[bytes] = queue.Queue(maxsize=400)
    stop = threading.Event()
    threading.Thread(target=_live_encoder, args=(q, stop), daemon=True).start()

    async def gen():
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(q.get, True, 5.0)
                except queue.Empty:
                    return
                yield chunk
        finally:
            stop.set()

    return StreamingResponse(gen(), media_type="video/mp2t", headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


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
    if is_remote(ws) and OFFLINE:
        await ws.close(code=1013)
        return
    local = (not STRICT_AUTH) and not is_remote(ws) and ws.client is not None and ws.client.host in ("127.0.0.1", "::1")
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
                await ws.send_text(json.dumps({"type": "settings", "settings": settings_json()}))
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


class RemotePrefix:
    """/fnadmin/x → /x with scope["fnav_remote"] (what nginx on the server + the tunnel do); /fnadmin → admin."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if scope["type"] == "http" and path in (REMOTE_PREFIX, REMOTE_PREFIX + "/"):
                await send({"type": "http.response.start", "status": 301, "headers": [(b"location", (REMOTE_PREFIX + "/admin/").encode())]})
                await send({"type": "http.response.body", "body": b""})
                return
            if path.startswith(REMOTE_PREFIX + "/"):
                scope = dict(scope)
                scope["path"] = path[len(REMOTE_PREFIX):]
                scope["raw_path"] = scope["path"].encode()
                scope["fnav_remote"] = True
        await self.inner(scope, receive, send)


def main() -> None:
    global STRICT_AUTH, OFFLINE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--strict-auth", action="store_true", help="require a session even from loopback (to test the UI auth flow)")
    ap.add_argument("--offline", action="store_true", help=f"under {REMOTE_PREFIX}/ act as if the device were not connected")
    a = ap.parse_args()
    STRICT_AUTH = a.strict_auth
    OFFLINE = a.offline
    print(f"fnav web UI mock: http://{a.host}:{a.port}/admin/   (password: {PASSWORD})")
    print(f"  remote access:   http://{a.host}:{a.port}{REMOTE_PREFIX}/" + ("   (device offline)" if OFFLINE else ""))
    uvicorn.run(RemotePrefix(app), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
