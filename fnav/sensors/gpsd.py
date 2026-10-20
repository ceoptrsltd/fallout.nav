"""Асинхронный клиент gpsd (JSON-протокол, без python3-gps) и разбор TPV/SKY.

    client = GpsdClient(on_message=state.handle)       # JSON: TPV, SKY, DEVICES, ...
    nmea = GpsdClient(on_line=ring.append, nmea=True)   # отдельное соединение с сырыми NMEA
    asyncio.create_task(client.run())
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fnav.models import GpsFix, Satellite

GNSSID = {0: "gps", 1: "sbas", 2: "galileo", 3: "beidou", 5: "qzss", 6: "glonass"}


def gnss_from_prn(prn: int) -> str:
    """Для старых gpsd без gnssid: нумерация PRN gpsd/NMEA."""
    if 1 <= prn <= 32:
        return "gps"
    if 33 <= prn <= 64 or 120 <= prn <= 158:
        return "sbas"
    if 65 <= prn <= 96:
        return "glonass"
    if 193 <= prn <= 202:
        return "qzss"
    if 201 <= prn <= 263 or 401 <= prn <= 437:
        return "beidou"
    if 301 <= prn <= 336:
        return "galileo"
    return "other"


def _num(v: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    if (lo is not None and f < lo) or (hi is not None and f > hi):
        return None
    return f


def parse_tpv(msg: dict, prev: GpsFix | None = None) -> GpsFix:
    """TPV → GpsFix. hdop/спутники берутся из prev (они приходят в SKY)."""
    prev = prev or GpsFix()
    mode = msg.get("mode", 0)
    fix = {2: "2d", 3: "3d"}.get(mode, "none")
    has = fix != "none"
    alt = None
    for k in ("altMSL", "alt", "altHAE"):
        alt = _num(msg.get(k))
        if alt is not None:
            break
    track = _num(msg.get("track"))
    return GpsFix(
        fix=fix,
        lat=_num(msg.get("lat"), -90, 90) if has else None,
        lon=_num(msg.get("lon"), -180, 180) if has else None,
        alt_m=alt if fix == "3d" else None,
        speed_mps=_num(msg.get("speed"), 0) if has else None,
        course_deg=(track % 360.0) if (has and track is not None) else None,
        hdop=prev.hdop,
        sats_used=prev.sats_used,
        sats_visible=prev.sats_visible,
        time_utc=msg.get("time") if isinstance(msg.get("time"), str) else prev.time_utc,
        age_s=0.0,
    )


def parse_sky(msg: dict) -> dict:
    """SKY → {"satellites": [Satellite] | None, "hdop", "used", "visible"} (None = поля нет в сообщении)."""
    out: dict[str, Any] = {"hdop": _num(msg.get("hdop"), 0), "satellites": None, "used": None, "visible": None}
    sats_raw = msg.get("satellites")
    if isinstance(sats_raw, list):
        sats: list[Satellite] = []
        for s in sats_raw:
            if not isinstance(s, dict):
                continue
            prn = s.get("PRN", s.get("svid"))
            if not isinstance(prn, int):
                continue
            gid = s.get("gnssid")
            gnss = GNSSID.get(gid, "other") if isinstance(gid, int) else gnss_from_prn(prn)
            ss = _num(s.get("ss"), 0, 99)
            sats.append(
                Satellite(
                    gnss=gnss,
                    prn=prn,
                    el=_num(s.get("el"), -90, 90),
                    az=_num(s.get("az"), 0, 360),
                    snr=ss if ss else None,  # 0 = не принимается
                    used=bool(s.get("used", False)),
                )
            )
        out["satellites"] = sats
        out["visible"] = len(sats)
        out["used"] = sum(1 for s in sats if s.used)
    if isinstance(msg.get("nSat"), int):
        out["visible"] = msg["nSat"]
    if isinstance(msg.get("uSat"), int):
        out["used"] = msg["uSat"]
    return out


class GpsState:
    """Накопленное состояние из потока сообщений gpsd."""

    STALE_S = 3.0
    NO_DATA_S = 5.0

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.fix = GpsFix()
        self.satellites: list[Satellite] = []
        self.last_tpv: float | None = None
        self.devices: list[dict] | None = None
        self.device_bps: int | None = None
        self.version: str | None = None
        self.tpv_count = 0
        self.since = clock()  # момент (пере)подключения к gpsd

    def on_connect(self) -> None:
        self.since = self.clock()
        self.devices = None

    def handle(self, msg: dict) -> None:
        cls = msg.get("class")
        if cls == "TPV":
            self.fix = parse_tpv(msg, self.fix)
            self.last_tpv = self.clock()
            self.tpv_count += 1
        elif cls == "SKY":
            sky = parse_sky(msg)
            if sky["satellites"] is not None:
                self.satellites = sky["satellites"]
            upd: dict[str, Any] = {}
            if sky["hdop"] is not None:
                upd["hdop"] = sky["hdop"]
            if sky["used"] is not None:
                upd["sats_used"] = sky["used"]
            if sky["visible"] is not None:
                upd["sats_visible"] = sky["visible"]
            if upd:
                self.fix = self.fix.model_copy(update=upd)
        elif cls == "DEVICES":
            devs = msg.get("devices")
            self.devices = devs if isinstance(devs, list) else []
            for d in self.devices:
                if isinstance(d, dict) and isinstance(d.get("bps"), int):
                    self.device_bps = d["bps"]
        elif cls == "DEVICE":
            if isinstance(msg.get("bps"), int):
                self.device_bps = msg["bps"]
            if msg.get("activated") == 0 or msg.get("activated") == "0":
                self.devices = []
            elif self.devices is not None and not self.devices:
                self.devices = [msg]
        elif cls == "VERSION":
            self.version = msg.get("release")

    def current(self) -> GpsFix:
        """Фикс с актуальным age_s; если TPV давно не было — fix='none'."""
        if self.last_tpv is None:
            return self.fix.model_copy(update={"age_s": None})
        age = self.clock() - self.last_tpv
        upd: dict[str, Any] = {"age_s": round(age, 2)}
        if age > self.STALE_S:
            upd["fix"] = "none"
        return self.fix.model_copy(update=upd)

    def problem(self) -> str | None:
        if self.devices is not None and len(self.devices) == 0:
            return "GPS: receiver not connected (gpsd sees no device; check /dev/ttyAMA0 and wiring)"
        now = self.clock()
        if self.last_tpv is None or self.last_tpv < self.since:
            if now - self.since > self.NO_DATA_S:
                return "GPS: receiver not connected (no data from gpsd; check wiring and /dev/ttyAMA0)"
            return None
        if now - self.last_tpv > self.STALE_S:
            return f"GPS: no data from receiver for {now - self.last_tpv:.0f} s"
        return None


class GpsdClient:
    """Подключение к gpsd с автоматическим переподключением.

    nmea=False → JSON-объекты в on_message(dict); nmea=True → сырые строки NMEA в on_line(str).
    status — человекочитаемая проблема соединения (None = всё хорошо).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 2947,
        *,
        nmea: bool = False,
        on_message: Callable[[dict], None] | None = None,
        on_line: Callable[[str], None] | None = None,
        on_connect: Callable[[], None] | None = None,
        reconnect_min_s: float = 1.0,
        reconnect_max_s: float = 10.0,
        silence_reconnect_s: float = 30.0,
    ):
        self.host, self.port = host, port
        self.nmea = nmea
        self.on_message = on_message or (lambda m: None)
        self.on_line = on_line or (lambda s: None)
        self.on_connect = on_connect or (lambda: None)
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.silence_reconnect_s = silence_reconnect_s
        self.status: str | None = "gpsd: connecting..."
        self.connected = False
        self.connects = 0
        self._writer: asyncio.StreamWriter | None = None

    @property
    def watch_cmd(self) -> bytes:
        w = {"enable": True, "nmea": True} if self.nmea else {"enable": True, "json": True}
        return f"?WATCH={json.dumps(w, separators=(',', ':'))};\n".encode()

    async def run(self) -> None:
        delay = self.reconnect_min_s
        while True:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5.0)
            except (TimeoutError, OSError) as e:
                self.connected = False
                self.status = f"gpsd: cannot connect to {self.host}:{self.port} ({e.__class__.__name__})"
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max_s, delay * 2)
                continue
            self._writer = writer
            self.connected = True
            self.connects += 1
            self.status = None
            delay = self.reconnect_min_s
            self.on_connect()
            try:
                writer.write(self.watch_cmd)
                await writer.drain()
                await self._read_loop(reader)
                self.status = "gpsd: connection closed"
            except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
                self.status = f"gpsd: connection lost ({e.__class__.__name__})"
            except TimeoutError:
                self.status = f"gpsd: no data for {self.silence_reconnect_s:.0f} s, reconnecting"
            finally:
                self.connected = False
                self._writer = None
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ConnectionError):
                    pass
            await asyncio.sleep(self.reconnect_min_s)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await asyncio.wait_for(reader.readline(), self.silence_reconnect_s)
            if not line:
                return
            if len(line) > 65536:
                continue
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            if text[0] in "$!":
                self.on_line(text)
            elif text[0] == "{" and not self.nmea:
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    try:
                        self.on_message(msg)
                    except Exception as e:  # noqa: BLE001 — разбор не должен убивать соединение
                        self.status = f"gpsd: failed to parse {msg.get('class')}: {e}"


class NmeaRing:
    def __init__(self, maxlen: int = 500):
        self._d: deque[str] = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        self._d.append(line)

    def tail(self, n: int = 50) -> list[str]:
        if n <= 0:
            return []
        items = list(self._d)
        return items[-n:]
