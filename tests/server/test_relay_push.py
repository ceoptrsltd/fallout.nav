"""Охрана и push в fnav-relay: подписки браузеров, /device/alarm (повторы, рассылка в фоне), 404/410 и ошибки
push-сервиса, синхронный тест, сторож связи (notify_offline) с подменённым временем, работа без cryptography."""

from __future__ import annotations

import json
import threading
import time

import pytest
from push_helpers import Browser, FakePush, b64d, verify_vapid
from relay_helpers import (
    DEVICE_TOKEN,
    Config,
    Notifier,
    Relay,
    RelayServer,
    Store,
    alarm,
    webpush,
)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Env:
    def __init__(self, relay: Relay, fake: FakePush, clock: Clock, notifier: Notifier) -> None:
        self.relay, self.fake, self.clock, self.notifier = relay, fake, clock, notifier
        self.browsers: dict[str, Browser] = {}
        self.cookie = relay.session()

    def api(self, method: str, path: str, body: object = None, headers: dict | None = None):
        return self.relay.call(method, "/fnadmin/api/push/" + path, body, {**self.cookie, **(headers or {})})

    def subscribe(self, name: str, label: str | None = None, host: str = "van.example.org") -> int:
        b = self.browsers.setdefault(name, Browser())
        r = self.api("POST", "subscribe",
                     {"subscription": b.subscription(self.fake.endpoint(name)), "name": label or name}, {"Host": host})
        assert r.status == 200 and r.data["ok"] is True, r.data
        return r.data["id"]

    def alarm(self, events: list, armed: bool = True, notify_offline: bool = True):
        r = self.relay.device("POST", "/device/alarm",
                              {"state": {"armed": armed, "notify_offline": notify_offline}, "events": events})
        assert self.notifier.wait_idle()
        return r

    def subs(self) -> list[dict]:
        r = self.api("GET", "subscriptions")
        assert r.status == 200
        return r.data["subscriptions"]

    def pushes(self, n: int | None = None) -> list[tuple[str, dict, dict]]:
        """(имя подписки, тело push, заголовки) по порядку прихода."""
        got = self.fake.wait(n) if n is not None else list(self.fake.received)
        return [(g.name, json.loads(self.browsers[g.name].decrypt(g.body)), g.headers) for g in got]


@pytest.fixture
def env(tmp_path):
    fake = FakePush()
    clock = Clock()
    cfg = Config(listen_host="127.0.0.1", listen_port=0, db_path=str(tmp_path / "relay.db"),
                 device_token=DEVICE_TOKEN, push_allow_http=True)
    st = Store(cfg.db_path)
    notifier = Notifier(st, cfg, clock=clock, watch_every_s=0.02)
    srv = RelayServer(cfg, st, notifier=notifier)
    notifier.start()
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    yield Env(Relay(f"http://127.0.0.1:{srv.server_address[1]}", st, cfg), fake, clock, notifier)
    srv.shutdown()
    srv.server_close()
    th.join(5)
    notifier.stop()
    st.close()
    fake.close()


def event(eid: str, kind: str = "impact", ts: float = 1000.0, **kw) -> dict:
    return {"id": eid, "ts": ts, "kind": kind, "title": "Impact detected", "body": f"Hit 0.42 g ({eid})",
            "url": "#/alarm", "data": {"peak_g": 0.42}, **kw}


# ── вход и маршруты ──


def test_push_api_needs_login(env):
    r = env.relay
    calls = [("GET", "key", None), ("POST", "subscribe", {}), ("POST", "unsubscribe", {"endpoint": "x"}),
             ("GET", "subscriptions", None), ("DELETE", "subscriptions/1", None), ("POST", "test", {})]
    for method, path, body in calls:
        assert r.call(method, "/fnadmin/api/push/" + path, body).status == 401, path
        assert r.call(method, "/api/push/" + path, body, {"Cookie": "fnav_session=v1.9999999999.x.y"}).status == 401
    assert r.call("GET", "/api/push/key", headers={"X-Fnav-Device-Token": "wrong"}).status == 401
    k = r.device("GET", "/api/push/key")  # токен устройства — как у /api/tracking
    assert k.status == 200
    assert env.api("GET", "key").data == k.data
    assert env.api("GET", "nope").status == 404
    assert env.api("POST", "key").status == 405
    assert env.api("GET", "subscribe").status == 405
    assert env.api("POST", "subscriptions/1").status == 405


