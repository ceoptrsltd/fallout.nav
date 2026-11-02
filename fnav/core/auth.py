"""Авторизация: пароль (scrypt), подписанные cookie-сессии (HMAC), анти-перебор, ASGI-middleware.

Роли сессии: владелец (`v1.…`, вход паролем) и гость только для просмотра (`g1.…`, по гостевой ссылке,
fnav/core/guests.py). Гостю middleware пропускает чтение (GET/HEAD/OPTIONS) и короткий явный список запросов, нужных
только для просмотра; всё остальное — 403 `Read-only guest access`. Новые маршруты закрыты для гостя сами собой."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from fnav.core import secretsfile as sf

if TYPE_CHECKING:
    from fnav.core.guests import GuestLinks

log = logging.getLogger("fnav.core.auth")

COOKIE_NAME = "fnav_session"
SESSION_TTL_S = 30 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_S = 30.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
LOGIN_PAGE = "/admin/login.html"
GUEST_LOGIN = "/api/guest/login"
GUEST_DENIED = "Read-only guest access"
ROLE_KEY = "fnav.role"  # роль запроса в ASGI scope (кэш на запрос): OWNER | GUEST | None
OWNER, GUEST = "owner", "guest"
GUEST_ID_RE = re.compile(r"^[a-z0-9]{8,32}$")

# Статика, доступная без входа (нужна странице логина): всё, кроме .html, внутри /admin/,
# и шрифты экрана (общие с админкой).
_PUBLIC_ASSET_EXT = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".gif",
    ".woff", ".woff2", ".ttf", ".otf", ".json", ".txt",
)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _sig_ok(key: bytes, payload: str, sig: str) -> bool:
    """Подпись токена. Не-ASCII подпись (мусор в cookie) — просто неверная, а не TypeError из compare_digest."""
    if not sig.isascii():
        return False
    good = _b64u(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return hmac.compare_digest(good, sig)


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
    guests: GuestLinks | None = None  # гостевые ссылки (fnav/core/guests.py); None — гостевого входа нет
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
        if not _sig_ok(self._key(), f"{ver}.{exp_s}.{nonce}", sig):
            return False
        return exp > (now if now is not None else time.time())

    # ── гостевые сессии: g1.<exp>.<id ссылки>.<sig>, тот же ключ (смена пароля сбрасывает и их) ──
    def issue_guest_token(self, link_id: str, exp: float) -> str:
        payload = f"g1.{int(exp)}.{link_id}"
        return f"{payload}.{_b64u(hmac.new(self._key(), payload.encode(), hashlib.sha256).digest())}"

    def guest_of_token(self, token: str | None, now: float | None = None) -> str | None:
        """id действующей гостевой ссылки по cookie или None: подпись, срок cookie, ссылка не отозвана и не истекла."""
        if not token or not self.password_hash or self.guests is None:
            return None
        try:
            ver, exp_s, link_id, sig = token.split(".")
            if ver != "g1" or not GUEST_ID_RE.match(link_id):
                return None
            exp = int(exp_s)
        except ValueError:
            return None
        if not _sig_ok(self._key(), f"{ver}.{exp_s}.{link_id}", sig):
            return None
        now = time.time() if now is None else now
        if exp <= now or not self.guests.active(link_id, now):
            return None
        return link_id

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

    def register_attempt(self, ip: str, ok: bool) -> None:
        """Попытка не паролем (гостевая ссылка): тот же счётчик ошибок и та же пауза, что у входа."""
        self._register(ip, ok, time.monotonic())

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

    def role(self, conn: HTTPConnection) -> str | None:
        """OWNER (вход паролем), GUEST (гостевая ссылка) или None. Считается раз на запрос (кэш в scope)."""
        if ROLE_KEY in conn.scope:
            return conn.scope[ROLE_KEY]
        token = conn.cookies.get(COOKIE_NAME)
        role = OWNER if self.check_token(token) else GUEST if self.guest_of_token(token) else None
        conn.scope[ROLE_KEY] = role
        return role

    def is_authenticated(self, conn: HTTPConnection) -> bool:
        """Вошёл владелец. Гость сюда не входит (см. role(), is_guest())."""
        return self.role(conn) == OWNER

    def is_guest(self, conn: HTTPConnection) -> bool:
        return self.role(conn) == GUEST

    def guest_id(self, conn: HTTPConnection) -> str | None:
        return self.guest_of_token(conn.cookies.get(COOKIE_NAME)) if self.is_guest(conn) else None


def is_public_path(path: str, method: str) -> bool:
    if path in ("/api/login", "/api/session", GUEST_LOGIN, "/", "/favicon.ico", LOGIN_PAGE):
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
    if path in ("/api/camera/mjpeg", "/api/camera/rear/mjpeg"):
        return True
    if path == "/api/games" and method in ("GET", "HEAD") or path == "/api/games/launch" and method == "POST":
        return True  # меню игр на экране (геймпад у киоска)
    return path == "/api/settings" and method in ("GET", "HEAD")


# Гостю, кроме чтения: живое видео WebRTC (WHEP: POST — предложение SDP, PATCH — ICE, DELETE — закрыть свою сессию
# просмотра) и выход (удаляет cookie только у самого гостя). Больше ничего: список явный и короткий.
_GUEST_READ = frozenset({"GET", "HEAD", "OPTIONS"})
_WHEP_ROOT = re.compile(r"^/api/camera(?:/rear)?/whep$")
_WHEP_SUB = re.compile(r"^/api/camera(?:/rear)?/whep/.+$")


def guest_allowed(path: str, method: str) -> bool:
    """Можно ли гостю (только просмотр) этот HTTP-запрос. Команды гостя по WebSocket проверяет ws.py."""
    if method in _GUEST_READ:
        return True
    if method == "POST" and (path == "/api/logout" or _WHEP_ROOT.match(path)):
        return True
    return method in ("PATCH", "DELETE") and bool(_WHEP_SUB.match(path))


def _is_api(path: str) -> bool:
    return path.startswith(("/api/", "/tiles/")) or path == "/ws"


async def _send_json(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


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
        role = auth.role(conn)
        if (
            is_public_path(path, method)
            or (auth.is_local(conn) and is_local_allowed(path, method))
            or role == OWNER
        ):
            await self.app(scope, receive, send)
            return

        if role == GUEST:
            # только просмотр: WebSocket пускаем (его команды фильтрует ws.py), HTTP — по guest_allowed()
            if scope["type"] == "websocket" or guest_allowed(path, method):
                if auth.guests is not None:
                    await auth.guests.touch(auth.guest_id(conn))
                await self.app(scope, receive, send)
            else:
                await _send_json(send, 403, GUEST_DENIED)
            return

        if scope["type"] == "websocket":
            msg = await receive()
            if msg.get("type") == "websocket.connect":
                await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})
            return

        if _is_api(path) or method not in ("GET", "HEAD"):
            await _send_json(send, 401, "Login required")
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
