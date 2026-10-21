import asyncio
import json
import time

import httpx
import pytest
from core_helpers import FakeUpstream, login, make_opts, running_app
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


async def _recv_json(ws, timeout=3.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def test_ws_telemetry_rate_settings_nmea(live_server):
    srv = live_server()
    async with connect(srv.ws_url) as ws:  # с 127.0.0.1 — без входа (киоск)
        first = await _recv_json(ws)
        assert first["type"] == "settings" and first["settings"]["display"]["orientation"] == "heading_up"
        second = await _recv_json(ws)
        assert second["type"] == "telemetry"
        for key in ("t", "gps", "heading", "display", "camera", "disk", "system", "errors"):
            assert key in second
        assert second["camera"]["state"] == "recording"  # mock-камера

        # частота ~10 Гц
        t0 = time.monotonic()
        n = 0
        while time.monotonic() - t0 < 2.0:
            m = await _recv_json(ws)
            if m["type"] == "telemetry":
                n += 1
        assert 16 <= n <= 24, n

        # подписка на NMEA
        await ws.send(json.dumps({"type": "subscribe", "topics": ["nmea"]}))
        got = None
        for _ in range(40):
            m = await _recv_json(ws)
            if m["type"] == "nmea":
                got = m
                break
        assert got and got["lines"] and got["lines"][0].startswith("$GNRMC")

        # изменение настроек → сообщение settings
        async with httpx.AsyncClient(base_url=srv.url) as c:
            await login(c)
            r = await c.patch("/api/settings", json={"display": {"orientation": "north_up"}})
            assert r.status_code == 200
        for _ in range(60):
            m = await _recv_json(ws)
            if m["type"] == "settings":
                assert m["settings"]["display"]["orientation"] == "north_up"
                break
        else:
            pytest.fail("no settings message")
        # и телеметрия теперь с north_up
        for _ in range(10):
            m = await _recv_json(ws)
            if m["type"] == "telemetry":
                assert m["display"]["orientation"] == "north_up"
                break

        # событие кнопки (mock)
        async with httpx.AsyncClient(base_url=srv.url) as c:
            await login(c)
            await c.post("/api/mock/button", json={"event": "long_press"})
        events = set()
        for _ in range(30):
            m = await _recv_json(ws)
            if m["type"] == "event":
                events.add(m["event"])
            if {"button", "protected", "toast"} <= events:
                break
        assert {"button", "protected", "toast"} <= events


async def test_ws_requires_auth_when_remote(live_server):
    srv = live_server(trust_localhost=False)
    with pytest.raises(InvalidStatus):
        async with connect(srv.ws_url):
            pass
    async with httpx.AsyncClient(base_url=srv.url) as c:
        await login(c)
        cookie = c.cookies.get("fnav_session")
    async with connect(srv.ws_url, additional_headers={"Cookie": f"fnav_session={cookie}"}) as ws:
        assert (await _recv_json(ws))["type"] == "settings"


async def test_mjpeg_stream_mock(live_server):
    srv = live_server()
    async with httpx.AsyncClient(base_url=srv.url, timeout=5) as c:
        async with c.stream("GET", "/api/camera/mjpeg") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
            buf = b""
            t0 = time.monotonic()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if buf.count(b"--frame") >= 6 or time.monotonic() - t0 > 4:
                    break
        assert buf.count(b"--frame") >= 6
        assert b"Content-Length: 0\r\n" not in buf
        first = buf.index(b"\xff\xd8")
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(buf[first:buf.index(b"\xff\xd9", first) + 2]))
        assert img.size == (768, 576)
        r = await c.get("/api/camera/snapshot.jpg")
        assert r.status_code == 401  # snapshot с localhost — только с сессией
        await login(c)
        r = await c.get("/api/camera/snapshot.jpg")
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"


