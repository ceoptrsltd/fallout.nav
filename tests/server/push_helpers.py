"""Помощники тестов Web Push: поддельный push-сервис, ключи «браузера», расшифровка и проверка JWT VAPID.

Расшифровка и проверка подписи написаны независимо от fnav_relay.webpush (HKDF — из cryptography), чтобы тесты
ловили ошибки, а не повторяли их. Нужен пакет cryptography: без него conftest.py эти тесты не собирает.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def raw_public(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


@dataclass
class Browser:
    """Ключи подписки, как их делает браузер (PushSubscription.keys)."""

    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    auth: bytes = field(default_factory=lambda: os.urandom(16))

    @property
    def p256dh(self) -> str:
        return b64e(raw_public(self.key))

    def subscription(self, endpoint: str) -> dict:
        return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": self.p256dh, "auth": b64e(self.auth)}}

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8291/8188: заголовок (salt, rs, keyid) + одна запись → открытый текст."""
        salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
        keyid, record = body[21:21 + idlen], body[21 + idlen:]
        assert len(record) <= rs
        server_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keyid)
        ecdh = self.key.exchange(ec.ECDH(), server_pub)
        ikm = HKDF(hashes.SHA256(), 32, salt=self.auth,
                   info=b"WebPush: info\x00" + raw_public(self.key) + keyid).derive(ecdh)
        cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, record, None).rstrip(b"\x00")
        assert plain.endswith(b"\x02"), "last record must end with the 0x02 delimiter"
        return plain[:-1]


def verify_vapid(authorization: str) -> tuple[dict, dict, str]:
    """`vapid t=<jwt>, k=<key>` → (заголовок JWT, claims, k); подпись ES256 проверяется ключом k."""
    scheme, _, rest = authorization.partition(" ")
    assert scheme == "vapid"
    parts = dict(p.strip().split("=", 1) for p in rest.split(","))
    head, claims, sig = parts["t"].split(".")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(parts["k"]))
    raw = b64d(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # InvalidSignature — провал
    return json.loads(b64d(head)), json.loads(b64d(claims)), parts["k"]


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


class FakePush:
    """Push-сервис на 127.0.0.1: запоминает запросы; ответ по имени подписки — `status[name] = (код, тело)`."""

    def __init__(self) -> None:
        self.received: list[Received] = []
        self.status: dict[str, tuple[int, bytes]] = {}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with fake._lock:
                    fake.received.append(Received(self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                    code, text = fake.status.get(self.path.rsplit("/", 1)[-1], (201, b""))
                self.send_response(code)
                self.send_header("Content-Length", str(len(text)))
                self.end_headers()
                self.wfile.write(text)

            def log_message(self, format: str, *args) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self._th = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._th.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def endpoint(self, name: str) -> str:
        return f"{self.origin}/push/v2/{name}"

    def wait(self, n: int, timeout: float = 5.0) -> list[Received]:
        end = time.monotonic() + timeout
        while len(self.received) < n and time.monotonic() < end:
            time.sleep(0.01)
        return list(self.received)

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self._th.join(5)
