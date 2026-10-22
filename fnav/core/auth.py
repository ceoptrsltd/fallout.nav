"""Авторизация: пароль (scrypt), подписанные cookie-сессии (HMAC), анти-перебор, ASGI-middleware."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from fnav.core import secretsfile as sf

log = logging.getLogger("fnav.core.auth")

COOKIE_NAME = "fnav_session"
SESSION_TTL_S = 30 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_S = 30.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
LOGIN_PAGE = "/admin/login.html"

# Статика, доступная без входа (нужна странице логина): всё, кроме .html, внутри /admin/,
# и шрифты экрана (общие с админкой).
_PUBLIC_ASSET_EXT = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".gif",
    ".woff", ".woff2", ".ttf", ".otf", ".json", ".txt",
)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class AuthManager:
    """Хранит хэш пароля и секрет, выдаёт/проверяет токены сессий."""

    secrets_path: Path
    trust_localhost: bool = True
    # Порт «удалённого» входа (туда приходит ssh-туннель с сервера): для него localhost-доверие киоска не
    # действует, а IP клиента берётся из X-Real-IP (его ставит nginx на сервере). None — входа нет.
    remote_port: int | None = None
    password_hash: str | None = None
    session_secret: str = ""
    _attempts: dict[str, _Attempts] = field(default_factory=dict)

    # ── загрузка ──
    def load(self, *, create_default_password: str | None = None) -> None:
        try:
            data = sf.ensure_secrets(default_password=create_default_password, path=self.secrets_path)
        except OSError as e:
            log.error("cannot write %s: %s", self.secrets_path, e)
            data = sf.read_secrets(self.secrets_path)
        # файл — источник правды; переменные окружения (EnvironmentFile systemd) — запасной вариант
        self.password_hash = data.get(sf.KEY_HASH) or os.environ.get(sf.KEY_HASH) or None
        self.session_secret = data.get(sf.KEY_SECRET) or os.environ.get(sf.KEY_SECRET) or ""
        if not self.session_secret:
            log.warning("FNAV_SESSION_SECRET is not set - using a temporary secret until restart")
            self.session_secret = sf.new_session_secret()
        if not self.password_hash:
            log.error("No password set (%s). Set it with: python -m fnav.core.passwd", self.secrets_path)

    # ── сессии ──
    def _key(self) -> bytes:
        # смена пароля делает недействительными все старые сессии
        return hmac.new(self.session_secret.encode(), (self.password_hash or "").encode(), hashlib.sha256).digest()

    def issue_token(self, now: float | None = None) -> str:
        exp = int((now if now is not None else time.time()) + SESSION_TTL_S)
        payload = f"v1.{exp}.{_b64u(os.urandom(12))}"
        sig = _b64u(hmac.new(self._key(), payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def check_token(self, token: str | None, now: float | None = None) -> bool:
        if not token or not self.password_hash:
            return False
        try:
            ver, exp_s, nonce, sig = token.split(".")
            if ver != "v1":
                return False
            exp = int(exp_s)
        except ValueError:
            return False
        good = _b64u(hmac.new(self._key(), f"{ver}.{exp_s}.{nonce}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return False
        return exp > (now if now is not None else time.time())

    # ── анти-перебор ──
    def lockout_left(self, ip: str, now: float | None = None) -> float:
        a = self._attempts.get(ip)
        if not a:
            return 0.0
        left = a.locked_until - (now if now is not None else time.monotonic())
        return max(0.0, left)

    def _register(self, ip: str, ok: bool, now: float) -> None:
        if ok:
            self._attempts.pop(ip, None)
            return
        a = self._attempts.setdefault(ip, _Attempts())
        if a.locked_until and now >= a.locked_until:
            a.failures = 0
            a.locked_until = 0.0
        a.failures += 1
        if a.failures >= MAX_FAILURES:
            a.locked_until = now + LOCKOUT_S
        if len(self._attempts) > 10000:  # не даём словарю расти бесконечно
            self._attempts.clear()

    async def try_password(self, ip: str, password: str) -> tuple[bool, float]:
        """→ (ok, сколько ждать). Если IP заблокирован — пароль даже не проверяется."""
        now = time.monotonic()
        left = self.lockout_left(ip, now)
        if left > 0:
            return False, left
        ok = await asyncio.to_thread(sf.verify_password, password, self.password_hash)
        self._register(ip, ok, time.monotonic())
        return ok, 0.0

    async def change_password(self, new_password: str) -> None:
        new_hash = await asyncio.to_thread(sf.hash_password, new_password)
        await asyncio.to_thread(
            sf.update_secrets, {sf.KEY_HASH: new_hash, sf.KEY_SECRET: self.session_secret}, self.secrets_path
        )
        self.password_hash = new_hash

    # ── запросы ──
    def is_remote(self, conn: HTTPConnection) -> bool:
        srv = conn.scope.get("server")
        return bool(self.remote_port) and bool(srv) and srv[1] == self.remote_port

    def is_local(self, conn: HTTPConnection) -> bool:
        return (self.trust_localhost and not self.is_remote(conn)
                and conn.client is not None and conn.client.host in LOCAL_HOSTS)

    def client_ip(self, conn: HTTPConnection) -> str:
        if self.is_remote(conn):
            ip = conn.headers.get("x-real-ip", "").strip()
            if ip:
                return ip
        return conn.client.host if conn.client else "?"

    def is_authenticated(self, conn: HTTPConnection) -> bool:
        return self.check_token(conn.cookies.get(COOKIE_NAME))


def is_public_path(path: str, method: str) -> bool:
    if path in ("/api/login", "/api/session", "/", "/favicon.ico", LOGIN_PAGE):
        return True
    if path.startswith("/admin/") and path.lower().endswith(_PUBLIC_ASSET_EXT):
        return True
    return path.startswith("/display/ui/fonts/")


def is_local_allowed(path: str, method: str) -> bool:
    """Что можно с 127.0.0.1 без сессии (киоск Chromium на самом устройстве)."""
    if path == "/display" or path.startswith("/display/"):
        return True
    if path == "/ws" or path.startswith("/tiles/"):
        return True
    if path == "/api/camera/mjpeg":
        return True
    return path == "/api/settings" and method in ("GET", "HEAD")


def _is_api(path: str) -> bool:
    return path.startswith(("/api/", "/tiles/")) or path == "/ws"


class AuthMiddleware:
    """Чистый ASGI-middleware (без BaseHTTPMiddleware — не ломает стриминг и WebSocket)."""

    def __init__(self, app: ASGIApp, get_auth) -> None:
        self.app = app
        self.get_auth = get_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        auth: AuthManager = self.get_auth()
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET") if scope["type"] == "http" else "GET"
        conn = HTTPConnection(scope)
        if (
            is_public_path(path, method)
            or (auth.is_local(conn) and is_local_allowed(path, method))
            or auth.is_authenticated(conn)
        ):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            msg = await receive()
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})
            return

        if _is_api(path) or method not in ("GET", "HEAD"):
            body = json.dumps({"detail": "Login required"}, ensure_ascii=False).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return

        qs = scope.get("query_string", b"").decode("latin-1")
        nxt = path + (f"?{qs}" if qs else "")
        loc = f"{LOGIN_PAGE}?next={quote(nxt, safe='')}"
        await send({
            "type": "http.response.start",
            "status": 302,
            "headers": [(b"location", loc.encode("latin-1")), (b"content-length", b"0"),
                        (b"cache-control", b"no-store")],
        })
        await send({"type": "http.response.body", "body": b""})
