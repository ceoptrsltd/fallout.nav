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

from . import auth
from .config import Config
from .store import Store

log = logging.getLogger("fnav_relay.api")

MAX_BODY = 2 * 1024 * 1024
_PLACE_RE = re.compile(r"^/api/tracking/places/(\d+)$")
_TRACKING_GET = {"/api/tracking/places", "/api/tracking/track", "/api/tracking/last"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: Config, store: Store, guard: auth.LoginGuard | None = None) -> None:
        if ":" in cfg.listen_host:
            self.address_family = socket.AF_INET6
        self.cfg, self.store = cfg, store
        self.guard = guard or auth.LoginGuard()
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

    def _logged_in(self) -> bool:
        password_hash, secret = self.server.store.auth()
        return auth.check_token(self._session_token(), secret, password_hash)

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
            raise ApiError(404, "Not found")
        if path == "/api/login":
            self._method(method, "POST")
            return self._login()
        if path == "/api/logout":
            self._method(method, "POST")
            return self._send(200, {"ok": True}, cookies=[self._cookie("", 0)])
        if path == "/api/session":
            self._method(method, "GET")
            return self._send(200, {"authenticated": self._logged_in(), "local": False, "mock": False,
                                    "offline": True})
        if path == "/api/tracking" or path.startswith("/api/tracking/"):
            if not (self._has_device_token() or self._logged_in()):
                raise ApiError(401, "Login required")
            return self._tracking(method, path, qs)
        if path == "/api" or path.startswith("/api/"):
            # nginx присылает сюда запросы к устройству, когда туннеля нет
            raise ApiError(503, "Device is offline")
        raise ApiError(404, "Not found")

    def _device_auth(self) -> None:
        data = self._json_body()
        h, s = data.get("password_hash"), data.get("session_secret")
        if not auth.looks_like_hash(h) or not isinstance(s, str) or len(s) < 16:
            raise ApiError(400, "password_hash (scrypt$…) and session_secret (16+ chars) are required")
        if self.server.store.auth() != (h, s):
            self.server.store.set_auth(h, s)
            log.info("device synced its password hash and session secret")
        self._send(200, {"ok": True})

    def _device_samples(self) -> None:
        samples = self._json_body().get("samples")
        if not isinstance(samples, list):
            raise ApiError(400, "'samples' must be a list")
        res = self.server.store.ingest(samples)
        log.debug("samples: %s", res)
        self._send(200, {"ok": True, **res})

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
