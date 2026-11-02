"""Задняя камера: поиск по MAC и состояние между запусками (camera.md «Задняя камера», «Поиск по MAC»).

Камера живёт у точки доступа телефона: адрес по DHCP меняется, менялась и сама подсеть (172.23.204.x, 10.150.83.x).
Пока камера работает, сервис запоминает её MAC (таблица соседей ядра для её адреса) в ``camera_rear.json`` в каталоге
данных. Несколько неудач подряд вида «недоступна» (нет маршрута, таймаут, отказ в соединении — не логин и не кодек) —
поиск: «обход ARP» своей /24 на интерфейсе, через который идёт путь к камере (UDP-пакет на каждый адрес: ядро само
спрашивает ARP, прав root не нужно), затем ``ip neigh`` — где теперь этот MAC. Не чаще раза в минуту. ONVIF
WS-Discovery эта камера не отвечает — на него не полагаемся.

Найденный адрес сервис камеры использует сразу (url и lores_url), а в настройки его записывает fnav-core (сервис
камеры settings.json не пишет): ``CameraStatus.discovered_host``.

Там же — измеренный средний битрейт записи задней: по нему оценки места у обеих камер (у передней — её доля задней).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("fnav.camera.rear")

STATE_FILE = "camera_rear.json"  # в каталоге данных: MAC и адрес камеры, измеренный битрейт задней
UNREACHABLE_FAILS = 3  # столько неудач «недоступна» подряд — искать камеру по MAC
SWEEP_MIN_S = 60.0  # обход подсети — не чаще
ARP_WAIT_S = 1.5  # ждём ответов ARP после обхода
LEARN_EVERY_S = 600.0  # MAC работающей камеры перепроверяем не чаще
BITRATE_FRESH_S = 3600.0  # измеренный битрейт задней старше — не верим (камера давно не писала)
# «камера недоступна» (а не логин, кодек, обрыв уже идущего потока): тексты ошибок FFmpeg/PyAV и errno
_UNREACHABLE = ("no route to host", "network is unreachable", "host is unreachable", "timed out",
                "connection refused", "immediate exit requested")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_GOOD_STATES = {"PERMANENT": 0, "REACHABLE": 0, "DELAY": 1, "PROBE": 1, "STALE": 2, "NOARP": 2}


def is_unreachable(msg: str | None) -> bool:
    """Ошибка подключения вида «камеры нет по этому адресу» (нет маршрута, таймаут, отказ)."""
    m = (msg or "").lower()
    return any(x in m for x in _UNREACHABLE)


def is_ipv4(host: str | None) -> bool:
    try:
        ipaddress.IPv4Address(host or "")
        return True
    except ValueError:
        return False


def url_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def replace_host(url: str, host: str) -> str:
    """rtsp://[user@]old[:port]/path → rtsp://[user@]host[:port]/path."""
    try:
        parts = urlsplit(url)
        if not parts.hostname:
            return url
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # кривой порт и т.п. — не трогаем
        return url
    auth = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=(auth + "@" if auth else "") + host + port))


# ── таблица соседей ──


def parse_ip_neigh(text: str) -> dict[str, tuple[str, str]]:
    """Вывод ``ip -4 neigh show`` → {адрес: (mac, состояние)}; без MAC (FAILED, INCOMPLETE) — пропуск."""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or "lladdr" not in f or not is_ipv4(f[0]):
            continue
        i = f.index("lladdr")
        mac = f[i + 1].lower() if i + 1 < len(f) else ""
        if _MAC_RE.match(mac):
            out[f[0]] = (mac, f[-1].upper())
    return out


def parse_proc_arp(text: str) -> dict[str, tuple[str, str]]:
    """/proc/net/arp → {адрес: (mac, "REACHABLE"|"STALE")} (флаг 0x2 — запись полная)."""
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 4 or not is_ipv4(f[0]):
            continue
        mac = f[3].lower()
        try:
            complete = int(f[2], 16) & 0x2
        except ValueError:
            continue
        if complete and _MAC_RE.match(mac) and mac != "00:00:00:00:00:00":
            out[f[0]] = (mac, "REACHABLE")
    return out


