"""Журнал позиций в SQLite: места и визиты (одинаковые позиции не пишутся), плюс служебные значения;
подписки Web Push и события охраны (docs/contracts/alarm.md, раздел 6).

Слияние точки (в порядке ts):
  1. рядом (≤ radius_m) с местом текущего визита → продлить визит;
  2. иначе ближайшее известное место в радиусе → новый визит в нём;
  3. иначе новое место (координаты этой точки) и новый визит.
Перерыв в данных визит не разрывает, если точка снова в том же месте.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlsplit

EARTH_R_M = 6371008.8
M_PER_DEG_LAT = 111320.0
_FAR = 1e300  # «всё время» для фильтра по диапазону

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS places_latlon ON places(lat, lon);
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY,
    place_id INTEGER NOT NULL REFERENCES places(id),
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    samples INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS visits_place ON visits(place_id, start_ts);
CREATE INDEX IF NOT EXISTS visits_start ON visits(start_ts);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subs (
    id INTEGER PRIMARY KEY,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    last_ok REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS alarm_seen (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    received REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS alarm_seen_received ON alarm_seen(received);
"""
SEEN_KEEP_S = 30 * 24 * 3600  # столько помнятся id событий охраны (повторы из outbox устройства)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def clean_sample(s: Any) -> dict | None:
    """Проверенная точка или None (без ts/координат, вне диапазона, NaN)."""
    if not isinstance(s, dict):
        return None
    ts, lat, lon = _num(s.get("ts")), _num(s.get("lat")), _num(s.get("lon"))
    if ts is None or ts <= 0 or lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None
    fix = s.get("fix")
    return {
        "ts": ts, "lat": lat, "lon": lon,
        "speed_mps": _num(s.get("speed_mps")),
        "course_deg": _num(s.get("course_deg")),
        "hdop": _num(s.get("hdop")),
        "fix": fix if isinstance(fix, str) else None,
    }


def _stats(row: sqlite3.Row, stop_min_s: float) -> dict:
    longest = row["longest_s"] or 0.0
    return {
        "id": row["id"], "lat": row["lat"], "lon": row["lon"],
        "kind": "stop" if longest >= stop_min_s else "transit",
        "visits": row["visits"], "first_ts": row["first_ts"], "last_ts": row["last_ts"],
        "longest_s": longest, "total_s": row["total_s"] or 0.0,
    }


_STATS_SQL = """
SELECT p.id, p.lat, p.lon, COUNT(v.id) AS visits, MIN(v.start_ts) AS first_ts, MAX(v.end_ts) AS last_ts,
       MAX(v.end_ts - v.start_ts) AS longest_s, SUM(v.end_ts - v.start_ts) AS total_s
FROM places p JOIN visits v ON v.place_id = p.id
"""


