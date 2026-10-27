"""Охрана на сервере: события от устройства → Web Push в фоне; сторож связи для notify_offline.

Контракт: docs/contracts/alarm.md, разделы 5–6. Потоки: рассылка (очередь сообщений) и сторож (раз в WATCH_EVERY_S).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable

from . import webpush
from .config import Config, valid_vapid_sub
from .store import Store, _num

log = logging.getLogger("fnav_relay.alarm")

PUSH_TTL_S = 86400
OFFLINE_AFTER_S = 180.0  # устройство на охране молчит столько — push «Van went offline»
WATCH_EVERY_S = 10.0
CONNECTION_TAG = "offline"  # «went offline» и «back online» — один tag: второе заменяет первое на телефоне

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")  # заодно годится для заголовка Topic (≤ 32, base64url)


def _text(v: object, limit: int) -> str:
    return " ".join(v.split())[:limit] if isinstance(v, str) else ""


def clean_event(e: object) -> dict | None:
    """AlarmEvent от устройства → проверенный словарь или None (без id/kind/ts)."""
    if not isinstance(e, dict):
        return None
    eid, kind, ts = e.get("id"), e.get("kind"), _num(e.get("ts"))
    if not (isinstance(eid, str) and _ID_RE.match(eid) and isinstance(kind, str) and _KIND_RE.match(kind)
            and ts is not None and ts > 0):
        return None
    url = e.get("url")
    if not (isinstance(url, str) and url.startswith("#") and len(url) <= 200 and not any(c.isspace() for c in url)):
        url = "#/tracking" if kind == "move" else "#/alarm"  # только адреса внутри админки
    return {"id": eid, "ts": ts, "kind": kind, "title": _text(e.get("title"), 120) or "Alarm",
            "body": _text(e.get("body"), 500), "url": url}


def message(title: str, body: str, tag: str, url: str, ts: float) -> dict:
    """Тело push (JSON), его читает sw.js админки."""
    return {"title": title, "body": body, "tag": tag, "url": url, "ts": ts}


def public_host(host: str | None) -> str | None:
    """Заголовок Host запроса → имя для sub `https://<host>` или None (localhost, IP, мусор)."""
    h = (host or "").strip().lower().removesuffix(":443")
    return h if h and valid_vapid_sub(f"https://{h}") else None


def _duration(s: float) -> str:
    m = max(1, int(s // 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} d"


class Notifier:
    def __init__(self, store: Store, cfg: Config, clock: Callable[[], float] = time.time,
                 watch_every_s: float = WATCH_EVERY_S, offline_after_s: float = OFFLINE_AFTER_S) -> None:
        self.store, self.cfg, self.clock = store, cfg, clock
        self.watch_every_s, self.offline_after_s = watch_every_s, offline_after_s
        self._q: queue.Queue[tuple[dict, str] | None] = queue.Queue()
        self._stop = threading.Event()
        self._state_lock = threading.Lock()  # состояние устройства: приём от устройства и сторож
        self._key_lock = threading.Lock()
        self._idle = threading.Condition()
        self._pending = 0
        self._threads: list[threading.Thread] = []
        self._started = clock()

    # ── потоки ──
    def start(self) -> None:
        self._started = self.clock()  # сторож отсчитывает молчание не раньше запуска relay
        self._stop.clear()
        if webpush.available():
            n = len(self.store.push_subs())
            log.info("push: VAPID key %s..., %d subscription(s)", self.vapid().public_key[:12], n)
        else:
            log.warning("push is off: %s", webpush.NO_CRYPTO)
        self._threads = [threading.Thread(target=self._worker, name="push", daemon=True),
                         threading.Thread(target=self._watch, name="offline-watch", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Сторож — сразу; рассылка — после уже поставленных в очередь сообщений (не дольше timeout)."""
        self._stop.set()
        self._q.put(None)
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads = []

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Дождаться, пока очередь рассылки опустеет (тесты)."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def _enqueue(self, msg: dict, urgency: str = "high") -> None:
        with self._idle:
            self._pending += 1
        self._q.put((msg, urgency))

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            msg, urgency = item
            try:
                res = self.broadcast(msg, urgency=urgency)
                if res["sent"] or res["failed"]:
                    log.info("push %s: sent %d, failed %d", msg["tag"], res["sent"], res["failed"])
                for err in res["errors"]:
                    log.warning("push %s: %s", msg["tag"], err)
            except webpush.PushUnavailable:
                log.warning("push %s not sent: %s", msg["tag"], webpush.NO_CRYPTO)
            except Exception:
                log.exception("push %s failed", msg["tag"])
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def _watch(self) -> None:
        while not self._stop.wait(self.watch_every_s):
            try:
                self.check_offline()
            except Exception:
                log.exception("offline watchdog failed")

    # ── ключи VAPID ──
    def vapid(self) -> webpush.Vapid:
        """Ключи VAPID (создаются при первом обращении и хранятся в базе). PushUnavailable — без cryptography."""
        webpush.require()
        with self._key_lock:
            priv, pub = self.store.get_value("vapid_private"), self.store.get_value("vapid_public")
            if not priv or not pub:
                priv, pub = webpush.generate_vapid_keys()
                self.store.set_values({"vapid_private": priv, "vapid_public": pub})
                log.info("generated VAPID keys")
        return webpush.Vapid(priv, pub, self.sub())

    def sub(self) -> str | None:
        return self.cfg.vapid_sub or self.store.get_value("vapid_sub")

    def remember_host(self, host: str | None) -> None:
        """sub по умолчанию — https://<Host> первой подписки (сохраняется)."""
        if self.sub():
            return
        h = public_host(host)
        if h:
            sub = self.store.set_default("vapid_sub", f"https://{h}")
            log.info("push: VAPID sub is %s (set FNAV_RELAY_VAPID_SUB to change)", sub)
        else:
            log.warning("push: Host %r is not a public name, VAPID JWT goes without 'sub' "
                        "(Apple may reject it); set FNAV_RELAY_VAPID_SUB", host)

    # ── рассылка ──
    def broadcast(self, msg: dict, *, endpoint: str | None = None, urgency: str = "high") -> dict:
        """Отправить всем подпискам (или одной — по endpoint) синхронно → {"sent", "failed", "errors"}.

        404/410 от push-сервиса — подписка удаляется; прочие ошибки — в last_error подписки.
        """
        vapid = self.vapid()
        subs = self.store.push_subs()
        if endpoint is not None:
            subs = [s for s in subs if s["endpoint"] == endpoint]
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        sent, errors = 0, []
        for s in subs:
            label = s["name"] or s["host"]
            try:
                r = webpush.send(s["endpoint"], s["p256dh"], s["auth"], payload, vapid, ttl=PUSH_TTL_S,
                                 urgency=urgency, topic=msg.get("tag"))
            except ValueError as e:
                r = webpush.Result(None, f"Invalid subscription: {e}")
            if r.ok:
                self.store.push_result(s["endpoint"], self.clock())
                sent += 1
            elif r.gone:
                self.store.push_unsubscribe(s["endpoint"])
                log.info("push: subscription %d (%s) is gone (HTTP %s), removed", s["id"], s["host"], r.status)
                errors.append(f"{label}: subscription expired (HTTP {r.status}), removed")
            else:
                self.store.push_result(s["endpoint"], None, r.error)
                errors.append(f"{label}: {r.error}")
        return {"sent": sent, "failed": len(subs) - sent, "errors": errors}

    def test_message(self) -> dict:
        return message("Test notification", "Alarm notifications work on this device", "test", "#/alarm",
                       self.clock())

    # ── устройство ──
    def device_report(self, state: dict, events: list) -> int:
        """POST /device/alarm: состояние охраны (пульс) и события. Новые события — в очередь рассылки.

        Возвращает число новых (ещё не виденных) событий. Битые события пропускаются.
        """
        now = self.clock()
        clean = [c for c in map(clean_event, events) if c is not None]
        if len(clean) < len(events):
            log.warning("alarm: %d malformed event(s) skipped", len(events) - len(clean))
        new = self.store.alarm_new_events(clean, now)
        with self._state_lock:
            prev = self.store.alarm_state()
            self.store.set_alarm_state({"armed": state["armed"], "notify_offline": state["notify_offline"],
                                        "last_seen": now, "offline_sent": False})
        if prev.get("offline_sent"):
            silent = now - (prev.get("last_seen") or now)
            log.info("alarm: device is back after %.0f s", silent)
            self._enqueue(message("Van is back online", f"Contact restored after {_duration(silent)}",
                                  CONNECTION_TAG, "#/tracking", now))
        # в одной пачке (outbox после обрыва) — по одному push на вид: на телефоне tag всё равно оставит последнее
        latest: dict[str, dict] = {}
        for e in new:
            if e["kind"] not in latest or e["ts"] >= latest[e["kind"]]["ts"]:
                latest[e["kind"]] = e
        for e in sorted(latest.values(), key=lambda e: e["ts"]):
            log.info("alarm event %s: %s", e["kind"], e["body"] or e["title"])
            self._enqueue(message(e["title"], e["body"], e["kind"], e["url"], e["ts"]),
                          "normal" if e["kind"] == "test" else "high")
        return len(new)

    def check_offline(self) -> bool:
        """Один шаг сторожа: устройство на охране (с notify_offline) молчит ≥ offline_after_s → один push."""
        now = self.clock()
        with self._state_lock:
            st = self.store.alarm_state()
            last = st.get("last_seen")
            if st.get("offline_sent") or not (st.get("armed") and st.get("notify_offline")) or last is None:
                return False
            if now - max(last, self._started) < self.offline_after_s:
                return False
            st["offline_sent"] = True
            self.store.set_alarm_state(st)
        log.warning("alarm: no contact with the armed device for %.0f s", now - last)
        self._enqueue(message("Van went offline",
                              f"No contact for {_duration(self.offline_after_s)} while the alarm is armed",
                              CONNECTION_TAG, "#/tracking", now))
        return True