def test_public_key_is_created_once_and_kept(env, tmp_path):
    pub = env.api("GET", "key").data["public_key"]
    raw = b64d(pub)
    assert len(raw) == 65 and raw[0] == 4
    assert env.api("GET", "key").data["public_key"] == pub
    assert env.relay.store.get_value("vapid_public") == pub
    assert webpush.public_from_private(env.relay.store.get_value("vapid_private")) == pub


# ── подписки ──


def test_subscribe_update_list_unsubscribe_delete(env):
    a = env.subscribe("a", "Pixel 8")
    env.clock.t = 2000.0
    b = env.subscribe("b", "  iPhone \n 15  ", host="other.example.org")
    assert (a, b) == (1, 2)
    assert env.relay.store.get_value("vapid_sub") == "https://van.example.org"  # Host первой подписки

    subs = env.subs()
    assert subs == [
        {"id": 1, "name": "Pixel 8", "created": 1000.0, "last_ok": None, "last_error": None, "host": "127.0.0.1"},
        {"id": 2, "name": "iPhone 15", "created": 2000.0, "last_ok": None, "last_error": None, "host": "127.0.0.1"},
    ]
    # тот же endpoint — обновление ключей и имени, id и created прежние
    env.browsers["a"] = Browser()
    assert env.subscribe("a", "Pixel 8 Pro") == 1
    subs = env.subs()
    assert [(s["id"], s["name"], s["created"]) for s in subs] == [(1, "Pixel 8 Pro", 1000.0), (2, "iPhone 15", 2000.0)]
    assert env.relay.store.push_subs()[0]["p256dh"] == env.browsers["a"].p256dh

    r = env.api("POST", "unsubscribe", {"endpoint": env.fake.endpoint("a")})
    assert r.status == 200 and r.data == {"ok": True}
    assert env.api("POST", "unsubscribe", {"endpoint": env.fake.endpoint("a")}).status == 200  # повтор — не ошибка
    assert env.api("POST", "unsubscribe", {}).status == 400
    assert [s["id"] for s in env.subs()] == [2]

    r = env.api("DELETE", "subscriptions/2")
    assert r.status == 200 and r.data == {"ok": True}
    assert env.api("DELETE", "subscriptions/2").status == 404
    assert env.subs() == []


def test_subscribe_validation(env):
    good = Browser().subscription(env.fake.endpoint("x"))
    bad = [
        {},
        {"subscription": "x"},
        {"subscription": {**good, "endpoint": "ftp://push.example/x"}},
        {"subscription": {"endpoint": good["endpoint"]}},
        {"subscription": {**good, "keys": {"p256dh": "AAAA", "auth": good["keys"]["auth"]}}},
        {"subscription": {**good, "keys": {"p256dh": good["keys"]["p256dh"], "auth": "AAAA"}}},
    ]
    for body in bad:
        r = env.api("POST", "subscribe", body)
        assert r.status == 400 and "subscription" in r.data["detail"], body
    assert env.subs() == []


def test_subscribe_https_only_and_no_sub_from_local_host(relay):
    """Обычная настройка: endpoint только https; Host без публичного имени в sub не попадает."""
    b = Browser()
    cookie = relay.session()
    r = relay.call("POST", "/api/push/subscribe", {"subscription": b.subscription("http://push.example/x")}, cookie)
    assert r.status == 400 and "https" in r.data["detail"]
    r = relay.call("POST", "/api/push/subscribe",
                   {"subscription": b.subscription("https://fcm.googleapis.com/fcm/send/abc"), "name": 5}, cookie)
    assert r.status == 200 and r.data == {"ok": True, "id": 1}
    assert relay.call("GET", "/api/push/subscriptions", headers=cookie).data["subscriptions"][0]["name"] == ""
    assert relay.store.get_value("vapid_sub") is None  # Host: 127.0.0.1:<port>


