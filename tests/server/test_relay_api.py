"""HTTP API fnav-relay: токен устройства, вход/анти-перебор, cookie, префикс /fnadmin, ответ за выключенное устройство,
совместимость паролей и токенов с fnav-core."""

from __future__ import annotations

import time

import pytest
from relay_helpers import DEVICE_TOKEN, auth, pt

from fnav.core import secretsfile as sf
from fnav.core.auth import AuthManager

PASSWORD = "relay-test-pass"


@pytest.fixture(scope="module")
def pw_hash() -> str:
    return sf.hash_password(PASSWORD)


SECRET = "session-secret-0123456789abcdef"


def sync_auth(relay, pw_hash: str, secret: str = SECRET) -> None:
    r = relay.device("POST", "/device/auth", {"password_hash": pw_hash, "session_secret": secret})
    assert r.status == 200 and r.data == {"ok": True}


def cookie_of(resp) -> str:
    (c,) = [c for c in resp.cookies() if c.startswith(auth.COOKIE_NAME + "=")]
    return c.split(";", 1)[0]


def login(relay, password: str = PASSWORD, ip: str = "203.0.113.7"):
    return relay.call("POST", "/fnadmin/api/login", {"password": password}, {"X-Real-IP": ip})


# ── устройство ──


def test_device_endpoints_need_token(relay):
    body = {"samples": [pt(1000)]}
    assert relay.call("POST", "/device/samples", body).status == 403
    assert relay.call("POST", "/device/samples", body, {"X-Fnav-Device-Token": "wrong"}).status == 403
    assert relay.call("POST", "/device/auth", {}).status == 403
    r = relay.device("POST", "/device/samples", body)
    assert r.status == 200 and r.data == {"ok": True, "accepted": 1, "skipped": 0, "last_ts": 1000}
    r = relay.device("POST", "/device/samples", body)  # повтор безопасен
    assert r.data["accepted"] == 0 and r.data["last_ts"] == 1000


def test_device_bad_bodies(relay):
    assert relay.device("POST", "/device/samples", {"samples": "x"}).status == 400
    assert relay.call("POST", "/device/samples", headers={"X-Fnav-Device-Token": DEVICE_TOKEN},
                      raw=b"{not json").status == 400
    assert relay.device("POST", "/device/auth", {"password_hash": "plain", "session_secret": SECRET}).status == 400
    assert relay.device("POST", "/device/auth", {"password_hash": "scrypt$1$1$1$a$b",
                                                 "session_secret": "short"}).status == 400
    assert relay.device("GET", "/device/samples").status == 405
    assert relay.device("POST", "/device/other").status == 404


def test_empty_device_token_rejects_everything(relay):
    object.__setattr__(relay.cfg, "device_token", "")  # конфиг frozen, сервер держит тот же объект
    assert relay.call("POST", "/device/samples", {"samples": []}, {"X-Fnav-Device-Token": ""}).status == 403


# ── журнал ──


def test_tracking_needs_cookie_or_device_token(relay, pw_hash):
    relay.device("POST", "/device/samples", {"samples": [pt(1000), pt(1300, 5), pt(1400, 900)]})
    assert relay.call("GET", "/fnadmin/api/tracking/places").status == 401
    r = relay.device("GET", "/api/tracking/places")
    assert r.status == 200 and r.data["stop_min_s"] == 300 and r.data["radius_m"] == 50
    assert [(p["id"], p["kind"]) for p in r.data["places"]] == [(1, "stop"), (2, "transit")]

    sync_auth(relay, pw_hash)
    c = cookie_of(login(relay))
    r = relay.call("GET", "/fnadmin/api/tracking/places?from=1350&to=2000", headers={"Cookie": c})
    assert r.status == 200 and [p["id"] for p in r.data["places"]] == [2]
    r = relay.call("GET", "/fnadmin/api/tracking/places/1", headers={"Cookie": "other=1; " + c})
    assert r.status == 200 and r.data["place"]["visits"] == 1
    assert r.data["visits"][0]["duration_s"] == 300 and r.data["visits"][0]["samples"] == 2
    assert relay.call("GET", "/fnadmin/api/tracking/places/99", headers={"Cookie": c}).status == 404
    r = relay.call("GET", "/fnadmin/api/tracking/track", headers={"Cookie": c})
    assert [p[0] for p in r.data["points"]] == [1000, 1400]
    r = relay.call("GET", "/fnadmin/api/tracking/last", headers={"Cookie": c})
    assert r.data["ts"] == 1400 and "received_ts" in r.data
    assert relay.call("GET", "/api/tracking/places?from=abc", headers={"Cookie": c}).status == 400
    assert relay.call("GET", "/api/tracking/nope", headers={"Cookie": c}).status == 404
    assert relay.call("POST", "/api/tracking/places", {}, {"Cookie": c}).status == 405


def test_tracking_last_empty(relay):
    r = relay.device("GET", "/fnadmin/api/tracking/last")
    assert r.status == 200 and r.data == {}


# ── вход ──


def test_login_before_sync_is_503(relay):
    r = login(relay)
    assert r.status == 503 and "synced" in r.data["detail"]


