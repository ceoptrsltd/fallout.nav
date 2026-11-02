"""HTTP API (ThreadingHTTPServer, JSON). За nginx: путь может прийти с префиксом /fnadmin — он отрезается."""

from __future__ import annotations

import hmac
import json
import logging
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import auth, webpush
from .alarm import Notifier
from .config import Config
from .store import Store

log = logging.getLogger("fnav_relay.api")

MAX_BODY = 2 * 1024 * 1024
_PLACE_RE = re.compile(r"^/api/tracking/places/(\d+)$")
_TRACKING_GET = {"/api/tracking/places", "/api/tracking/track", "/api/tracking/last"}
_PUSH_SUB_RE = re.compile(r"^/api/push/subscriptions/(\d+)$")
_PUSH_SNAP_RE = re.compile(r"^/api/push/snap/([A-Za-z0-9_-]{24,64})\.jpg$")

GUEST_DENIED = "Read-only guest access"
MAX_GUEST_LINKS = 200


class ApiError(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: Config, store: Store, guard: auth.LoginGuard | None = None,
                 notifier: Notifier | None = None) -> None:
        if ":" in cfg.listen_host:
            self.address_family = socket.AF_INET6
        self.cfg, self.store = cfg, store
        self.guard = guard or auth.LoginGuard()
        # потоки рассылки и сторожа запускает и останавливает владелец сервера (__main__, тесты)
        self.notifier = notifier or Notifier(store, cfg)
        super().__init__((cfg.listen_host, cfg.listen_port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: RelayServer
    server_version = "fnav-relay"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30
    _body = b""

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args) -> None:
        log.debug("%s %s", self._ip(), format % args)

    # ── ответы ──
    def _send(self, status: int, obj: object, headers: dict[str, str] | None = None,
              cookies: list[str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, value: str, max_age: int) -> str:
        return (f"{auth.COOKIE_NAME}={value}; Path={self.server.cfg.cookie_path}; Max-Age={max_age}; "
                "HttpOnly; Secure; SameSite=Lax")

    # ── запрос ──
    def _ip(self) -> str:
        headers = getattr(self, "headers", None)  # нет, если запрос не разобран
        real = (headers.get("X-Real-IP") or "").strip() if headers is not None else ""
        return real or self.client_address[0]

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApiError(411, "Content-Length is required")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "Invalid Content-Length") from None
        if n > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "Request body is too large")
        return self.rfile.read(n) if n > 0 else b""

    def _json_body(self) -> dict:
        try:
            data = json.loads(self._body or b"{}")
        except ValueError:
            raise ApiError(400, "Invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON object expected")
        return data

    def _session_token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == auth.COOKIE_NAME:
                return v.strip()
        return None

    def _has_device_token(self) -> bool:
        want = self.server.cfg.device_token
        got = self.headers.get("X-Fnav-Device-Token") or ""
        return bool(want) and hmac.compare_digest(got.encode(), want.encode())

    def _guest_links(self) -> dict[str, float]:
        """Действующие гостевые ссылки (id → срок), как их прислало устройство в POST /device/auth."""
        raw = self.server.store.get_value("guest_links")
        try:
            return {str(x["id"]): float(x["expires"]) for x in json.loads(raw)} if raw else {}
        except (ValueError, KeyError, TypeError):
            return {}

    def _role(self) -> str | None:
        """"owner" (вход паролем), "guest" (гостевая ссылка: только просмотр) или None."""
        password_hash, secret = self.server.store.auth()
        token = self._session_token()
        if auth.check_token(token, secret, password_hash):
            return "owner"
        if token and token.startswith("g1.") and auth.check_guest_token(token, secret, password_hash,
                                                                        self._guest_links()):
            return "guest"
        return None

    def _logged_in(self) -> bool:
        return self._role() == "owner"

    def _range_arg(self, qs: dict, name: str) -> float | None:
        v = qs.get(name, [""])[0]
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            raise ApiError(400, f"Invalid '{name}'") from None

    # ── маршруты ──
    def _dispatch(self, method: str) -> None:
        try:
            self._body = self._read_body()
            url = urlsplit(self.path)
            path = url.path
            pre = self.server.cfg.prefix
            if pre and (path == pre or path.startswith(pre + "/")):
                path = path[len(pre):] or "/"
            self._route(method, path, parse_qs(url.query))
        except ApiError as e:
            self._send(e.status, {"detail": e.detail}, e.headers)
        except Exception:
            log.exception("%s %s failed", method, self.path)
            self.close_connection = True
            self._send(500, {"detail": "Internal server error"})

    @staticmethod
    def _method(method: str, allowed: str) -> None:
        if method != allowed:
            raise ApiError(405, "Method not allowed", {"Allow": allowed})

    def _route(self, method: str, path: str, qs: dict) -> None:
        if path == "/healthz":
            self._method(method, "GET")
            return self._send(200, {"ok": True})
        if path.startswith("/device/"):
            if not self._has_device_token():
                raise ApiError(403, "Invalid device token")
            if path == "/device/auth":
                self._method(method, "POST")
                return self._device_auth()
            if path == "/device/samples":
                self._method(method, "POST")
                return self._device_samples()
            if path == "/device/alarm":
                self._method(method, "POST")
                return self._device_alarm()
            raise ApiError(404, "Not found")
        if path == "/api/login":
            self._method(method, "POST")
            return self._login()
        if path == "/api/logout":
            self._method(method, "POST")
            return self._send(200, {"ok": True}, cookies=[self._cookie("", 0)])
        if path == "/api/session":
            self._method(method, "GET")
            role = self._role()
            out = {"authenticated": role is not None, "local": False, "mock": False, "offline": True}
            if role is not None:
                out["guest"] = role == "guest"
            return self._send(200, out)
        m = _PUSH_SNAP_RE.match(path)
        if m:  # картинка уведомления: браузер грузит её без cookie — неугадываемая ссылка с ограниченным сроком
            self._method(method, "GET")
            return self._push_snap(m.group(1))
        if path in ("/api/tracking", "/api/push") or path.startswith(("/api/tracking/", "/api/push/")):
            role = "device" if self._has_device_token() else self._role()
            if role is None:
                raise ApiError(401, "Login required")
            if role == "guest" and method != "GET":  # гость — только просмотр: ни подписок, ни теста push
                raise ApiError(403, GUEST_DENIED)
        if path == "/api/tracking" or path.startswith("/api/tracking/"):
            return self._tracking(method, path, qs)
        if path == "/api/push" or path.startswith("/api/push/"):
            try:
                return self._push(method, path)
            except webpush.PushUnavailable:
                raise ApiError(503, webpush.NO_CRYPTO) from None
        if path == "/api" or path.startswith("/api/"):
            # nginx присылает сюда запросы к устройству, когда туннеля нет
            raise ApiError(503, "Device is offline")
        raise ApiError(404, "Not found")

    def _device_auth(self) -> None:
        data = self._json_body()
        h, s = data.get("password_hash"), data.get("session_secret")
        if not auth.looks_like_hash(h) or not isinstance(s, str) or len(s) < 16:
            raise ApiError(400, "password_hash (scrypt$…) and session_secret (16+ chars) are required")
        links = data.get("guest_links", [])  # гостевые ссылки устройства: какие гостевые cookie принимать
        if not isinstance(links, list) or len(links) > MAX_GUEST_LINKS:
            raise ApiError(400, "'guest_links' must be a list of {\"id\", \"expires\"}")
        clean = [{"id": x["id"], "expires": float(x["expires"])} for x in links
                 if isinstance(x, dict) and isinstance(x.get("id"), str) and auth.GUEST_ID_RE.match(x["id"])
                 and isinstance(x.get("expires"), (int, float)) and not isinstance(x.get("expires"), bool)]
        if self.server.store.auth() != (h, s):
            self.server.store.set_auth(h, s)
            log.info("device synced its password hash and session secret")
        text = json.dumps(sorted(clean, key=lambda x: x["id"]))
        if (self.server.store.get_value("guest_links") or "[]") != text:
            self.server.store.set_values({"guest_links": text})
            log.info("device synced %d guest link(s)", len(clean))
        self._send(200, {"ok": True})

    def _device_samples(self) -> None:
        samples = self._json_body().get("samples")
        if not isinstance(samples, list):
            raise ApiError(400, "'samples' must be a list")
        res = self.server.store.ingest(samples)
        log.debug("samples: %s", res)
        self._send(200, {"ok": True, **res})

    def _device_alarm(self) -> None:
        data = self._json_body()
        state, events = data.get("state"), data.get("events", [])
        if not (isinstance(state, dict) and isinstance(state.get("armed"), bool)
                and isinstance(state.get("notify_offline"), bool)):
            raise ApiError(400, "'state' must be {\"armed\": bool, \"notify_offline\": bool}")
        if not isinstance(events, list):
            raise ApiError(400, "'events' must be a list")
        accepted = self.server.notifier.device_report(state, events)  # рассылка — в фоне
        self._send(200, {"ok": True, "accepted": accepted})

    def _login(self) -> None:
        store, guard = self.server.store, self.server.guard
        password_hash, secret = store.auth()
        if not password_hash or not secret:
            raise ApiError(503, "The device has not synced its password to the server yet")
        password = self._json_body().get("password")
        if not isinstance(password, str):
            raise ApiError(400, "Field 'password' is required")
        ip = self._ip()
        wait = guard.wait_s(ip)
        if wait > 0:
            raise ApiError(429, f"Too many attempts. Wait {int(wait) + 1} s", {"Retry-After": str(int(wait) + 1)})
        ok = auth.verify_password(password, password_hash)
        guard.register(ip, ok)
        if not ok:
            log.warning("failed login from %s", ip)
            raise ApiError(401, "Wrong password")
        token = auth.issue_token(secret, password_hash, time.time())
        self._send(200, {"ok": True}, cookies=[self._cookie(token, auth.SESSION_TTL_S)])

    def _tracking(self, method: str, path: str, qs: dict) -> None:
        store = self.server.store
        m = _PLACE_RE.match(path)
        if m:
            self._method(method, "GET")
            res = store.place(int(m.group(1)))
            if res is None:
                raise ApiError(404, "Place not found")
            return self._send(200, res)
        if path not in _TRACKING_GET:
            raise ApiError(404, "Not found")
        self._method(method, "GET")
        if path == "/api/tracking/last":
            return self._send(200, store.last())
        t0, t1 = self._range_arg(qs, "from"), self._range_arg(qs, "to")
        if path == "/api/tracking/track":
            return self._send(200, {"points": store.track(t0, t1)})
        self._send(200, {"places": store.places(t0, t1), "stop_min_s": store.stop_min_s,
                         "radius_m": store.radius_m})

    def _push(self, method: str, path: str) -> None:
        store, notifier = self.server.store, self.server.notifier
        m = _PUSH_SUB_RE.match(path)
        if m:
            self._method(method, "DELETE")
            if not store.push_delete(int(m.group(1))):
                raise ApiError(404, "Subscription not found")
            return self._send(200, {"ok": True})
        if path == "/api/push/key":
            self._method(method, "GET")
            return self._send(200, {"public_key": notifier.vapid().public_key})
        if path == "/api/push/subscriptions":
            self._method(method, "GET")
            keys = ("id", "name", "created", "last_ok", "last_error", "host")
            return self._send(200, {"subscriptions": [{k: s[k] for k in keys} for s in store.push_subs()]})
        if path == "/api/push/subscribe":
            self._method(method, "POST")
            return self._push_subscribe()
        if path == "/api/push/unsubscribe":
            self._method(method, "POST")
            endpoint = self._json_body().get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise ApiError(400, "Field 'endpoint' is required")
            store.push_unsubscribe(endpoint)
            return self._send(200, {"ok": True})
        if path == "/api/push/test":
            self._method(method, "POST")
            endpoint = self._json_body().get("endpoint")
            if endpoint is not None and not isinstance(endpoint, str):
                raise ApiError(400, "'endpoint' must be a string or null")
            if endpoint is not None and all(s["endpoint"] != endpoint for s in store.push_subs()):
                raise ApiError(404, "Subscription not found")
            res = notifier.broadcast(notifier.test_message(), endpoint=endpoint, urgency="normal")
            return self._send(200, {"ok": True, **res})
        raise ApiError(404, "Not found")

    def _push_snap(self, token: str) -> None:
        got = self.server.notifier.snaps.get(token)
        if got is None:
            raise ApiError(404, "Not found")
        data, left = got
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", f"private, max-age={int(left)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _push_subscribe(self) -> None:
        data = self._json_body()
        notifier = self.server.notifier
        notifier.vapid()  # 503 без cryptography; ключи — до первой подписки
        try:
            endpoint, p256dh, key_auth = webpush.clean_subscription(
                data.get("subscription"), allow_http=self.server.cfg.push_allow_http)
        except webpush.InvalidSubscription as e:
            raise ApiError(400, str(e)) from None
        name = data.get("name")
        name = " ".join(name.split())[:64] if isinstance(name, str) else ""
        notifier.remember_host(self.headers.get("Host"))
        sub_id = self.server.store.push_subscribe(endpoint, p256dh, key_auth, name, notifier.clock())
        log.info("push: subscription %d from %s (%s)", sub_id, urlsplit(endpoint).hostname, name or "no name")
        self._send(200, {"ok": True, "id": sub_id})