def test_public_host_rule():
    assert alarm.public_host("Van.Example.org") == "van.example.org"
    assert alarm.public_host("van.example.org:443") == "van.example.org"
    assert alarm.public_host("van.example.org:8443") == "van.example.org:8443"
    for h in (None, "", "localhost", "127.0.0.1:8095", "[::1]:8095", "fnav.localhost", "a b.org", "x.org/evil"):
        assert alarm.public_host(h) is None, h


def test_configured_sub_wins(tmp_path):
    with pytest.raises(ValueError, match="FNAV_RELAY_VAPID_SUB"):
        Config.from_env({"FNAV_RELAY_VAPID_SUB": "mailto:me@localhost"})
    with pytest.raises(ValueError):
        Config.from_env({"FNAV_RELAY_VAPID_SUB": "https://127.0.0.1"})
    cfg = Config.from_env({"FNAV_RELAY_VAPID_SUB": " mailto:owner@example.org ", "FNAV_RELAY_DB": str(tmp_path / "d")})
    assert cfg.vapid_sub == "mailto:owner@example.org"
    st = Store(cfg.db_path)
    try:
        n = Notifier(st, cfg)
        n.remember_host("van.example.org")
        assert st.get_value("vapid_sub") is None and n.vapid().sub == "mailto:owner@example.org"
    finally:
        st.close()


# ── события от устройства ──


def test_device_alarm_needs_token_and_validates(env):
    body = {"state": {"armed": True, "notify_offline": False}, "events": []}
    assert env.relay.call("POST", "/device/alarm", body).status == 403
    assert env.relay.call("POST", "/device/alarm", body, {"X-Fnav-Device-Token": "x"}).status == 403
    assert env.relay.call("POST", "/device/alarm", body, env.cookie).status == 403  # cookie сюда не годится
    assert env.relay.device("GET", "/device/alarm").status == 405
    for bad in ({}, {"state": None}, {"state": {"armed": 1, "notify_offline": False}}, {"state": {"armed": True}},
                {"state": {"armed": True, "notify_offline": False}, "events": {}}):
        r = env.relay.device("POST", "/device/alarm", bad)
        assert r.status == 400, bad
    r = env.relay.device("POST", "/device/alarm", {"state": {"armed": False, "notify_offline": False}})
    assert r.status == 200 and r.data == {"ok": True, "accepted": 0}
    st = env.relay.store.alarm_state()
    assert st == {"armed": False, "notify_offline": False, "last_seen": 1000.0, "offline_sent": False}


def test_new_events_are_pushed_once(env):
    env.subscribe("a")
    pub = env.api("GET", "key").data["public_key"]
    r = env.alarm([event("e1", ts=990.5), event("e2", "tilt", 991.0, title="Van tilted", body="Tilted 4.2° at 14:03")])
    assert r.status == 200 and r.data == {"ok": True, "accepted": 2}
    got = env.pushes(2)
    assert [(n, p) for n, p, _ in got] == [
        ("a", {"title": "Impact detected", "body": "Hit 0.42 g (e1)", "tag": "impact", "url": "#/alarm", "ts": 990.5}),
        ("a", {"title": "Van tilted", "body": "Tilted 4.2° at 14:03", "tag": "tilt", "url": "#/alarm", "ts": 991.0}),
    ]
    h = got[0][2]
    assert h["ttl"] == "86400" and h["urgency"] == "high" and h["topic"] == "impact"
    assert h["content-encoding"] == "aes128gcm"
    _, claims, k = verify_vapid(h["authorization"])
    assert k == pub and claims["aud"] == env.fake.origin and claims["sub"] == "https://van.example.org"
    assert 0 < claims["exp"] - time.time() <= 12 * 3600
    assert env.subs()[0]["last_ok"] == 1000.0

    # повтор из outbox устройства: ничего нового
    r = env.alarm([event("e1", ts=990.5), event("e2", "tilt", 991.0)])
    assert r.data["accepted"] == 0
    time.sleep(0.1)
    assert len(env.fake.received) == 2

    # пачка после обрыва: по одному push на вид (самый новый), битые события пропускаются
    r = env.alarm([event("e3", ts=1001), event("e4", ts=1003, body="last"), event("e5", ts=1002),
                   {"id": "bad id!", "ts": 1, "kind": "impact"}, {"id": "e6", "kind": "impact"}, "junk",
                   event("e7", "move", 1004, url="https://evil.example/")])
    assert r.data["accepted"] == 4
    got = env.pushes(4)[2:]
    assert [(p["tag"], p["body"], p["url"]) for _, p, _ in got] == [("impact", "last", "#/alarm"),
                                                                     ("move", "Hit 0.42 g (e7)", "#/tracking")]

    # test — Urgency normal
    env.alarm([event("t1", "test", 1010, title="Test alarm")])
    _, p, h = env.pushes(5)[-1]
    assert p["tag"] == "test" and h["urgency"] == "normal" and h["topic"] == "test"


