"""Гостевой доступ «только просмотр» (U63): ссылки, обмен токена на cookie, 403 на всё, что меняет состояние
(по всем маршрутам приложения — новые маршруты проверяются сами), команды WebSocket, секреты, срок, отзыв, анти-перебор.
Плюс B7: мусор (не-ASCII) в cookie — 401, а не 500."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from core_helpers import login, make_opts, running_app
from fastapi.routing import APIRoute
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from fnav import paths
from fnav.core import auth as auth_mod
from fnav.core import guests as guests_mod
from fnav.core import secretsfile as sf
from fnav.core.auth import GUEST_DENIED, guest_allowed, is_public_path
from fnav.core.tracking import RelayConfig
from fnav.core.ws import Client, WsManager

READ = {"GET", "HEAD", "OPTIONS"}
# не чтение, но гостю можно (auth.guest_allowed): живое видео WebRTC (WHEP) и выход из своей сессии
EXPECTED_GUEST_WRITES = {
    ("POST", "/api/logout"),
    ("POST", "/api/camera/whep"), ("PATCH", "/api/camera/whep/{subpath:path}"),
    ("DELETE", "/api/camera/whep/{subpath:path}"),
    ("POST", "/api/camera/rear/whep"), ("PATCH", "/api/camera/rear/whep/{subpath:path}"),
    ("DELETE", "/api/camera/rear/whep/{subpath:path}"),
}
# открыты всем (вход паролем, обмен гостевой ссылки): гость там — просто посетитель без входа
EXPECTED_PUBLIC_WRITES = {("POST", "/api/login"), ("POST", "/api/guest/login")}
DAY = 86400


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)(:path)?\}", lambda m: "a/b.ts" if m.group(2) else "x1", path)


@asynccontextmanager
async def client_of(app, addr=("10.0.0.9", 50001), port: int | None = None):
    base = f"http://testserver:{port}" if port else "http://testserver"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=addr), base_url=base) as c:
        yield c


async def new_link(owner: httpx.AsyncClient, ttl: int = DAY, label: str = "Anna") -> dict:
    r = await owner.post("/api/guest/links", json={"label": label, "expires_in_s": ttl})
    assert r.status_code == 200, r.text
    return r.json()


async def as_guest(c: httpx.AsyncClient, token: str) -> httpx.Response:
    r = await c.post("/api/guest/login", json={"token": token})
    assert r.status_code == 200, r.text
    return r


# ── ссылки и сессия ──


async def test_guest_link_flow_session_and_revoke(data_dir):
    async with running_app() as owner:
        await login(owner)
        res = await new_link(owner)
        token, link = res["token"], res["link"]
        assert res["path"] == f"admin/login.html#guest={token}" and link["label"] == "Anna"
        assert link["expires"] - time.time() == pytest.approx(DAY, abs=5) and link["last_used"] is None
        # на диске — только хэш токена, 0600 как settings.json
        text = (data_dir / guests_mod.FILE_NAME).read_text(encoding="utf-8")
        assert token not in text and guests_mod.token_hash(token) in text

        async with client_of(owner.app) as g:
            r = await as_guest(g, token)
            raw = r.headers["set-cookie"]
            assert "fnav_session=g1." in raw and "HttpOnly" in raw and "Path=/" in raw
            assert f"Max-Age={DAY - 1}" in raw or f"Max-Age={DAY}" in raw  # до конца срока ссылки
            s = (await g.get("/api/session")).json()
            assert s["authenticated"] is True and s["guest"] is True
            assert s["guest_expires"] == link["expires"] and "timezone" in s
            for url in ("/api/settings", "/api/system/status", "/api/recordings", "/api/alarm", "/api/telemetry",
                        "/api/camera/status", "/api/remote/status", "/api/system/wifi"):
                assert (await g.get(url)).status_code == 200, url
            assert (await g.get("/admin/index.html")).status_code in (200, 404)  # не редирект на вход

            # владелец видит ссылку с «последним входом»; гостю список ссылок не положен
            lst = (await owner.get("/api/guest/links")).json()
            assert [x["id"] for x in lst["links"]] == [link["id"]] and lst["links"][0]["last_used"]
            assert token not in json.dumps(lst) and guests_mod.token_hash(token) not in json.dumps(lst)
            r = await g.get("/api/guest/links")
            assert r.status_code == 403 and r.json()["detail"] == GUEST_DENIED

            # отзыв действует сразу: cookie гостя больше не пускает, ссылку больше не обменять
            r = await owner.delete(f"/api/guest/links/{link['id']}")
            assert r.status_code == 200 and r.json()["links"] == []
            assert (await owner.delete(f"/api/guest/links/{link['id']}")).status_code == 404
            assert (await g.get("/api/settings")).status_code == 401
            assert (await g.get("/api/session")).json()["authenticated"] is False
            r = await g.post("/api/guest/login", json={"token": token})
            assert r.status_code == 401 and "revoked" in r.json()["detail"]
        # владелец не задет
        s = (await owner.get("/api/session")).json()
        assert s["authenticated"] is True and s["guest"] is False


async def test_guest_link_expiry(data_dir):
    async with running_app() as owner:
        await login(owner)
        r = await owner.post("/api/guest/links", json={"expires_in_s": 60})
        assert r.status_code == 422  # меньше 10 мин
        r = await owner.post("/api/guest/links", json={"expires_in_s": 400 * DAY})
        assert r.status_code == 422
        res = await new_link(owner, ttl=3600)
        guests = owner.app.state.core.guests
        async with client_of(owner.app) as g:
            await as_guest(g, res["token"])
            assert (await g.get("/api/settings")).status_code == 200
            guests.links[res["link"]["id"]].expires = time.time() - 1  # срок вышел
            assert (await g.get("/api/settings")).status_code == 401
            assert (await g.post("/api/guest/login", json={"token": res["token"]})).status_code == 401
        assert (await owner.get("/api/guest/links")).json()["links"] == []  # истёкшие не показываются
        # истёкшая удаляется из файла при следующей записи
        await new_link(owner)
        text = (data_dir / guests_mod.FILE_NAME).read_text(encoding="utf-8")
        assert res["link"]["id"] not in text


async def test_guest_cookie_forgery_and_password_change(data_dir):
    async with running_app() as owner:
        await login(owner)
        res = await new_link(owner)
        a = owner.app.state.core.auth
        lid = res["link"]["id"]
        async with client_of(owner.app) as g:
            await as_guest(g, res["token"])
            good = g.cookies["fnav_session"]
        _ver, exp, _id, sig = good.split(".")
        async with client_of(owner.app) as anon:  # без своих cookie: только заголовок
            def get(cookie):
                return anon.get("/api/settings", headers={"Cookie": f"fnav_session={cookie}"})

            # чужой id, продлённый срок, подпись как у владельца, ссылка, которой нет, — не пускают
            for bad in (f"g1.{exp}.ffffffffffff.{sig}", f"g1.{int(exp) + 999}.{lid}.{sig}",
                        f"v1.{exp}.{lid}.{sig}", a.issue_guest_token("ffffffffffff", time.time() + 99)):
                assert (await get(bad)).status_code == 401, bad
            assert (await get(good)).status_code == 200
            # смена пароля владельцем сбрасывает и гостевые cookie (ссылка остаётся — её можно открыть снова)
            r = await owner.post("/api/system/password", json={"old": "fnav-dev", "new": "new-password-1"})
            assert r.status_code == 200
            assert (await get(good)).status_code == 401
        async with client_of(owner.app) as g:
            await as_guest(g, res["token"])
            assert (await g.get("/api/settings")).status_code == 200


# ── 403 на всё, что меняет состояние ──


async def test_every_mutating_route_refused_for_guest(data_dir):
    """Обходит маршруты самого приложения: новый POST/PUT/PATCH/DELETE закрыт для гостя без правок этого теста."""
    async with running_app() as owner:  # --mock: и маршруты, которые есть только в mock
        await login(owner)
        res = await new_link(owner)
        async with client_of(owner.app) as g:
            await as_guest(g, res["token"])
            checked, allowed, public = 0, set(), set()
            for route in owner.app.routes:
                if not isinstance(route, APIRoute):
                    continue
                for method in sorted(route.methods - READ):
                    path = _concrete(route.path)
                    if is_public_path(path, method):
                        public.add((method, route.path))
                        continue
                    if guest_allowed(path, method):
                        allowed.add((method, route.path))
                        continue
                    r = await g.request(method, path, json={})
                    assert r.status_code == 403, (method, route.path, r.status_code, r.text)
                    assert r.json() == {"detail": GUEST_DENIED}
                    checked += 1
            assert checked >= 50, checked  # 53 на 2026-11
            assert allowed == EXPECTED_GUEST_WRITES  # исключения — только осознанные
            assert public == EXPECTED_PUBLIC_WRITES
            # то же для путей, которых нет: middleware отказывает раньше маршрутизации
            assert (await g.post("/api/new/thing", json={})).status_code == 403
            assert (await g.put("/api/settings", json={})).status_code == 403
            # ничего не изменилось
            assert (await owner.get("/api/guest/links")).json()["links"][0]["id"] == res["link"]["id"]


async def test_guest_cannot_change_password_create_links_or_log_out_the_owner(data_dir):
    async with running_app() as owner:
        await login(owner)
        res = await new_link(owner)
        async with client_of(owner.app) as g:
            await as_guest(g, res["token"])
            r = await g.post("/api/system/password", json={"old": "fnav-dev", "new": "hacked-123"})
            assert r.status_code == 403
            assert (await g.post("/api/guest/links", json={"expires_in_s": DAY})).status_code == 403
            assert (await g.delete(f"/api/guest/links/{res['link']['id']}")).status_code == 403
            assert (await g.patch("/api/settings", json={"display": {"orientation": "north_up"}})).status_code == 403
            # выход гостя — только его cookie
            r = await g.post("/api/logout")
            assert r.status_code == 200 and "fnav_session=" in r.headers["set-cookie"]
        assert sf.verify_password("fnav-dev", sf.read_secrets(paths.secrets_file())[sf.KEY_HASH])
        assert (await owner.get("/api/settings")).json()["display"]["orientation"] == "heading_up"
        assert (await owner.get("/api/session")).json()["authenticated"] is True


async def test_guest_link_attempts_rate_limited_like_logins(data_dir):
    async with running_app() as owner:
        await login(owner)
        res = await new_link(owner)
        async with client_of(owner.app, addr=("10.0.0.77", 50002)) as g:
            for _ in range(auth_mod.MAX_FAILURES - 1):
                r = await g.post("/api/guest/login", json={"token": "x" * 43})
                assert r.status_code == 401
            # действующая ссылка не снимает счётчик (иначе ею можно было бы перебирать пароль без пауз)
            await as_guest(g, res["token"])
            assert (await g.post("/api/guest/login", json={"token": "y" * 43})).status_code == 401
            r = await g.post("/api/guest/login", json={"token": res["token"]})
            assert r.status_code == 429 and "Retry-After" in r.headers
            # тот же счётчик, что у входа паролем
            assert (await g.post("/api/login", json={"password": "fnav-dev"})).status_code == 429
            assert (await g.post("/api/guest/login", json={"token": ""})).status_code == 422


# ── секреты ──


async def test_guest_gets_no_secrets(data_dir):
    relay = RelayConfig("http://127.0.0.1:9", "relay-device-token-xyz")
    async with running_app(make_opts(relay=relay)) as owner:
        await login(owner)
        r = await owner.patch("/api/settings", json={
            "tiles": {"esri_api_key": "ESRI-SECRET-KEY"},
            "rear_camera": {"url": "rtsp://admin:camPW@192.168.1.60:554/s1", "lores_url": "rtsp://192.168.1.60/s2"}})
        assert r.status_code == 200
        assert (await owner.post("/api/camera/rear/credentials", json={"password": "rear-secret-pw"})).status_code == 200
        res = await new_link(owner)
        secrets = sf.read_secrets(paths.secrets_file())
        needles = ["ESRI-SECRET-KEY", "camPW", "rear-secret-pw", "relay-device-token-xyz", res["token"],
                   guests_mod.token_hash(res["token"]), secrets[sf.KEY_HASH], secrets[sf.KEY_SECRET]]
        s = (await owner.get("/api/settings")).json()  # владельцу — всё как есть
        assert s["tiles"]["esri_api_key"] == "ESRI-SECRET-KEY" and "camPW" in s["rear_camera"]["url"]

        async with client_of(owner.app) as g:
            await as_guest(g, res["token"])
            s = (await g.get("/api/settings")).json()
            assert s["tiles"]["esri_api_key"] == ""
            assert s["rear_camera"]["url"] == "rtsp://192.168.1.60:554/s1"
            assert s["rear_camera"]["lores_url"] == "rtsp://192.168.1.60/s2" and s["rear_camera"]["username"] == ""
            seen = 0
            for route in owner.app.routes:
                if not isinstance(route, APIRoute) or "GET" not in route.methods or "{" in route.path:
                    continue
                if route.path.endswith(("/mjpeg", "/live.ts")):  # бесконечные потоки
                    continue
                r = await asyncio.wait_for(g.get(route.path), 20)
                body = r.content.decode("utf-8", "replace")
                for n in needles:
                    assert n not in body, (route.path, n[:12])
                seen += 1
            assert seen >= 30


def test_redact_settings_and_ws_message():
    d = {"tiles": {"esri_api_key": "K", "imagery_url": "u"}, "rear_camera": {"url": "rtsp://u:p@h/x", "lores_url": ""},
         "display": {"orientation": "north_up"}}
    out = guests_mod.redact_settings(d)
    assert out["tiles"] == {"esri_api_key": "", "imagery_url": "u"} and out["rear_camera"]["url"] == "rtsp://h/x"
    assert out["display"] is d["display"] and d["tiles"]["esri_api_key"] == "K"  # исходник не тронут
    clean = {"tiles": {"esri_api_key": ""}, "rear_camera": {"url": "rtsp://h/x"}}
    assert guests_mod.redact_settings(clean) == clean
    m = {"type": "settings", "settings": d}
    assert guests_mod.redact_message(m)["settings"]["tiles"]["esri_api_key"] == ""
    tel = {"type": "telemetry", "x": 1}
    assert guests_mod.redact_message(tel) is tel


# ── WebSocket ──


class _FakeWs:
    pass


def test_ws_guest_commands_refused_and_broadcast_redacted():
    mgr = WsManager()
    mgr.guest_filter = guests_mod.redact_message
    owner, guest = Client(_FakeWs()), Client(_FakeWs(), guest="abcdef012345")
    mgr.clients |= {owner, guest}

    def drain(c):
        out = []
        while not c.queue.empty():
            out.append(json.loads(c.queue.get_nowait()))
        return out

    mgr._handle(guest, json.dumps({"type": "subscribe", "topics": ["nmea"]}))
    mgr._handle(guest, json.dumps({"type": "ping"}))
    mgr._handle(guest, json.dumps({"type": "arm"}))
    mgr._handle(guest, json.dumps({"type": "settings_patch", "patch": {}}))
    assert guest.topics == {"nmea"}
    got = drain(guest)
    assert got[0] == {"type": "pong"}
    assert [m["command"] for m in got[1:]] == ["arm", "settings_patch"]
    assert all(m["type"] == "error" and m["status"] == 403 and m["detail"] == GUEST_DENIED for m in got[1:])
    mgr._handle(owner, json.dumps({"type": "arm"}))  # владельцу — как раньше (неизвестная команда молча)
    assert drain(owner) == []

    mgr.broadcast({"type": "settings", "settings": {"tiles": {"esri_api_key": "K"}}})
    assert drain(owner)[0]["settings"]["tiles"]["esri_api_key"] == "K"
    assert drain(guest)[0]["settings"]["tiles"]["esri_api_key"] == ""
    mgr.broadcast({"type": "event", "event": "toast", "text": "hi"})
    assert drain(guest) == [{"type": "event", "event": "toast", "text": "hi"}]

    # ссылку отозвали — гостевое соединение закрывается (None — сигнал отправителю), владельца не трогаем
    mgr.drop_guests(lambda gid: gid != "abcdef012345")
    assert guest.closed and guest.queue.get_nowait() is None and not owner.closed
    # срок ссылки вышел — закрывается с ближайшим кадром телеметрии
    late = Client(_FakeWs(), guest="0123456789ab", guest_until=time.time() - 1)
    fresh = Client(_FakeWs(), guest="0123456789ac", guest_until=time.time() + 60)
    mgr.clients |= {late, fresh}
    mgr.broadcast_telemetry('{"type":"telemetry"}', 0)
    assert late.closed and not fresh.closed and fresh.queue.get_nowait() == '{"type":"telemetry"}'


async def test_guest_websocket_end_to_end(live_server):
    srv = live_server(trust_localhost=False)
    async with httpx.AsyncClient(base_url=srv.url) as owner:
        await login(owner)
        assert (await owner.patch("/api/settings", json={"tiles": {"esri_api_key": "ESRI-SECRET"}})).status_code == 200
        res = await new_link(owner)
        async with httpx.AsyncClient(base_url=srv.url) as g:
            await as_guest(g, res["token"])
            cookie = g.cookies["fnav_session"]
        async with connect(srv.ws_url, additional_headers={"Cookie": f"fnav_session={cookie}"}) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert first["type"] == "settings" and first["settings"]["tiles"]["esri_api_key"] == ""
            await ws.send(json.dumps({"type": "arm"}))
            await ws.send(json.dumps({"type": "ping"}))
            got = set()
            for _ in range(80):
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if m["type"] == "error":
                    assert m["status"] == 403 and m["detail"] == GUEST_DENIED
                    got.add("error")
                elif m["type"] == "pong":
                    got.add("pong")
                if got == {"error", "pong"}:
                    break
            assert got == {"error", "pong"}
            # изменение настроек владельцем приходит гостю без ключа
            r = await owner.patch("/api/settings", json={"display": {"orientation": "north_up"}})
            assert r.status_code == 200
            for _ in range(80):
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if m["type"] == "settings":
                    assert m["settings"]["display"]["orientation"] == "north_up"
                    assert m["settings"]["tiles"]["esri_api_key"] == ""
                    break
            else:
                pytest.fail("no settings message")
            # отзыв закрывает открытое соединение гостя
            assert (await owner.delete(f"/api/guest/links/{res['link']['id']}")).status_code == 200
            with pytest.raises(ConnectionClosed):
                for _ in range(200):
                    await asyncio.wait_for(ws.recv(), 5)


# ── удалённый вход (через сервер) ──


async def test_guest_through_remote_listener(data_dir):
    async with running_app(make_opts(remote_port=8090)) as local:
        app = local.app
        async with client_of(app, addr=("127.0.0.1", 40000), port=8090) as owner:
            hdr = {"X-Real-IP": "203.0.113.7", "Host": "van.example.org", "X-Forwarded-Proto": "https"}
            r = await owner.post("/api/login", json={"password": "fnav-dev"}, headers=hdr)
            assert r.status_code == 200
            res = (await owner.post("/api/guest/links", json={"expires_in_s": DAY}, headers=hdr)).json()
            assert res["remote_origin"] == "https://van.example.org"
        # на Wi-Fi машины тот же список знает адрес сервера (ссылку «через сервер» можно показать и отсюда)
        await login(local)
        assert (await local.get("/api/guest/links")).json()["remote_origin"] == "https://van.example.org"
        async with client_of(app, addr=("127.0.0.1", 40001), port=8090) as g:
            ghdr = {"X-Real-IP": "198.51.100.4"}
            r = await g.post("/api/guest/login", json={"token": res["token"]}, headers=ghdr)
            assert r.status_code == 200
            s = (await g.get("/api/session", headers=ghdr)).json()
            assert s["guest"] is True and s["remote"] is True
            assert (await g.post("/api/alarm/arm", headers=ghdr)).status_code == 403
            # неверные попытки — по X-Real-IP от nginx, а не по 127.0.0.1 туннеля
            await g.post("/api/guest/login", json={"token": "z" * 43}, headers=ghdr)
            assert "198.51.100.4" in app.state.core.auth._attempts


# ── B7: мусор в cookie ──


@pytest.mark.parametrize("cookie", ["v1.9999999999.abc.é", "g1.9999999999.abcdefabcdef.éé",
                                    "v1.9999999999.é.abc"])
async def test_non_ascii_cookie_is_401_not_500(data_dir, cookie):
    async with running_app() as c:
        hdr = {"Cookie": ("fnav_session=" + cookie).encode("utf-8")}
        r = await c.get("/api/settings", headers=hdr)
        assert r.status_code == 401
        r = await c.get("/api/session", headers=hdr)
        assert r.status_code == 200 and r.json()["authenticated"] is False


def test_check_token_non_ascii(tmp_path):
    a = auth_mod.AuthManager(secrets_path=tmp_path / "s.env")
    a.load(create_default_password="pw-123456")
    assert a.check_token("v1.9999999999.abc.é") is False
    assert a.check_token("v1.9999999999.abc.Ã©") is False
    assert a.guest_of_token("g1.9999999999.abcdefabcdef.é") is None


# ── файл ссылок ──


async def test_guest_links_file_survives_restart_and_bad_file(tmp_path):
    p = tmp_path / "guest_links.json"
    gl = guests_mod.GuestLinks(p)
    link, token = await gl.create("Bob", DAY)
    again = guests_mod.GuestLinks(p)
    assert again.find_token(token).id == link.id and again.find_token(token + "x") is None
    assert again.relay_list() == [{"id": link.id, "expires": int(link.expires)}]
    p.write_text("{oops", encoding="utf-8")
    assert guests_mod.GuestLinks(p).links == {}  # битый файл — без гостей, без падения
    p.write_text(json.dumps({"links": [{"id": "BAD", "token_hash": "0" * 64, "created": 1, "expires": 2e9},
                                       {"id": "abcdef012345", "token_hash": "zz", "created": 1, "expires": 2e9}]}),
                 encoding="utf-8")
    assert guests_mod.GuestLinks(p).links == {}
    changed = []
    gl3 = guests_mod.GuestLinks(p, on_change=lambda: changed.append(1))
    link3, _ = await gl3.create("", DAY)
    assert await gl3.revoke(link3.id) and not await gl3.revoke(link3.id)
    assert changed == [1, 1]
