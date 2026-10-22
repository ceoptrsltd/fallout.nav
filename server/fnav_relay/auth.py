"""Пароль (scrypt) и токены сессий — байт-в-байт как fnav/core/secretsfile.py и fnav/core/auth.py, плюс анти-перебор.

Хэш пароля и секрет сессий присылает устройство, поэтому cookie, выданная устройством, действует и здесь, и наоборот.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

COOKIE_NAME = "fnav_session"
SESSION_TTL_S = 30 * 24 * 3600
MAX_FAILURES = 5  # подряд с одного IP → блокировка IP
LOCKOUT_S = 30.0
GLOBAL_WINDOW_S = 60.0  # общий предел неудачных попыток со всех IP за окно
GLOBAL_MAX_FAILURES = 30


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ── пароль: `scrypt$n$r$p$salt_b64$hash_b64` ──


def verify_password(password: str, stored: str | None) -> bool:
    """Любая ошибка формата → False."""
    if not stored:
        return False
    try:
        algo, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        if n < 2 or n > 2**20 or r < 1 or r > 32 or p < 1 or p > 16:
            return False
        salt, expected = base64.b64decode(salt_s.encode("ascii")), base64.b64decode(hash_s.encode("ascii"))
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                            maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def looks_like_hash(stored: object) -> bool:
    return isinstance(stored, str) and stored.startswith("scrypt$") and stored.count("$") == 5


# ── токены: `v1.<exp>.<nonce>.<sig>` ──


def _key(secret: str, password_hash: str) -> bytes:
    # смена пароля делает недействительными все старые сессии
    return hmac.new(secret.encode(), password_hash.encode(), hashlib.sha256).digest()


def issue_token(secret: str, password_hash: str, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + SESSION_TTL_S)
    payload = f"v1.{exp}.{_b64u(os.urandom(12))}"
    sig = _b64u(hmac.new(_key(secret, password_hash), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def check_token(token: str | None, secret: str | None, password_hash: str | None,
                now: float | None = None) -> bool:
    if not token or not secret or not password_hash:
        return False
    try:
        ver, exp_s, nonce, sig = token.split(".")
        if ver != "v1":
            return False
        exp = int(exp_s)
    except ValueError:
        return False
    good = _b64u(hmac.new(_key(secret, password_hash), f"{ver}.{exp_s}.{nonce}".encode(),
                          hashlib.sha256).digest())
    if not hmac.compare_digest(good, sig):
        return False
    return exp > (now if now is not None else time.time())


# ── анти-перебор ──


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


class LoginGuard:
    """Как в fnav-core: 5 ошибок с IP → пауза 30 с. Сверху — общий предел на все IP (перебор с многих адресов)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ips: dict[str, _Attempts] = {}
        self._recent: deque[float] = deque()  # моменты неудачных попыток со всех IP

    def wait_s(self, ip: str, now: float | None = None) -> float:
        """Сколько ждать до следующей попытки (0 — можно проверять пароль)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            a = self._ips.get(ip)
            left = max(0.0, a.locked_until - now) if a else 0.0
            while self._recent and self._recent[0] <= now - GLOBAL_WINDOW_S:
                self._recent.popleft()
            if len(self._recent) >= GLOBAL_MAX_FAILURES:
                left = max(left, self._recent[0] + GLOBAL_WINDOW_S - now)
            return left

    def register(self, ip: str, ok: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if ok:
                self._ips.pop(ip, None)
                return
            self._recent.append(now)
            a = self._ips.setdefault(ip, _Attempts())
            if a.locked_until and now >= a.locked_until:
                a.failures = 0
                a.locked_until = 0.0
            a.failures += 1
            if a.failures >= MAX_FAILURES:
                a.locked_until = now + LOCKOUT_S
            if len(self._ips) > 10000:  # не даём словарю расти бесконечно
                self._ips.clear()