def neighbours() -> dict[str, tuple[str, str]]:
    """Таблица соседей IPv4 ядра: ``ip -4 neigh show``, нет ``ip`` — /proc/net/arp; на ПК (Windows) — пусто."""
    try:
        r = subprocess.run(["ip", "-4", "neigh", "show"], capture_output=True, text=True, timeout=3, check=False)
        if r.returncode == 0:
            return parse_ip_neigh(r.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return parse_proc_arp(Path("/proc/net/arp").read_text())
    except OSError:
        return {}


# ── свой адрес и обход подсети ──


def local_ipv4_for(host: str) -> str | None:
    """Наш адрес на интерфейсе, через который ядро повело бы пакет к host (UDP connect ничего не шлёт). Подсети
    камеры больше нет — маршрут по умолчанию, то есть точка доступа телефона, где камера теперь и живёт."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if is_ipv4(ip) and not ip.startswith(("127.", "0.")) else None


def subnet24(ip: str) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(f"{ip}/24", strict=False)


def sweep(local_ip: str, port: int = 9) -> int:
    """«Обход ARP» /24 вокруг local_ip: по UDP-пакету на каждый адрес (порт discard) — ядро спрашивает ARP, ответившие
    попадают в таблицу соседей. Прав root не нужно. → сколько адресов опрошено."""
    n = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setblocking(False)
        for addr in subnet24(local_ip).hosts():
            a = str(addr)
            if a == local_ip:
                continue
            try:
                s.sendto(b"\0", (a, port))
                n += 1
            except OSError:
                pass  # ARP по адресу уже не ответил (EHOSTUNREACH) и т.п. — неважно
    return n


# ── состояние между запусками ──


class RearState:
    """``camera_rear.json``: {"mac", "host", "bitrate_kbps", "bitrate_at"} — пишет сервис задней камеры (атомарно),
    передняя читает битрейт. Ошибки чтения/записи — только в лог: состояние необязательно."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict = {}
        self._loaded = False
        self._bitrate_saved = 0.0  # monotonic последней записи битрейта

    def get(self) -> dict:
        with self._lock:
            if not self._loaded:
                self._loaded = True
                self.data = read_state(self.path)
            return dict(self.data)

    def update(self, **fields) -> None:
        self.get()
        with self._lock:
            new = {**self.data, **fields}
            if new == self.data:
                return
            self.data = new
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(new), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as e:
                log.warning("rear camera: cannot save %s: %s", self.path, e)

    def save_bitrate(self, kbps: float, *, every_s: float = 600.0, mono: Callable[[], float] = time.monotonic) -> None:
        """Измеренный битрейт — в файл, если заметно изменился (> 10 %) или раз в every_s (свежесть для передней)."""
        old = self.get().get("bitrate_kbps")
        now = mono()
        changed = not isinstance(old, int | float) or abs(kbps - old) > 0.1 * max(old, 1.0)
        if changed or now - self._bitrate_saved >= every_s:
            self._bitrate_saved = now
            self.update(bitrate_kbps=round(kbps, 1), bitrate_at=round(time.time()))


def read_state(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def read_bitrate(path: Path, max_age_s: float = BITRATE_FRESH_S, now: float | None = None) -> float | None:
    """Измеренный битрейт задней (кбит/с) из её состояния, если он свежий; иначе None (оценка — из настроек)."""
    d = read_state(path)
    kbps, at = d.get("bitrate_kbps"), d.get("bitrate_at")
    if not isinstance(kbps, int | float) or not isinstance(at, int | float) or kbps <= 0:
        return None
    return float(kbps) if (time.time() if now is None else now) - at <= max_age_s else None


# ── поиск ──


class CameraFinder:
    """MAC работающей камеры — запомнить (``learn``); камера пропала — найти её новый адрес по MAC (``find``)."""

    def __init__(self, state: RearState, *,
                 neighbours_fn: Callable[[], dict[str, tuple[str, str]]] = neighbours,
                 sweep_fn: Callable[[str], int] = sweep,
                 local_ip_fn: Callable[[str], str | None] = local_ipv4_for,
                 mono: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 wait_s: float = ARP_WAIT_S, sweep_min_s: float = SWEEP_MIN_S) -> None:
        self.state = state
        self.neighbours_fn = neighbours_fn
        self.sweep_fn = sweep_fn
        self.local_ip_fn = local_ip_fn
        self.mono = mono
        self.sleep = sleep
        self.wait_s = wait_s
        self.sweep_min_s = sweep_min_s
        self._last_sweep: float | None = None
        self._no_mac_logged = False

    @property
    def mac(self) -> str | None:
        m = self.state.get().get("mac")
        return m if isinstance(m, str) and _MAC_RE.match(m) else None

    def learn(self, host: str) -> str | None:
        """Камера работает по host: её MAC из таблицы соседей → в состояние (если изменился). → MAC или None."""
        if not is_ipv4(host):
            return None  # имя (cam.local) — адрес и так найдёт резолвер
        entry = self.neighbours_fn().get(host)
        if entry is None:
            return None
        mac = entry[0]
        d = self.state.get()
        if d.get("mac") != mac or d.get("host") != host:
            log.info("rear camera: MAC %s at %s remembered (to find the camera if its address changes)", mac, host)
            self.state.update(mac=mac, host=host)
        return mac

    def may_sweep(self) -> bool:
        return self._last_sweep is None or self.mono() - self._last_sweep >= self.sweep_min_s

    def find(self, host: str) -> str | None:
        """Камера не отвечает по host: обойти свою /24 и найти её MAC на другом адресе. Не чаще sweep_min_s.
        → новый адрес или None (MAC не известен, не найдена, рано)."""
        mac = self.mac
        if mac is None:
            if not self._no_mac_logged:
                self._no_mac_logged = True
                log.info("rear camera: its MAC is not known yet — cannot look for it on other addresses")
            return None
        if not self.may_sweep():
            return None
        self._last_sweep = self.mono()
        local = self.local_ip_fn(host) if is_ipv4(host) else None
        if local is None:
            log.info("rear camera: no local network to look for MAC %s in", mac)
            return None
        net = subnet24(local)
        try:
            n = self.sweep_fn(local)
        except OSError as e:
            log.warning("rear camera: ARP sweep of %s failed: %s", net, e)
            n = 0
        if n:
            self.sleep(self.wait_s)
        found = [(_GOOD_STATES.get(st, 3), ip) for ip, (m, st) in self.neighbours_fn().items()
                 if m == mac and ip != host and ipaddress.IPv4Address(ip) in net]
        if not found:
            log.info("rear camera: MAC %s not found in %s (%d addresses asked)", mac, net, n)
            return None
        new = min(found)[1]
        log.warning("rear camera: not reachable at %s — found it by MAC %s at %s", host, mac, new)
        self.state.update(host=new)
        return new