JPEG = b"\xff\xd8\xff\xe0" + b"thumb" * 900 + b"\xff\xd9"  # «миниатюра» ~4,5 КБ


def get_raw(relay: Relay, path: str) -> tuple[int, bytes, dict]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(relay.base + path, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_event_snapshot_goes_to_the_phone_as_a_link(env, tmp_path):
    """Миниатюра кадра (thumb, base64) приходит с событием; в push — только ссылка (предел Web Push ~4 КБ) на
    неугадываемый адрес, который открывается без входа (картинку уведомления браузер грузит без cookie) и устаревает."""
    import base64

    env.subscribe("a")
    thumb = base64.b64encode(JPEG).decode()
    r = env.alarm([event("s1", "approach", 999.0, thumb=thumb), event("s2", "tilt", 999.5, thumb="not base64!"),
                   event("s3", "move", 999.7, thumb=base64.b64encode(b"GIF89a").decode())])
    assert r.data["accepted"] == 3
    got = {p["tag"]: p for _, p, _ in env.pushes(3)}
    assert "image" not in got["tilt"] and "image" not in got["move"]  # мусор / не JPEG — push без картинки
    img = got["approach"]["image"]
    assert img.startswith("../api/push/snap/") and img.endswith(".jpg") and len(img) < 80
    assert all(len(g.body) < 4096 for g in env.fake.received)
    token = img.rsplit("/", 1)[1][:-4]
    assert len(token) >= 32

    st, body, headers = get_raw(env.relay, f"/fnadmin/api/push/snap/{token}.jpg")  # без cookie
    assert st == 200 and body == JPEG and headers["Content-Type"] == "image/jpeg"
    assert headers["Cache-Control"].startswith("private, max-age=") and headers["X-Content-Type-Options"] == "nosniff"
    assert get_raw(env.relay, f"/api/push/snap/{token}.jpg")[0] == 200  # и без префикса
    assert get_raw(env.relay, f"/fnadmin/api/push/snap/{token[:-1]}x.jpg")[0] == 404
    assert get_raw(env.relay, "/fnadmin/api/push/snap/short.jpg")[0] == 401  # не адрес снимка — обычный вход
    assert env.relay.call("POST", f"/fnadmin/api/push/snap/{token}.jpg", {}).status == 405
    assert list((tmp_path / "push_snaps").glob("*.jpg")) == [tmp_path / "push_snaps" / f"{token}.jpg"]

    from fnav_relay.snaps import SNAP_TTL_S

    env.clock.t += SNAP_TTL_S + 1  # устарел
    assert get_raw(env.relay, f"/fnadmin/api/push/snap/{token}.jpg")[0] == 404


def test_snap_store_prunes_old_and_extra(tmp_path):
    from fnav_relay.snaps import SnapStore, decode_thumb

    now = [1000.0]
    s = SnapStore(tmp_path / "snaps", ttl_s=100, max_files=3, clock=lambda: now[0])
    tokens = []
    for _ in range(4):
        tokens.append(s.put(JPEG))
        now[0] += 10
    assert s.get(tokens[0]) is None and [s.get(t)[0] for t in tokens[1:]] == [JPEG] * 3  # сверх max_files — старые
    assert s.get(tokens[3])[1] == 100 - 10
    now[0] += 75  # 1115: первому оставшемуся (1010) — 105 с
    s.put(JPEG)  # запись чистит устаревшие
    assert len(list((tmp_path / "snaps").glob("*.jpg"))) == 3 and not (tmp_path / "snaps" / f"{tokens[1]}.jpg").exists()
    assert s.get("../../etc/passwd") is None and s.get("x" * 10) is None
    assert decode_thumb(None) is None and decode_thumb("") is None and decode_thumb("A" * 100_000) is None


def test_gone_and_failing_subscriptions(env):
    for name in ("a", "b", "c"):
        env.subscribe(name)
    env.fake.status["b"] = (410, b"")
    env.fake.status["c"] = (500, b"internal error")
    env.alarm([event("e1")])
    assert sorted(g.name for g in env.fake.wait(3)) == ["a", "b", "c"]
    subs = {s["id"]: s for s in env.subs()}
    assert sorted(subs) == [1, 3]  # 410 — подписка удалена
    assert subs[1]["last_ok"] == 1000.0 and subs[1]["last_error"] is None
    assert subs[3]["last_ok"] is None and subs[3]["last_error"] == "HTTP 500: internal error"

    env.fake.status["c"] = (201, b"")  # push-сервис ожил
    env.clock.t = 1100.0
    env.alarm([event("e2", ts=1100)])
    env.fake.wait(5)
    subs = {s["id"]: s for s in env.subs()}
    assert subs[3]["last_ok"] == 1100.0 and subs[3]["last_error"] is None


def test_push_test_endpoint(env):
    env.subscribe("a", "Pixel")
    env.subscribe("c", "iPad")
    env.subscribe("g", "Old phone")
    env.fake.status["c"] = (403, b'{"reason":"BadJwtToken"}')
    env.fake.status["g"] = (404, b"")
    r = env.api("POST", "test", {"endpoint": None})
    assert r.status == 200 and r.data == {
        "ok": True, "sent": 1, "failed": 2,
        "errors": ['iPad: HTTP 403: {"reason":"BadJwtToken"}', "Old phone: subscription expired (HTTP 404), removed"]}
    name, p, h = env.pushes(3)[0]
    assert name == "a" and p["title"] == "Test notification" and p["tag"] == "test" and p["url"] == "#/alarm"
    assert h["urgency"] == "normal" and h["topic"] == "test"
    assert [s["name"] for s in env.subs()] == ["Pixel", "iPad"]

    r = env.api("POST", "test", {"endpoint": env.fake.endpoint("a")})  # только этому устройству
    assert r.data == {"ok": True, "sent": 1, "failed": 0, "errors": []}
    assert [g.name for g in env.fake.received] == ["a", "c", "g", "a"]
    assert env.api("POST", "test", {}).data["sent"] == 1  # без тела — всем
    assert env.api("POST", "test", {"endpoint": env.fake.endpoint("g")}).status == 404
    assert env.api("POST", "test", {"endpoint": 5}).status == 400


def test_push_test_without_subscriptions(env):
    assert env.api("POST", "test", {}).data == {"ok": True, "sent": 0, "failed": 0, "errors": []}


# ── сторож связи ──


def test_offline_watchdog(env):
    env.subscribe("a")
    env.alarm([], armed=True, notify_offline=True)  # last_seen = 1000
    env.clock.t = 1179.0
    time.sleep(0.15)  # сторож проверяет каждые 0,02 с
    assert env.fake.received == []
    env.clock.t = 1180.0
    (got,) = env.pushes(1)
    assert got[1] == {"title": "Van went offline", "body": "No contact for 3 min while the alarm is armed",
                      "tag": "offline", "url": "#/tracking", "ts": 1180.0}
    assert got[2]["urgency"] == "high" and got[2]["topic"] == "offline"
    env.clock.t = 5000.0
    time.sleep(0.15)
    assert len(env.fake.received) == 1  # один раз за пропажу

    env.clock.t = 1000.0 + 25 * 60
    env.alarm([], armed=True, notify_offline=True)
    _, p, _ = env.pushes(2)[1]
    assert p == {"title": "Van is back online", "body": "Contact restored after 25 min", "tag": "offline",
                 "url": "#/tracking", "ts": 2500.0}
    assert env.relay.store.alarm_state()["offline_sent"] is False

    # без notify_offline или не на охране — молчим
    env.alarm([], armed=True, notify_offline=False)
    env.clock.t += 1000
    time.sleep(0.15)
    env.alarm([], armed=False, notify_offline=True)
    env.clock.t += 1000
    time.sleep(0.15)
    assert len(env.fake.received) == 2


def test_offline_watchdog_counts_from_relay_start(tmp_path):
    """После перезапуска relay молчание отсчитывается не раньше запуска (иначе ложный «went offline»)."""
    fake = FakePush()
    st = Store(str(tmp_path / "r.db"))
    try:
        b = Browser()
        st.push_subscribe(fake.endpoint("a"), b.p256dh, webpush.b64u(b.auth), "a", 1.0)
        st.set_alarm_state({"armed": True, "notify_offline": True, "last_seen": 1000.0, "offline_sent": False})
        clock = Clock(5000.0)
        n = Notifier(st, Config(push_allow_http=True), clock=clock, watch_every_s=3600)
        n.start()
        try:
            assert n.check_offline() is False
            clock.t = 5179.0
            assert n.check_offline() is False
            clock.t = 5180.0
            assert n.check_offline() is True
            assert n.check_offline() is False
            assert n.wait_idle()
            (got,) = fake.received
            assert json.loads(b.decrypt(got.body))["title"] == "Van went offline"
        finally:
            n.stop()
        assert st.alarm_state()["offline_sent"] is True  # переживает перезапуск: второго push не будет
    finally:
        st.close()
        fake.close()


# ── без cryptography ──


def test_without_cryptography(env, monkeypatch):
    env.subscribe("a")

    def no_crypto():
        raise webpush.PushUnavailable(webpush.NO_CRYPTO)

    monkeypatch.setattr(webpush, "_load", no_crypto)
    assert not webpush.available()
    for method, path, body in (("GET", "key", None), ("POST", "test", {}),
                               ("POST", "subscribe", {"subscription": Browser().subscription(env.fake.endpoint("z"))})):
        r = env.api(method, path, body)
        assert r.status == 503 and r.data == {"detail": "Push needs python3-cryptography on the server"}, path
    assert len(env.subs()) == 1  # список и удаление работают
    r = env.alarm([event("e1")])  # событие принимается, рассылка не падает
    assert r.data == {"ok": True, "accepted": 1}
    assert env.fake.received == []
    assert env.api("DELETE", "subscriptions/1").status == 200


# ── хранилище ──


def test_store_dedupe_and_vapid_survive_reopen(tmp_path):
    path = str(tmp_path / "s.db")
    st = Store(path)
    ev = [alarm.clean_event(event("e1")), alarm.clean_event(event("e2"))]
    assert [e["id"] for e in st.alarm_new_events(ev, now=1000)] == ["e1", "e2"]
    assert st.alarm_new_events(ev + ev, now=1001) == []
    pub = Notifier(st, Config()).vapid().public_key
    st.close()
    st = Store(path)
    try:
        assert st.alarm_new_events(ev, now=1002) == []
        assert Notifier(st, Config()).vapid().public_key == pub
        # старые id забываются через SEEN_KEEP_S
        from fnav_relay.store import SEEN_KEEP_S

        st.alarm_new_events([], now=1000 + SEEN_KEEP_S + 1)
        assert len(st.alarm_new_events(ev, now=1000 + SEEN_KEEP_S + 2)) == 2
    finally:
        st.close()


def test_clean_event():
    e = alarm.clean_event(event("abc_DEF-1", "tilt", 5.5, title=" Van\n tilted ", body=None, url=None))
    assert e == {"id": "abc_DEF-1", "ts": 5.5, "kind": "tilt", "title": "Van tilted", "body": "", "url": "#/alarm"}
    assert alarm.clean_event(event("x", "move", url="//evil.example"))["url"] == "#/tracking"
    assert alarm.clean_event(event("x", title=""))["title"] == "Alarm"
    for bad in (None, [], event("x" * 65), event("x", "Impact"), event("x", "a" * 33), event("x", ts=0),
                event("x", ts=True), event("x", ts=float("inf")), {**event("x"), "id": 5}):
        assert alarm.clean_event(bad) is None, bad