class Store:
    def __init__(self, path: str, radius_m: float = 50.0, stop_min_s: float = 300.0) -> None:
        self.radius_m = radius_m
        self.stop_min_s = stop_min_s
        self._lock = threading.Lock()  # одно соединение на все потоки HTTP-сервера
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._auth = (self._get("password_hash"), self._get("session_secret"))

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ── служебные значения ──
    def _get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def auth(self) -> tuple[str | None, str | None]:
        """(хэш пароля, секрет сессий), присланные устройством."""
        return self._auth

    def set_auth(self, password_hash: str, session_secret: str) -> None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._set("password_hash", password_hash)
                self._set("session_secret", session_secret)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._auth = (password_hash, session_secret)

    # ── приём точек ──
    def _nearest_place(self, lat: float, lon: float) -> sqlite3.Row | None:
        dlat = self.radius_m / M_PER_DEG_LAT
        c = math.cos(math.radians(lat))
        dlon = 180.0 if c < 1e-6 else min(180.0, self.radius_m / (M_PER_DEG_LAT * c))
        rows = self.db.execute(
            "SELECT id, lat, lon FROM places WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
        best, best_d = None, self.radius_m
        for r in rows:
            d = haversine_m(lat, lon, r["lat"], r["lon"])
            if d <= best_d:
                best, best_d = r, d
        return best

    def ingest(self, samples: list, now: float | None = None) -> dict:
        """Принять пачку точек. Точки с ts ≤ последней обработанной пропускаются (повтор безопасен)."""
        clean = sorted((c for c in map(clean_sample, samples) if c), key=lambda c: c["ts"])
        skipped = len(samples) - len(clean)
        accepted = 0
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                v = self._get("last_ts")
                last_ts = float(v) if v is not None else None
                cur = self.db.execute(
                    "SELECT v.id, p.lat, p.lon FROM visits v JOIN places p ON p.id = v.place_id "
                    "ORDER BY v.id DESC LIMIT 1").fetchone()
                last = None
                for s in clean:
                    if last_ts is not None and s["ts"] <= last_ts:
                        skipped += 1
                        continue
                    if cur is not None and haversine_m(s["lat"], s["lon"], cur["lat"], cur["lon"]) <= self.radius_m:
                        self.db.execute("UPDATE visits SET end_ts = ?, samples = samples + 1 WHERE id = ?",
                                        (s["ts"], cur["id"]))
                    else:
                        place = self._nearest_place(s["lat"], s["lon"])
                        if place is None:
                            pid = self.db.execute("INSERT INTO places(lat, lon, created_ts) VALUES(?, ?, ?)",
                                                  (s["lat"], s["lon"], s["ts"])).lastrowid
                            plat, plon = s["lat"], s["lon"]
                        else:
                            pid, plat, plon = place["id"], place["lat"], place["lon"]
                        vid = self.db.execute(
                            "INSERT INTO visits(place_id, start_ts, end_ts, samples) VALUES(?, ?, ?, 1)",
                            (pid, s["ts"], s["ts"])).lastrowid
                        cur = {"id": vid, "lat": plat, "lon": plon}
                    last_ts, last = s["ts"], s
                    accepted += 1
                if last is not None:
                    self._set("last_ts", repr(last_ts))
                    self._set("last_sample", json.dumps({
                        "ts": last["ts"], "lat": last["lat"], "lon": last["lon"],
                        "speed_mps": last["speed_mps"], "course_deg": last["course_deg"],
                        "received_ts": time.time() if now is None else now,
                    }))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return {"accepted": accepted, "skipped": skipped, "last_ts": last_ts}

    # ── запросы ──
    @staticmethod
    def _range(t0: float | None, t1: float | None) -> tuple[float, float]:
        return (-_FAR if t0 is None else t0), (_FAR if t1 is None else t1)

    def places(self, t0: float | None = None, t1: float | None = None) -> list[dict]:
        """Места с визитами, пересекающими [t0, t1]; счётчики и длительности — по этим визитам."""
        lo, hi = self._range(t0, t1)
        with self._lock:
            rows = self.db.execute(_STATS_SQL + "WHERE v.start_ts <= ? AND v.end_ts >= ? "
                                   "GROUP BY p.id ORDER BY last_ts, p.id", (hi, lo)).fetchall()
        return [_stats(r, self.stop_min_s) for r in rows]

    def place(self, place_id: int) -> dict | None:
        """Место (счётчики за всё время) и все его визиты, новые первыми."""
        with self._lock:
            row = self.db.execute(_STATS_SQL + "WHERE p.id = ? GROUP BY p.id", (place_id,)).fetchone()
            if row is None:
                return None
            visits = self.db.execute(
                "SELECT id, start_ts, end_ts, samples FROM visits WHERE place_id = ? "
                "ORDER BY start_ts DESC, id DESC", (place_id,)).fetchall()
        return {
            "place": _stats(row, self.stop_min_s),
            "visits": [{"id": v["id"], "start_ts": v["start_ts"], "end_ts": v["end_ts"],
                        "duration_s": v["end_ts"] - v["start_ts"], "samples": v["samples"]} for v in visits],
        }

    def track(self, t0: float | None = None, t1: float | None = None) -> list[list[float]]:
        """Визиты по времени: [[start_ts, lat, lon], …] — линия маршрута."""
        lo, hi = self._range(t0, t1)
        with self._lock:
            rows = self.db.execute(
                "SELECT v.start_ts, p.lat, p.lon FROM visits v JOIN places p ON p.id = v.place_id "
                "WHERE v.start_ts <= ? AND v.end_ts >= ? ORDER BY v.start_ts, v.id", (hi, lo)).fetchall()
        return [[r[0], r[1], r[2]] for r in rows]

    def last(self) -> dict:
        with self._lock:
            v = self._get("last_sample")
        return json.loads(v) if v else {}

    # ── служебные значения (ключи VAPID, sub, состояние охраны) ──
    def get_value(self, key: str) -> str | None:
        with self._lock:
            return self._get(key)

    def set_values(self, values: dict[str, str]) -> None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for k, v in values.items():
                    self._set(k, v)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def set_default(self, key: str, value: str) -> str:
        """Записать, если ключа ещё нет; вернуть то, что в итоге лежит в базе."""
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (key, value))
            return self._get(key) or value

    def alarm_state(self) -> dict:
        """Последнее состояние охраны от устройства: {armed, notify_offline, last_seen, offline_sent} или {}."""
        v = self.get_value("alarm_state")
        return json.loads(v) if v else {}

    def set_alarm_state(self, state: dict) -> None:
        self.set_values({"alarm_state": json.dumps(state)})

    # ── события охраны ──
    def alarm_new_events(self, events: list[dict], now: float | None = None) -> list[dict]:
        """Запомнить id событий; вернуть только новые (повтор из outbox устройства — пропуск)."""
        now = time.time() if now is None else now
        new = []
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for e in events:
                    cur = self.db.execute("INSERT OR IGNORE INTO alarm_seen(id, ts, kind, received) "
                                          "VALUES(?, ?, ?, ?)", (e["id"], e["ts"], e["kind"], now))
                    if cur.rowcount == 1:
                        new.append(e)
                self.db.execute("DELETE FROM alarm_seen WHERE received < ?", (now - SEEN_KEEP_S,))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return new

    # ── подписки Web Push ──
    def push_subscribe(self, endpoint: str, p256dh: str, auth: str, name: str, now: float | None = None) -> int:
        """Новая подписка или обновление ключей и имени у того же endpoint (id и created остаются)."""
        now = time.time() if now is None else now
        with self._lock:
            self.db.execute(
                "INSERT INTO push_subs(endpoint, p256dh, auth, name, created) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
                "name = excluded.name, last_error = NULL", (endpoint, p256dh, auth, name, now))
            return self.db.execute("SELECT id FROM push_subs WHERE endpoint = ?", (endpoint,)).fetchone()[0]

    def push_unsubscribe(self, endpoint: str) -> bool:
        with self._lock:
            return self.db.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,)).rowcount > 0

    def push_delete(self, sub_id: int) -> bool:
        with self._lock:
            return self.db.execute("DELETE FROM push_subs WHERE id = ?", (sub_id,)).rowcount > 0

    def push_subs(self) -> list[dict]:
        """Все подписки (с endpoint и ключами — для отправки), по id; host — хост endpoint'а."""
        with self._lock:
            rows = self.db.execute("SELECT id, endpoint, p256dh, auth, name, created, last_ok, last_error "
                                   "FROM push_subs ORDER BY id").fetchall()
        return [{**dict(r), "host": urlsplit(r["endpoint"]).hostname or ""} for r in rows]

    def push_result(self, endpoint: str, ok_ts: float | None, error: str | None = None) -> None:
        """Итог отправки: успех (ok_ts) стирает last_error, ошибка пишется в last_error."""
        with self._lock:
            if ok_ts is not None:
                self.db.execute("UPDATE push_subs SET last_ok = ?, last_error = NULL WHERE endpoint = ?",
                                (ok_ts, endpoint))
            else:
                self.db.execute("UPDATE push_subs SET last_error = ? WHERE endpoint = ?", (error, endpoint))
