"""Снимки к push охраны (docs/contracts/alarm.md, раздел 6): миниатюра кадра (~5 КБ JPEG) приходит от устройства вместе
с событием, в push уходит только ссылка на неё (у Web Push предел ~4 КБ на сообщение) — телефон (Android) показывает
большую картинку.

Файл `<token>.jpg` в каталоге рядом с базой relay; token — 24 случайных байта (base64url), ссылка не угадывается и
открывается без входа (картинку уведомления браузер грузит сам, без cookie сессии), пока снимку меньше ttl_s. Время
создания — mtime файла (ставится по часам relay: переживает перезапуск). Лишнее и старое удаляется при каждой записи.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("fnav_relay.snaps")

SNAP_TTL_S = 2 * 86400  # push живёт у push-сервиса до суток (TTL), картинка нужна в момент показа — с запасом
SNAP_MAX_FILES = 500
SNAP_MAX_BYTES = 64 * 1024  # миниатюра ~5 КБ; больше — не снимок к push
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,64}$")


def decode_thumb(v: object) -> bytes | None:
    """Поле thumb события (base64 JPEG) → байты или None (нет, мусор, не JPEG, слишком большой)."""
    if not isinstance(v, str) or not v or len(v) > SNAP_MAX_BYTES * 4 // 3 + 4:
        return None
    try:
        data = base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if data.startswith(b"\xff\xd8") and len(data) <= SNAP_MAX_BYTES else None


class SnapStore:
    def __init__(self, root: Path, ttl_s: float = SNAP_TTL_S, max_files: int = SNAP_MAX_FILES,
                 clock: Callable[[], float] = time.time) -> None:
        self.root, self.ttl_s, self.max_files, self.clock = root, ttl_s, max_files, clock

    def _path(self, token: str) -> Path | None:
        return self.root / f"{token}.jpg" if TOKEN_RE.match(token) else None

    def put(self, jpeg: bytes) -> str | None:
        """Сохранить снимок → token (None — не вышло: push уйдёт без картинки)."""
        token = secrets.token_urlsafe(24)
        p = self.root / f"{token}.jpg"
        now = self.clock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.tmp")
            tmp.write_bytes(jpeg)
            os.utime(tmp, (now, now))
            tmp.replace(p)
        except OSError as e:
            log.warning("push snapshot not saved: %s", e)
            return None
        self.prune()
        return token

    def get(self, token: str) -> tuple[bytes, float] | None:
        """(JPEG, сколько ещё живёт, с) или None — нет такого / устарел."""
        p = self._path(token)
        if p is None:
            return None
        try:
            left = self.ttl_s - (self.clock() - p.stat().st_mtime)
            if left <= 0:
                return None
            return p.read_bytes(), left
        except OSError:
            return None

    def prune(self) -> None:
        """Удалить устаревшие и сверх max_files (старые первыми)."""
        now = self.clock()
        files = []
        with contextlib.suppress(OSError):
            for p in self.root.glob("*.jpg"):
                with contextlib.suppress(OSError):
                    files.append((p.stat().st_mtime, p))
        files.sort()
        extra = max(0, len(files) - self.max_files)
        for i, (mtime, p) in enumerate(files):
            if i < extra or now - mtime >= self.ttl_s:
                with contextlib.suppress(OSError):
                    p.unlink()
