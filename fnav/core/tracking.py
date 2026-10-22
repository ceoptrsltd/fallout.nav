"""Журнал позиций: раз в interval_s берём GPS-точку → outbox (SQLite) → fnav-relay на сервере.

Сервер (docs/contracts/remote.md) сливает одинаковые позиции в «места» и «визиты». Здесь — только выборка
точек, надёжная очередь (переживает обрывы связи и перезагрузки), синхронизация пароля для входа на
сервере и прокси /api/tracking/* для админки на устройстве.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from fnav.models import GpsFix, TrackingSettings

log = logging.getLogger("fnav.core.tracking")

OUTBOX_MAX_ROWS = 200_000  # ~140 суток по точке в минуту без связи; старше — выбрасываем
BATCH = 500
UPLOAD_EVERY_S = 15.0
RETRY_MAX_S = 120.0
MIN_VALID_TS = 1.7e9  # 2023-11: часы ещё не выставлены, если меньше
TOKEN_HEADER = "X-Fnav-Device-Token"


@dataclass(frozen=True)
class RelayConfig:
    """Адрес fnav-relay (через ssh -L туннель) и токен устройства — из /etc/fnav/remote.env."""

    url: str | None = None
    token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @classmethod
    def from_env(cls) -> RelayConfig:
        url = os.environ.get("FNAV_RELAY_URL", "").strip().rstrip("/") or None
        token = os.environ.get("FNAV_RELAY_DEVICE_TOKEN", "").strip() or None
        return cls(url, token)


def fix_time(fix: GpsFix) -> float | None:
    """Время точки по GPS — не зависит от того, успели ли выставить системные часы."""
    if not fix.time_utc:
        return None
    try:
        dt = datetime.fromisoformat(fix.time_utc)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ts = dt.timestamp()
    return ts if ts > MIN_VALID_TS else None


def make_sample(fix: GpsFix, cfg: TrackingSettings, now: float | None = None) -> dict | None:
    """Точка для журнала или None, если фикс негодный (нет, устарел, плохой HDOP, неизвестно время)."""
    if fix.fix not in ("2d", "3d") or fix.lat is None or fix.lon is None:
        return None
    if fix.age_s is not None and fix.age_s > 5.0:
        return None
    if fix.hdop is not None and fix.hdop > cfg.max_hdop:
        return None
    now = time.time() if now is None else now
    ts = fix_time(fix) or (now if now > MIN_VALID_TS else None)
    if ts is None:
        return None

    def r(v: float | None, nd: int) -> float | None:
        return None if v is None else round(v, nd)

    return {"ts": round(ts, 1), "lat": round(fix.lat, 7), "lon": round(fix.lon, 7),
            "speed_mps": r(fix.speed_mps, 2), "course_deg": r(fix.course_deg, 1), "hdop": r(fix.hdop, 2),
            "fix": fix.fix}


class Outbox:
    """Очередь точек на отправку (SQLite, WAL, synchronous=FULL: точка раз в минуту — fsync не жалко)."""

    def __init__(self, path: Path, max_rows: int = OUTBOX_MAX_ROWS) -> None:
        self.path = path
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS outbox("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, body TEXT NOT NULL)")
            self._db = db
        return self._db

    def add(self, sample: dict) -> None:
        with self._lock:
            db = self._conn()
            db.execute("INSERT INTO outbox(ts, body) VALUES(?, ?)", (sample["ts"], json.dumps(sample)))
            (n,) = db.execute("SELECT COUNT(*) FROM outbox").fetchone()
            if n > self.max_rows:
                db.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)",
                           (n - self.max_rows,))

    def peek(self, n: int = BATCH) -> list[tuple[int, dict]]:
        with self._lock:
            rows = self._conn().execute("SELECT id, body FROM outbox ORDER BY id LIMIT ?", (n,)).fetchall()
        return [(i, json.loads(b)) for i, b in rows]

    def drop_upto(self, last_id: int) -> None:
        with self._lock:
            self._conn().execute("DELETE FROM outbox WHERE id <= ?", (last_id,))

    def count(self) -> int:
        with self._lock:
            return int(self._conn().execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


class TrackService:
    """Выборка точек + отправка на сервер + прокси чтения журнала."""

    def __init__(self, outbox_path: Path, relay: RelayConfig, get_auth: Callable[[], tuple[str | None, str]],
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.outbox = Outbox(outbox_path)
        self.relay = relay
        self.get_auth = get_auth  # → (password_hash, session_secret)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), transport=transport,
                                         trust_env=False)
        self._kick = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._auth_synced: tuple[str, str] | None = None
        self.last_sample_ts: float | None = None
        self.last_upload_ts: float | None = None
        self.last_error: str | None = None

    # ── жизненный цикл ──
    def start(self, get_fix: Callable[[], GpsFix], get_settings: Callable[[], TrackingSettings]) -> None:
        self._tasks = [
            asyncio.create_task(self._sample_loop(get_fix, get_settings), name="track-sample"),
            asyncio.create_task(self._upload_loop(), name="track-upload"),
        ]
        log.info("tracking started (relay %s)", self.relay.url if self.relay.configured else "not configured")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        await self._client.aclose()
        await asyncio.to_thread(self.outbox.close)

    # ── выборка ──
    async def record(self, sample: dict) -> None:
        await asyncio.to_thread(self.outbox.add, sample)
        self.last_sample_ts = sample["ts"]
        self._kick.set()

    async def _sample_loop(self, get_fix: Callable[[], GpsFix], get_settings: Callable[[], TrackingSettings]) -> None:
        next_at = 0.0  # monotonic; первая годная точка — сразу (момент появления фикса)
        while True:
            try:
                cfg = get_settings()
                now = time.monotonic()
                if cfg.enabled and now >= next_at:
                    sample = make_sample(get_fix(), cfg)
                    if sample is not None:
                        await self.record(sample)
                        next_at = now + cfg.interval_s
            except Exception:
                log.exception("tracking sample")
            await asyncio.sleep(1.0)

    # ── отправка ──
    def _headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.relay.token or ""}

    async def _sync_auth(self) -> None:
        """Сервер принимает вход тем же паролем и проверяет те же cookie — отдаём ему хэш и секрет."""
        pw_hash, secret = self.get_auth()
        if not pw_hash or not secret or self._auth_synced == (pw_hash, secret):
            return
        r = await self._client.post(f"{self.relay.url}/device/auth", headers=self._headers(),
                                    json={"password_hash": pw_hash, "session_secret": secret})
        r.raise_for_status()
        self._auth_synced = (pw_hash, secret)
        log.info("tracking: login credentials synced to the server")

    async def _flush(self) -> None:
        while True:
            rows = await asyncio.to_thread(self.outbox.peek, BATCH)
            if not rows:
                return
            r = await self._client.post(f"{self.relay.url}/device/samples", headers=self._headers(),
                                        json={"samples": [s for _, s in rows]})
            r.raise_for_status()
            await asyncio.to_thread(self.outbox.drop_upto, rows[-1][0])
            self.last_upload_ts = time.time()
            if len(rows) < BATCH:
                return

    async def upload_once(self) -> None:
        await self._sync_auth()
        await self._flush()

    async def _upload_loop(self) -> None:
        delay = UPLOAD_EVERY_S
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=delay)
            self._kick.clear()
            if not self.relay.configured:
                delay = 60.0
                continue
            try:
                await self.upload_once()
                if self.last_error:
                    log.info("tracking: server reachable again")
                self.last_error = None
                delay = UPLOAD_EVERY_S
            except (httpx.HTTPError, OSError) as e:
                if self.last_error is None:
                    log.warning("tracking: upload failed (%s), will retry", e)
                self.last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                self._auth_synced = None  # после восстановления связи — синхронизировать заново
                delay = min(RETRY_MAX_S, delay * 2)
            except Exception as e:
                log.exception("tracking upload")
                self.last_error = f"{type(e).__name__}: {e}"
                delay = RETRY_MAX_S

    # ── чтение журнала (админка на устройстве) ──
    async def proxy_get(self, path: str, query: str) -> tuple[int, bytes, str]:
        def err(status: int, text: str) -> tuple[int, bytes, str]:
            return status, json.dumps({"detail": text}).encode(), "application/json"

        if not self.relay.configured:
            return err(503, "Tracking server is not configured")
        url = f"{self.relay.url}/api/tracking/{path}" + (f"?{query}" if query else "")
        try:
            r = await self._client.get(url, headers=self._headers())
        except httpx.HTTPError:
            return err(503, "Tracking server is unreachable")
        return r.status_code, r.content, r.headers.get("content-type", "application/json")

    async def status(self) -> dict:
        return {
            "configured": self.relay.configured,
            "outbox": await asyncio.to_thread(self.outbox.count),
            "last_sample_ts": self.last_sample_ts,
            "last_upload_ts": self.last_upload_ts,
            "last_error": self.last_error,
        }
