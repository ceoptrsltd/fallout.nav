"""Web Push fnav-relay: шифрование RFC 8291 (вектор из приложения A), VAPID RFC 8292, проверка подписки, отправка."""

from __future__ import annotations

import json
import socket

import pytest
from push_helpers import Browser, FakePush, b64d, b64e, verify_vapid
from relay_helpers import webpush

# RFC 8291, приложение A
RFC_PLAINTEXT = "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_AS_PUBLIC = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_RESULT = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK"
              "6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


@pytest.fixture
def fake():
    f = FakePush()
    yield f
    f.close()


@pytest.fixture(scope="module")
def vapid() -> webpush.Vapid:
    priv, pub = webpush.generate_vapid_keys()
    return webpush.Vapid(priv, pub, "https://van.example.org")


# ── шифрование ──


def test_rfc8291_appendix_a_vector():
    assert webpush.public_from_private(RFC_AS_PRIVATE) == RFC_AS_PUBLIC
    assert webpush.public_from_private(RFC_UA_PRIVATE) == RFC_UA_PUBLIC
    body = webpush.encrypt(b64d(RFC_PLAINTEXT), RFC_UA_PUBLIC, RFC_AUTH, salt=b64d(RFC_SALT),
                           server_key=b64d(RFC_AS_PRIVATE))
    assert b64e(body) == RFC_RESULT


def test_round_trip_with_fresh_browser_keys():
    browser = Browser()
    payload = json.dumps({"title": "Impact detected", "body": "Hit 0.42 g at 14:03 — ёж"}).encode()
    body = webpush.encrypt(payload, browser.p256dh, b64e(browser.auth))
    assert int.from_bytes(body[16:20], "big") == webpush.RECORD_SIZE and body[20] == 65 and body[21] == 4
    assert browser.decrypt(body) == payload
    # каждый раз новые salt и ключ сервера
    again = webpush.encrypt(payload, browser.p256dh, b64e(browser.auth))
    assert again[:16] != body[:16] and again[21:86] != body[21:86]
    assert browser.decrypt(again) == payload
    # чужой auth не расшифрует
    other = Browser(key=browser.key)
    with pytest.raises(Exception):  # noqa: B017 — InvalidTag из cryptography
        other.decrypt(body)


def test_max_payload():
    browser = Browser()
    big = b"x" * webpush.MAX_PAYLOAD
    body = webpush.encrypt(big, browser.p256dh, browser.auth)
    assert len(body) == 86 + webpush.RECORD_SIZE  # ровно одна полная запись
    assert browser.decrypt(body) == big
    with pytest.raises(ValueError, match="too large"):
        webpush.encrypt(big + b"x", browser.p256dh, browser.auth)


def test_base64url_decoding_is_strict_but_tolerant_to_padding():
    assert webpush.b64u_decode("BTBZMqHH6r4Tts7J_aSIgg") == webpush.b64u_decode("BTBZMqHH6r4Tts7J/aSIgg==")
    with pytest.raises(ValueError):
        webpush.b64u_decode("not base64!")


# ── VAPID ──


def test_vapid_keys():
    priv, pub = webpush.generate_vapid_keys()
    assert len(b64d(priv)) == 32 and len(b64d(pub)) == 65 and b64d(pub)[0] == 4
    assert webpush.public_from_private(priv) == pub
    assert webpush.generate_vapid_keys()[1] != pub


def test_vapid_jwt(vapid):
    now = 1_761_140_000.7
    endpoint = "https://fcm.googleapis.com/fcm/send/abc:def?x=1"
    head, claims, k = verify_vapid(webpush.vapid_authorization(endpoint, vapid, now=now))
    assert head == {"typ": "JWT", "alg": "ES256"}
    assert claims == {"aud": "https://fcm.googleapis.com", "exp": 1_761_140_000 + 12 * 3600,
                      "sub": "https://van.example.org"}
    assert claims["exp"] - now <= 12 * 3600
    assert k == vapid.public_key
    # aud — origin: порт не по умолчанию остаётся, 443 — нет; без sub claim не пишется
    anon = webpush.Vapid(vapid.private_key, vapid.public_key)
    _, claims, _ = verify_vapid(webpush.vapid_authorization("https://web.push.apple.com:443/QG_x", anon, now=now))
    assert claims == {"aud": "https://web.push.apple.com", "exp": 1_761_140_000 + 12 * 3600}
    _, claims, _ = verify_vapid(webpush.vapid_authorization("http://127.0.0.1:8099/p", anon))
    assert claims["aud"] == "http://127.0.0.1:8099"


