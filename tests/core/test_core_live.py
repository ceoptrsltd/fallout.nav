"""fnav-core: /api/camera/live.ts (прокси живого видео и mock) и ?fps для MJPEG."""

from __future__ import annotations

import httpx
from core_helpers import FakeUpstream, login, make_opts, running_app

TS1 = bytes([0x47]) + bytes(187)  # один TS-пакет


async def chunks(data: bytes):  # потоковое тело (bytes в httpx.Response считаются уже прочитанными)
    yield data


async def test_live_ts_mock_requires_login_and_streams(live_server):
    srv = live_server()
    async with httpx.AsyncClient(base_url=srv.url, timeout=10) as c:
        r = await c.get("/api/camera/live.ts")
        assert r.status_code == 401  # живое видео — только с сессией, даже с localhost
        await login(c)
        async with c.stream("GET", "/api/camera/live.ts") as r:
            assert r.status_code == 200 and r.headers["content-type"] == "video/mp2t"
            buf = b""
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > 30_000:
                    break
        assert buf[0] == 0x47 and ((buf[1] & 0x1F) << 8 | buf[2]) == 0  # PAT
        assert all(buf[i] == 0x47 for i in range(0, len(buf) - 188, 188))


async def test_live_ts_proxy_and_mjpeg_fps(data_dir):
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.port == 8081 and req.url.path == "/status":
            return httpx.Response(200, json={"state": "recording", "recording": True})
        if req.url.port == 8081 and req.url.path == "/live.ts":
            return httpx.Response(200, content=chunks(TS1), headers={"content-type": "video/mp2t"})
        if req.url.port == 8081 and req.url.path == "/mjpeg":
            return httpx.Response(200, content=chunks(b"--frame\r\n"),
                                  headers={"content-type": "multipart/x-mixed-replace; boundary=frame"})
        return httpx.Response(404)

    up = FakeUpstream(h)
    async with running_app(make_opts(local_transport=up.transport)) as c:
        await login(c)
        await c.get("/api/camera/status")
        r = await c.get("/api/camera/live.ts")
        assert r.status_code == 200 and r.content == TS1
        assert r.headers["content-type"] == "video/mp2t" and "no-store" in r.headers["cache-control"]
        await c.get("/api/camera/mjpeg?fps=5")
        await c.get("/api/camera/mjpeg?fps=500")
        await c.get("/api/camera/mjpeg?fps=x")
    mj = [q.url.params.get("fps") for q in up.requests if q.url.path == "/mjpeg"]
    assert mj == ["5", "30", None]


async def test_live_ts_camera_error_is_503(data_dir):
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/status":
            return httpx.Response(200, json={"state": "recording", "recording": True})
        if req.url.path == "/live.ts":
            return httpx.Response(503, json={"ok": False, "error": "Live stream: no second encoder"})
        return httpx.Response(404)

    async with running_app(make_opts(local_transport=FakeUpstream(h).transport)) as c:
        await login(c)
        await c.get("/api/camera/status")
        r = await c.get("/api/camera/live.ts")
        assert r.status_code == 503 and r.json()["detail"] == "Live stream: no second encoder"
