"""Web Push без сторонних push-библиотек: шифрование RFC 8291 (aes128gcm, RFC 8188) и VAPID RFC 8292 (ES256).

Криптография — пакет cryptography (на сервере — apt python3-cryptography), импорт ленивый: без него push выключен
(PushUnavailable с понятной ошибкой), остальной relay работает. Только давно существующие API (на сервере 41,
локально 46). HTTP — urllib. Контракт: docs/contracts/alarm.md, раздел 6.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlsplit

NO_CRYPTO = "Push needs python3-cryptography on the server"
RECORD_SIZE = 4096  # rs в заголовке aes128gcm; запись одна
TAG_LEN = 16
MAX_PAYLOAD = RECORD_SIZE - TAG_LEN - 1  # открытый текст + разделитель 0x02 + тег GCM — в одну запись
JWT_TTL_S = 12 * 3600  # exp в JWT VAPID — не дальше 12 ч (Mozilla и Apple принимают ≤ 24 ч)
TIMEOUT_S = 10.0
ERROR_MAX = 300  # столько символов ответа push-сервиса сохраняется в last_error
MAX_ENDPOINT = 2048


class PushUnavailable(RuntimeError):
    """Нет пакета cryptography: push выключен."""


class InvalidSubscription(ValueError):
    """Подписка браузера с ошибкой; текст — для ответа 400."""


_crypto: SimpleNamespace | None = None


def _load() -> SimpleNamespace:
    """Модули cryptography (импорт при первом обращении)."""
    global _crypto
    if _crypto is None:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as e:
            raise PushUnavailable(NO_CRYPTO) from e
        _crypto = SimpleNamespace(hashes=hashes, serialization=serialization, ec=ec, AESGCM=AESGCM,
                                  decode_dss_signature=decode_dss_signature)
    return _crypto


def require() -> None:
    """PushUnavailable, если cryptography нет."""
    _load()


def available() -> bool:
    try:
        _load()
    except PushUnavailable:
        return False
    return True


# ── base64url ──


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    """base64url с паддингом и без (обычный base64 тоже принимается); мусор → ValueError."""
    s = s.strip().rstrip("=").replace("+", "-").replace("/", "_")
    return base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_", validate=True)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _json(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


# ── ключи P-256 ──


def _private_key(raw: bytes):
    c = _load()
    if len(raw) != 32:
        raise ValueError("P-256 private key must be 32 bytes")
    return c.ec.derive_private_key(int.from_bytes(raw, "big"), c.ec.SECP256R1())


def _public_raw(private_key) -> bytes:
    """Несжатая точка (65 байт, 0x04 || X || Y)."""
    c = _load()
    return private_key.public_key().public_bytes(c.serialization.Encoding.X962,
                                                 c.serialization.PublicFormat.UncompressedPoint)


def _public_key(raw: bytes):
    c = _load()
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("P-256 public key must be an uncompressed point (65 bytes)")
    return c.ec.EllipticCurvePublicKey.from_encoded_point(c.ec.SECP256R1(), raw)


def generate_vapid_keys() -> tuple[str, str]:
    """(закрытый ключ — 32 байта, открытый — 65 байт) в base64url."""
    c = _load()
    key = c.ec.generate_private_key(c.ec.SECP256R1())
    return b64u(key.private_numbers().private_value.to_bytes(32, "big")), b64u(_public_raw(key))


def public_from_private(private_b64: str) -> str:
    return b64u(_public_raw(_private_key(b64u_decode(private_b64))))


# ── VAPID (RFC 8292) ──


@dataclass(frozen=True)
class Vapid:
    private_key: str  # base64url, 32 байта
    public_key: str  # base64url, 65 байт — applicationServerKey подписки в браузере
    sub: str | None = None  # mailto:… или https://… (без него Apple отвечает BadJwtToken)


def origin(endpoint: str) -> str:
    """aud для JWT: схема://хост[:порт] адреса push-сервиса."""
    u = urlsplit(endpoint)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise ValueError("Invalid push endpoint")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    default = 443 if u.scheme == "https" else 80
    port = u.port  # ValueError при кривом порте
    return f"{u.scheme}://{host}" + (f":{port}" if port and port != default else "")


def vapid_jwt(endpoint: str, vapid: Vapid, now: float | None = None) -> str:
    """JWT ES256: {"typ":"JWT","alg":"ES256"} . {aud, exp, sub} . r||s (по 32 байта)."""
    c = _load()
    claims: dict[str, object] = {"aud": origin(endpoint),
                                 "exp": int(time.time() if now is None else now) + JWT_TTL_S}
    if vapid.sub:
        claims["sub"] = vapid.sub
    signing_input = f"{b64u(_json({'typ': 'JWT', 'alg': 'ES256'}))}.{b64u(_json(claims))}"
    der = _private_key(b64u_decode(vapid.private_key)).sign(signing_input.encode("ascii"),
                                                           c.ec.ECDSA(c.hashes.SHA256()))
    r, s = c.decode_dss_signature(der)
    return f"{signing_input}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def vapid_authorization(endpoint: str, vapid: Vapid, now: float | None = None) -> str:
    """Значение заголовка Authorization (RFC 8292 §3)."""
    return f"vapid t={vapid_jwt(endpoint, vapid, now)}, k={vapid.public_key}"