def test_vapid_signature_is_checked():
    """Помощник теста действительно проверяет подпись: подмена claims ловится."""
    from cryptography.exceptions import InvalidSignature

    priv, pub = webpush.generate_vapid_keys()
    t = webpush.vapid_jwt("https://updates.push.services.mozilla.com/wpush/v2/x", webpush.Vapid(priv, pub))
    head, _, sig = t.split(".")
    forged = b64e(json.dumps({"aud": "https://evil.example", "exp": 1}).encode())
    with pytest.raises(InvalidSignature):
        verify_vapid(f"vapid t={head}.{forged}.{sig}, k={pub}")


# ── подписка ──


def test_clean_subscription():
    b = Browser()
    ep = "https://web.push.apple.com/QGuQyavXutnMei"
    assert webpush.clean_subscription(b.subscription(ep)) == (ep, b.p256dh, b64e(b.auth))
    padded = {"endpoint": ep, "keys": {"p256dh": b.p256dh + "=", "auth": b64e(b.auth) + "=="}}
    assert webpush.clean_subscription(padded) == (ep, b.p256dh, b64e(b.auth))

    bad = [
        None, "x", {}, {"endpoint": ep}, {"endpoint": "http://push.example/x", "keys": b.subscription(ep)["keys"]},
        {"endpoint": "ftp://push.example/x", "keys": b.subscription(ep)["keys"]},
        {"endpoint": "https://", "keys": b.subscription(ep)["keys"]},
        {"endpoint": "https://a b/", "keys": b.subscription(ep)["keys"]},
        {"endpoint": ep, "keys": {"p256dh": b64e(b"\x04" + b"\x01" * 64), "auth": b64e(b.auth)}},  # не на кривой
        {"endpoint": ep, "keys": {"p256dh": b.p256dh[:-4], "auth": b64e(b.auth)}},
        {"endpoint": ep, "keys": {"p256dh": b.p256dh, "auth": b64e(b"short")}},
        {"endpoint": ep, "keys": {"p256dh": b.p256dh, "auth": "!!"}},
    ]
    for sub in bad:
        with pytest.raises(webpush.InvalidSubscription):
            webpush.clean_subscription(sub)
    # http — только с allow_http (тесты)
    assert webpush.clean_subscription(b.subscription("http://127.0.0.1:1/x"), allow_http=True)[0].startswith("http:")


# ── отправка ──


def test_send_headers_body_and_statuses(fake, vapid):
    b = Browser()
    payload = b'{"title":"t"}'
    r = webpush.send(fake.endpoint("ok"), b.p256dh, b64e(b.auth), payload, vapid, urgency="high", topic="impact")
    assert r.ok and not r.gone and r.status == 201 and r.error is None
    (got,) = fake.received
    assert got.path == "/push/v2/ok"
    h = got.headers
    assert h["content-encoding"] == "aes128gcm" and h["content-type"] == "application/octet-stream"
    assert h["ttl"] == "86400" and h["urgency"] == "high" and h["topic"] == "impact"
    assert int(h["content-length"]) == len(got.body)
    _, claims, _ = verify_vapid(h["authorization"])
    assert claims["aud"] == fake.origin and claims["sub"] == "https://van.example.org"
    assert b.decrypt(got.body) == payload

    fake.status["gone"] = (410, b"")
    fake.status["missing"] = (404, b"")
    fake.status["jwt"] = (403, b'{"reason":"BadJwtToken"}')
    fake.status["busy"] = (500, b"boom\n  and more")
    fake.status["moved"] = (301, b"")
    r = webpush.send(fake.endpoint("gone"), b.p256dh, b64e(b.auth), payload, vapid, ttl=60, urgency="normal")
    assert r.gone and not r.ok and r.status == 410
    assert fake.received[-1].headers["ttl"] == "60" and "topic" not in fake.received[-1].headers
    assert webpush.send(fake.endpoint("missing"), b.p256dh, b64e(b.auth), payload, vapid).gone
    r = webpush.send(fake.endpoint("jwt"), b.p256dh, b64e(b.auth), payload, vapid)
    assert not r.ok and not r.gone and r.error == 'HTTP 403: {"reason":"BadJwtToken"}'
    r = webpush.send(fake.endpoint("busy"), b.p256dh, b64e(b.auth), payload, vapid)
    assert r.status == 500 and r.error == "HTTP 500: boom and more"
    r = webpush.send(fake.endpoint("moved"), b.p256dh, b64e(b.auth), payload, vapid)
    assert r.status == 301 and not r.ok  # перенаправления не выполняются
    assert len(fake.received) == 6


def test_send_network_error(vapid):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # порт свободен — соединение будет отвергнуто
    b = Browser()
    r = webpush.send(f"http://127.0.0.1:{port}/x", b.p256dh, b64e(b.auth), b"{}", vapid, timeout=2)
    assert r.status is None and not r.ok and not r.gone and r.error.startswith("Network error")