async def test_camera_proxy_and_whep(data_dir):
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.port == 8081 and req.url.path == "/status":
            return httpx.Response(200, json={"state": "recording", "recording": True,
                                             "current_segment": "2026-10-20/x.ts"})
        if req.url.port == 8081 and req.url.path == "/snapshot.jpg":
            return httpx.Response(200, content=b"\xff\xd8real\xff\xd9", headers={"content-type": "image/jpeg"})
        if req.url.port == 8889 and req.method == "POST" and req.url.path == "/cam/whep":
            assert req.headers["content-type"] == "application/sdp"
            assert req.content == b"v=0 offer"
            return httpx.Response(201, content=b"v=0 answer", headers={
                "content-type": "application/sdp", "location": "/cam/whep/abc-123", "etag": "E1",
                "link": '<stun:stun.l.google.com:19302>; rel="ice-server"'})
        if req.url.port == 8889 and req.method == "PATCH" and req.url.path == "/cam/whep/abc-123":
            assert req.headers["if-match"] == "*"
            return httpx.Response(204)
        if req.url.port == 8889 and req.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(404)

    up = FakeUpstream(h)
    async with running_app(make_opts(local_transport=up.transport)) as c:
        await login(c)
        st = (await c.get("/api/camera/status")).json()
        assert st["state"] == "recording" and st["mock"] is False
        assert (await c.get("/api/camera/snapshot.jpg")).content == b"\xff\xd8real\xff\xd9"
        r = await c.post("/api/camera/whep", content=b"v=0 offer", headers={"content-type": "application/sdp"})
        assert r.status_code == 201 and r.content == b"v=0 answer"
        assert r.headers["location"] == "/api/camera/whep/abc-123"
        assert r.headers["etag"] == "E1" and "ice-server" in r.headers["link"]
        r = await c.patch("/api/camera/whep/abc-123", content=b"a=candidate",
                          headers={"content-type": "application/trickle-ice-sdpfrag", "if-match": "*"})
        assert r.status_code == 204
        assert (await c.delete("/api/camera/whep/abc-123")).status_code == 200


async def test_whep_unavailable(data_dir):
    async with running_app() as c:
        await login(c)
        r = await c.post("/api/camera/whep", content=b"v=0", headers={"content-type": "application/sdp"})
        assert r.status_code == 503 and r.json()["detail"]


async def test_system_endpoints_mock(data_dir):
    async with running_app() as c:
        await login(c)
        st = (await c.get("/api/system/status")).json()
        assert set(st) >= {"system", "disk", "services", "version"}
        assert st["disk"]["total_gb"] > 0
        logs = (await c.get("/api/system/logs?unit=fnav-core&lines=10")).json()
        assert isinstance(logs["lines"], list)
        assert (await c.get("/api/system/logs?unit=sshd")).status_code == 400
        assert (await c.post("/api/system/reboot")).json() == {"ok": True, "mock": True}
        assert (await c.post("/api/system/shutdown")).json() == {"ok": True, "mock": True}
        wifi = (await c.get("/api/system/wifi")).json()
        assert wifi and {"ssid", "priority", "active", "signal_pct"} <= set(wifi[0])
        r = await c.post("/api/system/wifi", json={"ssid": "Home", "password": "12345678", "priority": 5})
        assert r.json()["ok"] is True
        r = await c.post("/api/system/wifi", json={"ssid": "Home", "password": "123", "priority": 5})
        assert r.status_code == 400
        assert (await c.delete("/api/system/wifi/Home")).json()["ok"] is True


def test_nmcli_split():
    from fnav.core.system import nmcli_split

    assert nmcli_split(r"yes:My\:Net:72") == ["yes", "My:Net", "72"]
    assert nmcli_split(r"a\\b:c") == ["a\\b", "c"]


def test_new_nmea_lines():
    from fnav.core.ws import new_nmea_lines

    tail = [f"$L{i}" for i in range(100)]
    assert new_nmea_lines(tail, None) == tail[-20:]
    assert new_nmea_lines(tail, "$L97") == ["$L98", "$L99"]
    assert new_nmea_lines(tail, "$L99") == []
    assert new_nmea_lines(tail, "gone") == tail[-50:]