# ── шифрование (RFC 8291 + RFC 8188) ──


def encrypt(payload: bytes, p256dh: str | bytes, auth: str | bytes, *, salt: bytes | None = None,
            server_key: bytes | None = None) -> bytes:
    """Тело запроса `Content-Encoding: aes128gcm`: заголовок (salt, rs, keyid = открытый ключ сервера) + одна запись.

    p256dh и auth — из подписки браузера (base64url или байты). salt и server_key (32 байта закрытого ключа) —
    только для тестов: по умолчанию каждый раз новые.
    """
    c = _load()
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"Push payload is too large ({len(payload)} > {MAX_PAYLOAD} bytes)")
    ua_raw = b64u_decode(p256dh) if isinstance(p256dh, str) else p256dh
    auth_secret = b64u_decode(auth) if isinstance(auth, str) else auth
    ua_public = _public_key(ua_raw)
    key = c.ec.generate_private_key(c.ec.SECP256R1()) if server_key is None else _private_key(server_key)
    as_raw = _public_raw(key)
    salt = os.urandom(16) if salt is None else salt
    if len(salt) != 16:
        raise ValueError("salt must be 16 bytes")
    ecdh_secret = key.exchange(c.ec.ECDH(), ua_public)
    # HKDF-SHA256 (RFC 5869); каждый выход ≤ 32 байт — Expand из одного блока T(1) = HMAC(PRK, info || 0x01)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_raw + as_raw + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    record = c.AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02 — разделитель последней записи
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_raw)) + as_raw + record


# ── подписка браузера ──


def clean_subscription(sub: object, allow_http: bool = False) -> tuple[str, str, str]:
    """PushSubscription.toJSON() → (endpoint, p256dh, auth) в base64url без паддинга.

    InvalidSubscription — с текстом для ответа 400; PushUnavailable — если cryptography нет (ключ не проверить).
    """
    if not isinstance(sub, dict):
        raise InvalidSubscription("'subscription' must be an object")
    endpoint, keys = sub.get("endpoint"), sub.get("keys")
    schemes = ("https", "http") if allow_http else ("https",)
    if (not isinstance(endpoint, str) or len(endpoint) > MAX_ENDPOINT or any(ch.isspace() for ch in endpoint)
            or urlsplit(endpoint).scheme not in schemes):
        raise InvalidSubscription("'subscription.endpoint' must be an https URL")
    try:
        origin(endpoint)
    except ValueError:
        raise InvalidSubscription("'subscription.endpoint' must be an https URL") from None
    if not (isinstance(keys, dict) and isinstance(keys.get("p256dh"), str) and isinstance(keys.get("auth"), str)):
        raise InvalidSubscription("'subscription.keys' must have 'p256dh' and 'auth'")
    try:
        p256dh = b64u_decode(keys["p256dh"])
        _public_key(p256dh)
    except ValueError:
        raise InvalidSubscription("'subscription.keys.p256dh' is not a P-256 public key") from None
    try:
        auth = b64u_decode(keys["auth"])
    except ValueError:
        auth = b""
    if len(auth) != 16:
        raise InvalidSubscription("'subscription.keys.auth' must be 16 bytes")
    return endpoint, b64u(p256dh), b64u(auth)


# ── отправка ──


@dataclass(frozen=True)
class Result:
    status: int | None  # None — до ответа не дошло (сеть, DNS, TLS)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def gone(self) -> bool:
        """Подписки больше нет (отписались, переустановили браузер) — удалить."""
        return self.status in (404, 410)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # push-сервисы не перенаправляют; 3xx — ошибка
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ERROR_MAX else text[:ERROR_MAX - 1] + "…"


def send(endpoint: str, p256dh: str, auth: str, payload: bytes, vapid: Vapid, *, ttl: int = 86400,
         urgency: str = "high", topic: str | None = None, timeout: float = TIMEOUT_S) -> Result:
    """Одно уведомление одной подписке. Сетевые и HTTP-ошибки — в Result, исключения — только PushUnavailable
    и ValueError (битые ключи подписки, слишком длинное сообщение)."""
    body = encrypt(payload, p256dh, auth)
    headers = {
        "Authorization": vapid_authorization(endpoint, vapid),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(int(ttl)),
        "Urgency": urgency,
    }
    if topic:
        headers["Topic"] = topic
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return Result(r.status)
    except urllib.error.HTTPError as e:
        try:
            text = e.read(ERROR_MAX * 4).decode("utf-8", "replace")
        except (OSError, http.client.HTTPException):
            text = ""
        text = _short(text)
        return Result(e.code, f"HTTP {e.code}" + (f": {text}" if text else ""))
    except urllib.error.URLError as e:
        return Result(None, _short(f"Network error: {e.reason}"))
    except (OSError, http.client.HTTPException) as e:
        return Result(None, _short(f"Network error: {type(e).__name__}: {e}"))
