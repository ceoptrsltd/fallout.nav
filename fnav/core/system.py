"""Система: статус (температура, троттлинг, память, Wi‑Fi), журналы, перезагрузка, Wi‑Fi (nmcli).

Все внешние команды — `asyncio.create_subprocess_exec` с таймаутом, без shell.
На Windows и в mock-режиме — безопасные заглушки.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import sys
import time
from collections import deque

from fnav import paths
from fnav.core.util import spawn
from fnav.models import DiskStatus, SystemStatus

log = logging.getLogger("fnav.core.system")

IS_LINUX = sys.platform.startswith("linux")
SERVICES = ("fnav-core", "fnav-camera", "fnav-display", "mediamtx", "gpsd", "chrony")
_UNIT_RE = re.compile(r"^(fnav-[a-z0-9-]{1,32}|mediamtx|gpsd)$")


class SystemError_(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def run(*args: str, timeout: float = 5.0) -> tuple[int, str, str]:
    """→ (код, stdout, stderr). Нет программы → (127, "", текст). Таймаут → (124, …)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, NotImplementedError, OSError) as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except TimeoutError:
            pass
        return 124, "", f"timeout {timeout} s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def nmcli_split(line: str) -> list[str]:
    """Разбор строки `nmcli -t` (разделитель `:`, экранирование `\\:` и `\\\\`)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class RingLogHandler(logging.Handler):
    """Последние строки журнала fnav-core в памяти (для /api/system/logs без journalctl)."""

    def __init__(self, n: int = 1000):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=n)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


RING = RingLogHandler()


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # пакеты не отправляются
            ip = s.getsockname()[0]
            return None if ip.startswith("0.") else ip
    except OSError:
        return None


def _basic_status() -> dict:
    """Блокирующее чтение /sys и /proc — вызывать в потоке."""
    st: dict = {}
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    if t:
        try:
            st["cpu_temp_c"] = round(int(t.strip()) / 1000.0, 1)
        except ValueError:
            pass
    try:
        st["load_1m"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        pass
    mi = _read("/proc/meminfo")
    if mi:
        vals = {}
        for line in mi.splitlines():
            k, _, v = line.partition(":")
            try:
                vals[k] = int(v.split()[0])
            except (ValueError, IndexError):
                pass
        if vals.get("MemTotal") and "MemAvailable" in vals:
            st["mem_used_pct"] = round(100.0 * (1 - vals["MemAvailable"] / vals["MemTotal"]), 1)
    up = _read("/proc/uptime")
    if up:
        try:
            st["uptime_s"] = float(up.split()[0])
        except (ValueError, IndexError):
            pass
    st["ip"] = _local_ip()
    return st


class SystemService:
    def __init__(self, *, mock: bool):
        self.mock = mock
        self.status = SystemStatus()
        self.disk = DiskStatus()
        self._t0 = time.monotonic()

    # ── статус ──
    async def refresh(self, *, internet: bool | None = None) -> SystemStatus:
        st = await asyncio.to_thread(_basic_status)
        if IS_LINUX:
            rc, out, _ = await run("vcgencmd", "get_throttled", timeout=2)
            if rc == 0 and "=" in out:
                st["throttled"] = out.strip().split("=", 1)[1]
            rc, out, _ = await run("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", timeout=3)
            if rc == 0:
                for line in out.splitlines():
                    f = nmcli_split(line)
                    if len(f) >= 3 and f[0] == "yes":
                        st["wifi_ssid"] = f[1] or None
                        try:
                            st["wifi_signal_pct"] = int(f[2])
                        except ValueError:
                            pass
                        break
            rc, out, _ = await run("timedatectl", "show", "-p", "NTPSynchronized", "--value", timeout=2)
            if rc == 0:
                st["time_synced"] = out.strip() == "yes"
        if "uptime_s" not in st:
            st["uptime_s"] = round(time.monotonic() - self._t0, 1)
        if self.mock:  # заглушки, чтобы веб-интерфейсу было что показать
            st.setdefault("cpu_temp_c", 47.5)
            st.setdefault("throttled", "0x0")
            st.setdefault("load_1m", 0.42)
            st.setdefault("mem_used_pct", 31.0)
            st.setdefault("wifi_ssid", "PhoneHotspot")
            st.setdefault("wifi_signal_pct", 72)
            st.setdefault("time_synced", True)
        st["internet"] = internet
        st["utc_offset_min"] = round((time.localtime().tm_gmtoff or 0) / 60)
        self.status = SystemStatus(**st)
        return self.status

    async def refresh_disk(self, *, recordings_bytes: int | None, tiles_mb: float | None) -> DiskStatus:
        def _du() -> tuple[float, float] | None:
            d = paths.data_dir()
            try:
                d.mkdir(parents=True, exist_ok=True)
                u = shutil.disk_usage(d)
                return u.total / 1e9, u.free / 1e9
            except OSError:
                return None

        du = await asyncio.to_thread(_du)
        self.disk = DiskStatus(
            total_gb=round(du[0], 2) if du else None,
            free_gb=round(du[1], 2) if du else None,
            recordings_gb=round(recordings_bytes / 1e9, 3) if recordings_bytes is not None else None,
            tiles_mb=tiles_mb,
        )
        return self.disk

    async def services(self) -> dict[str, str]:
        if self.mock or not IS_LINUX:
            return {s: ("active" if s == "fnav-core" else ("mock" if self.mock else "unknown")) for s in SERVICES}
        _rc, out, _ = await run("systemctl", "is-active", *SERVICES, timeout=3)
        states = out.split()
        if len(states) != len(SERVICES):
            return {s: "unknown" for s in SERVICES}
        return dict(zip(SERVICES, states))

    # ── журналы ──
    async def logs(self, unit: str, lines: int) -> list[str]:
        if not _UNIT_RE.match(unit):
            raise SystemError_(400, "Unit not allowed")
        lines = max(1, min(int(lines), 5000))
        if not self.mock and IS_LINUX:
            rc, out, err = await run("journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso",
                                     timeout=8)
            if rc == 0:
                return out.splitlines()
            if unit != "fnav-core":
                raise SystemError_(503, f"journalctl unavailable: {err.strip()[:200]}")
        if unit == "fnav-core":
            return list(RING.lines)[-lines:]
        return [f"mock: {unit} journal is not available in development mode"]

    # ── питание ──
    async def power(self, action: str) -> dict:
        if self.mock or not IS_LINUX:
            return {"ok": True, "mock": True}
        cmd = {"reboot": "reboot", "shutdown": "poweroff"}[action]

        async def later() -> None:
            await asyncio.sleep(1.0)  # дать ответу уйти клиенту
            rc, _, err = await run("sudo", "-n", "systemctl", cmd, timeout=10)
            if rc != 0:
                log.error("systemctl %s: %s", cmd, err.strip())

        spawn(later(), name="power")
        return {"ok": True}

    # ── Wi‑Fi ──
    async def wifi_list(self) -> list[dict]:
        if self.mock or not IS_LINUX:
            return [
                {"ssid": "PhoneHotspot", "priority": 10, "active": True, "signal_pct": 72},
                {"ssid": "Home", "priority": 5, "active": False, "signal_pct": None},
            ]
        rc, out, err = await run("nmcli", "-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE",
                                 "connection", "show", timeout=5)
        if rc != 0:
            raise SystemError_(503, f"nmcli: {err.strip()[:200] or 'unavailable'}")
        conns = []
        for line in out.splitlines():
            f = nmcli_split(line)
            if len(f) >= 5 and f[2] in ("802-11-wireless", "wifi"):
                conns.append(f)
        signals: dict[str, int] = {}
        rc2, out2, _ = await run("nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "no",
                                 timeout=5)
        if rc2 == 0:
            for line in out2.splitlines():
                f = nmcli_split(line)
                if len(f) >= 2 and f[0]:
                    try:
                        signals[f[0]] = max(signals.get(f[0], 0), int(f[1]))
                    except ValueError:
                        pass
        res = []
        for name, uuid, _t, prio, active in conns:
            ssid = await self._conn_ssid(uuid) or name
            try:
                p = int(prio)
            except ValueError:
                p = 0
            res.append({"ssid": ssid, "priority": p, "active": active == "yes", "signal_pct": signals.get(ssid),
                        "name": name})
        res.sort(key=lambda r: (-r["active"], -r["priority"], r["ssid"]))
        return res

    async def _conn_ssid(self, uuid: str) -> str | None:
        rc, out, _ = await run("nmcli", "-t", "-g", "802-11-wireless.ssid", "connection", "show", "uuid", uuid,
                               timeout=4)
        return out.strip() if rc == 0 and out.strip() else None

    async def _find_uuids(self, ssid: str) -> list[tuple[str, bool]]:
        rc, out, _ = await run("nmcli", "-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show", timeout=5)
        found = []
        if rc == 0:
            for line in out.splitlines():
                f = nmcli_split(line)
                if len(f) < 4 or f[2] not in ("802-11-wireless", "wifi"):
                    continue
                if f[0] == ssid or await self._conn_ssid(f[1]) == ssid:
                    found.append((f[1], f[3] == "yes"))
        return found

    async def wifi_add(self, ssid: str, password: str, priority: int) -> dict:
        if not ssid or len(ssid.encode()) > 32:
            raise SystemError_(400, "SSID must be 1 to 32 bytes")
        if password and not (8 <= len(password) <= 63):
            raise SystemError_(400, "Wi-Fi password must be 8 to 63 characters (or empty for an open network)")
        if not -999 <= priority <= 999:
            raise SystemError_(400, "Priority must be between -999 and 999")
        if self.mock or not IS_LINUX:
            return {"ok": True, "mock": True}
        existing = await self._find_uuids(ssid)
        sec = (["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password] if password else [])
        common = ["connection.autoconnect", "yes", "connection.autoconnect-priority", str(priority)]
        if existing:
            args = ["nmcli", "connection", "modify", "uuid", existing[0][0], *common,
                    "802-11-wireless.ssid", ssid, *sec]
        else:
            args = ["nmcli", "connection", "add", "type", "wifi", "con-name", ssid, "ssid", ssid, *common, *sec]
        rc, _, err = await run(*args, timeout=15)
        if rc != 0:
            raise SystemError_(500, f"nmcli: {err.strip()[:300]}")
        return {"ok": True}

    async def wifi_delete(self, ssid: str) -> dict:
        if self.mock or not IS_LINUX:
            return {"ok": True, "mock": True}
        found = await self._find_uuids(ssid)
        if not found:
            raise SystemError_(404, "Network not found")
        if any(active for _, active in found):
            raise SystemError_(409, "Cannot delete the network the device is currently connected to")
        for uuid, _ in found:
            rc, _, err = await run("nmcli", "connection", "delete", "uuid", uuid, timeout=10)
            if rc != 0:
                raise SystemError_(500, f"nmcli: {err.strip()[:300]}")
        return {"ok": True}

