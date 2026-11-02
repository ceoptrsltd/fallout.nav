"""Гостевые ссылки «только просмотр» (docs/contracts/api.md, «Гостевой доступ»).

Владелец создаёт ссылку со сроком; гость открывает её (`admin/login.html#guest=<токен>`), страница входа сразу меняет
токен на cookie гостевой сессии (`POST /api/guest/login`) и убирает его из адресной строки. Гость видит всю админку,
но менять ничего не может: это решает middleware (fnav/core/auth.py), а не кнопки.

Хранится только SHA-256 токена (сам токен показывается владельцу один раз, при создании):
`{data_dir}/guest_links.json`, атомарная запись (как settings.json: 0600, владелец — пользователь сервиса).
Отзыв = удаление записи: cookie гостя перестаёт действовать сразу. Истёкшие ссылки удаляются при следующей записи.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from fnav.core.auth import GUEST_ID_RE
from fnav.core.config import atomic_write_text

log = logging.getLogger("fnav.core.guests")

FILE_NAME = "guest_links.json"
MIN_TTL_S = 10 * 60
MAX_TTL_S = 365 * 86400
MAX_LINKS = 50
LABEL_MAX = 64
TOUCH_SAVE_S = 300.0  # «последний вход» пишется на диск не чаще раза в 5 мин на ссылку (карта памяти)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]{1,253}(:\d{1,5})?$")


class GuestError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


@dataclass
class GuestLink:
    id: str
    token_hash: str
    label: str
    created: float
    expires: float
    last_used: float | None = None

    def public(self) -> dict:
        """Владельцу в списке: без хэша токена."""
        return {"id": self.id, "label": self.label, "created": self.created, "expires": self.expires,
                "last_used": self.last_used}


def _parse(item: object) -> GuestLink | None:
    if not isinstance(item, dict):
        return None
    try:
        link = GuestLink(id=str(item["id"]), token_hash=str(item["token_hash"]), label=str(item.get("label", ""))[:LABEL_MAX],
                         created=float(item["created"]), expires=float(item["expires"]),
                         last_used=None if item.get("last_used") is None else float(item["last_used"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not GUEST_ID_RE.match(link.id) or not _HEX64.match(link.token_hash):
        return None
    return link


class GuestLinks:
    def __init__(self, path: Path, on_change: Callable[[], None] | None = None) -> None:
        self.path = path
        # набор действующих ссылок изменился: сервер (fnav-relay) должен узнать о нём (журнал позиций, push)
        self.on_change = on_change
        self.links: dict[str, GuestLink] = {}
        self.remote_origin: str | None = None  # https://<сервер> — адрес удалённой админки, если её открывали
        self._saved_used: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._load()

    # ── файл ──
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("guest links: cannot read %s (%s) - no guest links until the owner creates one", self.path, e)
            return
        if not isinstance(data, dict):
            return
        for item in data.get("links") or []:
            link = _parse(item)
            if link:
                self.links[link.id] = link
                self._saved_used[link.id] = link.last_used or 0.0
        origin = data.get("remote_origin")
        self.remote_origin = origin if isinstance(origin, str) and _ORIGIN_RE.match(origin) else None

    def _dump(self, now: float) -> str:
        for lid in [lid for lid, x in self.links.items() if x.expires <= now]:
            del self.links[lid]
            self._saved_used.pop(lid, None)
        return json.dumps({"links": [asdict(x) for x in self.links.values()], "remote_origin": self.remote_origin},
                          ensure_ascii=False, indent=2) + "\n"

    async def _persist(self, now: float | None = None) -> None:
        # под замком: снимок и запись по порядку (более старый снимок не перезапишет новый)
        async with self._lock:
            text = self._dump(time.time() if now is None else now)
            for x in self.links.values():
                self._saved_used[x.id] = x.last_used or 0.0
            await asyncio.to_thread(atomic_write_text, self.path, text)

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("guest links: on_change")

    # ── ссылки ──
    def active(self, link_id: str, now: float | None = None) -> bool:
        link = self.links.get(link_id)
        return link is not None and link.expires > (time.time() if now is None else now)

    def list(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        return [x.public() for x in sorted(self.links.values(), key=lambda x: -x.created) if x.expires > now]

    def relay_list(self, now: float | None = None) -> list[dict]:
        """Серверу (fnav-relay): какие гостевые cookie принимать — id и срок, без хэшей."""
        now = time.time() if now is None else now
        return sorted(({"id": x.id, "expires": int(x.expires)} for x in self.links.values() if x.expires > now),
                      key=lambda d: d["id"])

    async def create(self, label: str, ttl_s: float, now: float | None = None) -> tuple[GuestLink, str]:
        """→ (ссылка, токен). Токен больше нигде не хранится — показать владельцу сразу."""
        now = time.time() if now is None else now
        if not MIN_TTL_S <= ttl_s <= MAX_TTL_S:
            raise GuestError(422, f"The link must last from {MIN_TTL_S // 60} minutes to {MAX_TTL_S // 86400} days")
        if len(self.list(now)) >= MAX_LINKS:
            raise GuestError(409, f"Too many guest links (at most {MAX_LINKS}): revoke some first")
        token = secrets.token_urlsafe(32)
        lid = secrets.token_hex(6)
        while lid in self.links:
            lid = secrets.token_hex(6)
        link = GuestLink(id=lid, token_hash=token_hash(token), label=" ".join(label.split())[:LABEL_MAX],
                         created=round(now, 1), expires=round(now + ttl_s, 1))
        self.links[lid] = link
        try:
            await self._persist(now)
        except OSError:
            self.links.pop(lid, None)
            raise
        log.info("guest link %s created (%s), valid until %s", lid, link.label or "no label",
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(link.expires)))
        self._changed()
        return link, token

    async def revoke(self, link_id: str) -> bool:
        link = self.links.pop(link_id, None)
        if link is None:
            return False
        try:
            await self._persist()
        except OSError:
            self.links[link_id] = link
            raise
        log.info("guest link %s revoked", link_id)
        self._changed()
        return True

    def find_token(self, token: str, now: float | None = None) -> GuestLink | None:
        """Ссылка по токену (сравнение хэшей за постоянное время) или None — неверный, отозван, истёк."""
        now = time.time() if now is None else now
        want = token_hash(token)
        found = None
        for link in self.links.values():
            if hmac.compare_digest(link.token_hash, want) and link.expires > now:
                found = link
        return found

    async def touch(self, link_id: str | None, now: float | None = None) -> None:
        """Гость что-то открыл: «последний вход» в памяти сразу, на диск — не чаще TOUCH_SAVE_S."""
        link = self.links.get(link_id or "")
        if link is None:
            return
        now = time.time() if now is None else now
        link.last_used = round(now, 1)
        if now - self._saved_used.get(link.id, 0.0) >= TOUCH_SAVE_S:
            self._saved_used[link.id] = now
            try:
                await self._persist(now)
            except OSError as e:
                log.warning("guest links: cannot save %s: %s", self.path, e)

    async def note_remote_origin(self, origin: str | None) -> None:
        """Адрес удалённой админки (Host от nginx на удалённом входе) — чтобы и с Wi-Fi машины показать ссылку
        «через сервер»."""
        if not origin or not _ORIGIN_RE.match(origin) or origin == self.remote_origin:
            return
        self.remote_origin = origin
        try:
            await self._persist()
        except OSError as e:
            log.warning("guest links: cannot save %s: %s", self.path, e)


# ── что гостю не отдаётся ──

_USERINFO_RE = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", re.IGNORECASE)


def redact_settings(d: dict) -> dict:
    """Настройки для гостя: без ключа Esri и без логина/пароля, если их вписали в адрес RTSP. Остальное — как есть."""
    out = dict(d)
    tiles = out.get("tiles")
    if isinstance(tiles, dict) and tiles.get("esri_api_key"):
        out["tiles"] = {**tiles, "esri_api_key": ""}
    rear = out.get("rear_camera")
    if isinstance(rear, dict):
        clean = {k: _USERINFO_RE.sub(r"\1", v) for k in ("url", "lores_url") if isinstance(v := rear.get(k), str)}
        if any(clean[k] != rear.get(k) for k in clean):
            out["rear_camera"] = {**rear, **clean}
    return out


def redact_message(msg: dict) -> dict:
    """Сообщение WebSocket для гостя (ws.py): настройки — без секретов, остальное без изменений."""
    if msg.get("type") == "settings" and isinstance(msg.get("settings"), dict):
        return {**msg, "settings": redact_settings(msg["settings"])}
    return msg