def test_login_session_logout(relay, pw_hash):
    sync_auth(relay, pw_hash)
    r = relay.call("GET", "/fnadmin/api/session")
    assert r.data == {"authenticated": False, "local": False, "mock": False, "offline": True}

    assert login(relay, "wrong").status == 401
    r = login(relay)
    assert r.status == 200 and r.data == {"ok": True}
    raw = next(c for c in r.cookies() if c.startswith("fnav_session="))
    for attr in ("Path=/fnadmin/", "HttpOnly", "Secure", "SameSite=Lax", f"Max-Age={auth.SESSION_TTL_S}"):
        assert attr in raw
    c = cookie_of(r)
    assert relay.call("GET", "/api/session", headers={"Cookie": c}).data["authenticated"] is True

    r = relay.call("POST", "/fnadmin/api/logout", {})
    assert r.status == 200 and "Max-Age=0" in r.cookies()[0] and "Path=/fnadmin/" in r.cookies()[0]
    assert relay.call("POST", "/api/login", {}, {"X-Real-IP": "1.1.1.1"}).status == 400


def test_password_change_on_device_invalidates_cookie(relay, pw_hash):
    sync_auth(relay, pw_hash)
    c = cookie_of(login(relay))
    sync_auth(relay, sf.hash_password("new-password-1"))
    assert relay.call("GET", "/api/session", headers={"Cookie": c}).data["authenticated"] is False
    assert relay.call("GET", "/api/tracking/last", headers={"Cookie": c}).status == 401


def test_lockout_per_ip(relay, pw_hash):
    sync_auth(relay, pw_hash)
    for _ in range(auth.MAX_FAILURES):
        assert login(relay, "bad", ip="198.51.100.1").status == 401
    r = login(relay, ip="198.51.100.1")  # даже верный пароль не проверяется
    assert r.status == 429 and int(r.headers["Retry-After"]) >= 1
    assert login(relay, ip="198.51.100.2").status == 200  # другой IP не задет


def test_guard_lockout_expires_and_global_cap():
    g = auth.LoginGuard()
    for _ in range(auth.MAX_FAILURES):
        g.register("a", False, now=100.0)
    assert g.wait_s("a", now=100.0) == pytest.approx(auth.LOCKOUT_S)
    assert g.wait_s("a", now=100.0 + auth.LOCKOUT_S) == 0
    g.register("a", False, now=200.0)  # после паузы счёт заново
    assert g.wait_s("a", now=200.0) == 0

    g = auth.LoginGuard()
    for i in range(auth.GLOBAL_MAX_FAILURES):
        g.register(f"ip{i}", False, now=1000.0 + i)
    assert g.wait_s("fresh", now=1000.0 + auth.GLOBAL_MAX_FAILURES) > 0
    assert g.wait_s("fresh", now=1000.0 + auth.GLOBAL_WINDOW_S + 1) == 0


# ── префикс и ответ за устройство ──


def test_offline_fallback_and_routing(relay):
    r = relay.call("GET", "/fnadmin/api/settings")
    assert r.status == 503 and r.data == {"detail": "Device is offline"}
    assert relay.call("PATCH", "/fnadmin/api/settings", {"gps": {}}).status == 503
    assert relay.call("DELETE", "/api/recordings/file/x.ts").status == 503
    assert relay.call("GET", "/fnadmin/healthz").data == {"ok": True}
    assert relay.call("GET", "/nope").status == 404
    assert relay.call("GET", "/fnadminx/api/settings").status == 404  # префикс — только целым сегментом
    assert relay.call("GET", "/api/login").status == 405


def test_keepalive_after_unread_body(relay):
    """Тело запроса читается всегда — следующий запрос в том же соединении не ломается."""
    import http.client

    host, port = relay.base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("PUT", "/api/settings", body=b'{"a":1}', headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        assert r.status == 503
        conn.request("GET", "/healthz")
        r = conn.getresponse()
        assert r.status == 200 and r.read() == b'{"ok":true}'
    finally:
        conn.close()


# ── совместимость с fnav-core ──


def test_core_token_and_hash_valid_in_relay(tmp_path, pw_hash):
    core = AuthManager(secrets_path=tmp_path / "secrets.env", password_hash=pw_hash, session_secret=SECRET)
    assert auth.verify_password(PASSWORD, pw_hash) and not auth.verify_password("x", pw_hash)
    tok = core.issue_token()
    assert auth.check_token(tok, SECRET, pw_hash)
    assert not auth.check_token(tok, SECRET + "x", pw_hash)
    assert not auth.check_token(tok[:-2] + "AA", SECRET, pw_hash)
    # и обратно: cookie сервера принимает устройство
    mine = auth.issue_token(SECRET, pw_hash)
    assert core.check_token(mine)
    # срок действия
    old = auth.issue_token(SECRET, pw_hash, now=time.time() - auth.SESSION_TTL_S - 10)
    assert not auth.check_token(old, SECRET, pw_hash) and not core.check_token(old)


def test_core_cookie_accepted_by_relay_api(relay, tmp_path, pw_hash):
    sync_auth(relay, pw_hash)
    core = AuthManager(secrets_path=tmp_path / "s.env", password_hash=pw_hash, session_secret=SECRET)
    c = f"{auth.COOKIE_NAME}={core.issue_token()}"
    assert relay.call("GET", "/fnadmin/api/session", headers={"Cookie": c}).data["authenticated"] is True
    assert relay.call("GET", "/fnadmin/api/tracking/places", headers={"Cookie": c}).status == 200
