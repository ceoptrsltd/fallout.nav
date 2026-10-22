"""Журнал позиций на устройстве (outbox → fnav-relay) и «удалённый» вход через туннель."""

from __future__ import annotations

import asyncio
import json

import httpx
from core_helpers import FakeUpstream, login, make_opts

from fnav.core.app import create_app
from fnav.core.tracking import TOKEN_HEADER, Outbox, RelayConfig, TrackService, make_sample
from fnav.models import GpsFix, TrackingSettings

CFG = TrackingSettings()


def fix(**kw) -> GpsFix:
    base = {"fix": "3d", "lat": 50.45, "lon": 30.52, "speed_mps": 3.0, "course_deg": 90.0, "hdop": 0.9,
            "time_utc": "2026-10-22T10:00:00.000Z", "age_s": 0.2}
    return GpsFix(**{**base, **kw})


def test_make_sample_filters():
    s = make_sample(fix(), CFG)
    assert s is not None and s["ts"] == 1792663200.0 and s["fix"] == "3d" and s["lat"] == 50.45
    assert make_sample(fix(fix="none"), CFG) is None
    assert make_sample(fix(lat=None), CFG) is None
    assert make_sample(fix(age_s=12.0), CFG) is None
    assert make_sample(fix(hdop=7.5), CFG) is None
    # без времени GPS — системные часы, если они выставлены; иначе точку не пишем
    assert make_sample(fix(time_utc=None), CFG, now=1792663260.0)["ts"] == 1792663260.0
    assert make_sample(fix(time_utc=None), CFG, now=1000.0) is None


def test_outbox_order_and_cap(tmp_path):
    ob = Outbox(tmp_path / "o.db", max_rows=3)
    for i in range(5):
        ob.add({"ts": 100.0 + i})
    rows = ob.peek(10)
    assert [s["ts"] for _, s in rows] == [102.0, 103.0, 104.0] and ob.count() == 3
    ob.drop_upto(rows[1][0])
    assert [s["ts"] for _, s in ob.peek(10)] == [104.0]
    ob.close()
    assert Outbox(tmp_path / "o.db").count() == 1  # переживает перезапуск


class FakeRelay:
    def __init__(self) -> None:
        self.samples: list[dict] = []
        self.auth: dict | None = None
        self.up = True

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=req)
        if req.headers.get(TOKEN_HEADER) != "tok":
            return httpx.Response(401, json={"detail": "bad token"})
        if req.url.path == "/device/auth":
            self.auth = json.loads(req.content)
            return httpx.Response(200, json={"ok": True})
        if req.url.path == "/device/samples":
            got = json.loads(req.content)["samples"]
            self.samples += got
            return httpx.Response(200, json={"ok": True, "accepted": len(got)})
        if req.url.path.startswith("/api/tracking/"):
            return httpx.Response(200, json={"path": req.url.path, "q": req.url.query.decode()})
        return httpx.Response(404)


async def test_upload_retries_and_syncs_auth(tmp_path):
    relay = FakeRelay()
    svc = TrackService(tmp_path / "o.db", RelayConfig("http://relay", "tok"), lambda: ("scrypt$h", "sec"),
                       transport=httpx.MockTransport(relay))
    relay.up = False
    await svc.record(make_sample(fix(), CFG))
    try:
        await svc.upload_once()
    except httpx.HTTPError:
        pass
    assert svc.outbox.count() == 1 and relay.samples == []
    relay.up = True
    await svc.record(make_sample(fix(time_utc="2026-10-22T10:01:00Z"), CFG))
    await svc.upload_once()
    assert [s["ts"] for s in relay.samples] == [1792663200.0, 1792663260.0]
    assert relay.auth == {"password_hash": "scrypt$h", "session_secret": "sec"}
    assert svc.outbox.count() == 0
    code, body, _ = await svc.proxy_get("places", "from=1&to=2")
    assert code == 200 and json.loads(body) == {"path": "/api/tracking/places", "q": "from=1&to=2"}
    relay.up = False
    code, body, _ = await svc.proxy_get("last", "")
    assert code == 503 and json.loads(body)["detail"] == "Tracking server is unreachable"
    await svc.stop()


async def test_not_configured(tmp_path):
    svc = TrackService(tmp_path / "o.db", RelayConfig(), lambda: (None, ""))
    code, body, _ = await svc.proxy_get("places", "")
    assert code == 503 and "not configured" in json.loads(body)["detail"]
    assert (await svc.status())["configured"] is False
    await svc.stop()


async def test_sample_loop_interval(tmp_path):
    svc = TrackService(tmp_path / "o.db", RelayConfig(), lambda: (None, ""))
    svc.start(lambda: fix(), lambda: TrackingSettings(interval_s=10))
    await asyncio.sleep(0.3)
    assert svc.outbox.count() == 1  # первая точка — сразу, следующая — через interval_s
    await svc.stop()


def _client(app, port: int, peer=("127.0.0.1", 40000)) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url=f"http://testserver:{port}")


async def test_remote_port_has_no_localhost_trust(data_dir):
    app = create_app(make_opts(remote_port=8090))
    async with app.router.lifespan_context(app):
        async with _client(app, 80) as local:  # киоск: localhost, обычный порт
            assert (await local.get("/api/settings")).status_code == 200
            assert (await local.get("/api/session")).json()["remote"] is False
        async with _client(app, 8090) as remote:  # туннель: тоже localhost, но удалённый порт
            assert (await remote.get("/api/settings")).status_code == 401
            assert (await remote.get("/api/session")).json() == {
                "authenticated": False, "local": False, "remote": True, "mock": True}
            # анти-перебор — по X-Real-IP от nginx, а не по 127.0.0.1 туннеля
            auth = app.state.core.auth
            r = await remote.post("/api/login", json={"password": "bad"}, headers={"X-Real-IP": "203.0.113.7"})
            assert r.status_code == 401 and "203.0.113.7" in auth._attempts and "127.0.0.1" not in auth._attempts
            await login(remote)
            assert (await remote.get("/api/settings")).status_code == 200


async def test_tracking_proxy_route(data_dir):
    relay = FakeRelay()
    app = create_app(make_opts(relay=RelayConfig("http://relay", "tok"),
                               relay_transport=httpx.MockTransport(relay)))
    async with app.router.lifespan_context(app), _client(app, 80, peer=("10.0.0.5", 5000)) as c:
        assert (await c.get("/api/tracking/places")).status_code == 401  # только после входа
        await login(c)
        r = await c.get("/api/tracking/places", params={"from": 5})
        assert r.status_code == 200 and r.json()["q"] == "from=5"
        assert (await c.get("/api/remote/status")).json()["configured"] is True
    relay2 = FakeUpstream()  # сервер недоступен
    app = create_app(make_opts(relay=RelayConfig("http://relay", "tok"), relay_transport=relay2.transport))
    async with app.router.lifespan_context(app), _client(app, 80, peer=("10.0.0.5", 5000)) as c:
        await login(c)
        r = await c.get("/api/tracking/last")
        assert r.status_code == 503 and r.json()["detail"] == "Tracking server is unreachable"
