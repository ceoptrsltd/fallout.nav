"""Настройка u-blox NEO-M8N по UBX (скорость порта, частота, созвездия, динамическая модель, сохранение).

Работает напрямую с последовательным портом (pyserial), поэтому на время настройки **gpsd останавливается**
(`sudo -n systemctl stop gpsd.socket gpsd.service`) и после — запускается снова (в `finally`).
Почему не через gpsd: gpsd не отдаёт клиенту ACK/NAK и сам может переключать скорость/протокол — подтверждения
не проверить. Остановка на 5–15 с надёжнее. После смены скорости gpsd при запуске сам найдёт 115200
(автопоиск скорости), а настройки сохранены во flash модуля (CFG-CFG), так что переживают выключение.

Блокирующий код — вызывать из потока (`asyncio.to_thread`).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable
from typing import Any, Protocol

from fnav.sensors import ubx

MAJOR_GNSS = ("gps", "glonass", "galileo", "beidou")
BAUD_CANDIDATES = (38400, 9600, 115200, 57600, 230400, 19200, 4800)
RATE_STEPS = (18, 15, 10, 8, 5, 4, 2, 1)
DETECT_BUDGET_S = 15.0  # не держать gpsd остановленным дольше, если модуль не отвечает


class SerialLike(Protocol):
    baudrate: int

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int = 1) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


def open_serial(device: str, baud: int) -> SerialLike:
    import serial  # pyserial (apt: python3-serial), ленивый импорт

    return serial.Serial(device, baud, timeout=0.05, write_timeout=1.0)


def max_rate_for(constellations: list[str]) -> int:
    """NEO-M8N: до 18 Гц с одной системой, до 10 Гц с несколькими одновременно (datasheet)."""
    n = sum(1 for c in constellations if c in MAJOR_GNSS)
    return 18 if n <= 1 else 10


def rate_candidates(requested: int, constellations: list[str]) -> list[int]:
    top = min(requested, max_rate_for(constellations))
    return [top] + [r for r in RATE_STEPS if r < top]


# ───────────────────────── управление gpsd ─────────────────────────


def gpsd_control(action: str, timeout: float = 15.0) -> tuple[bool, str]:
    """stop|start gpsd (socket + service). Нужны права: sudoers для пользователя fnav (см. отчёт агента F)."""
    units = ["gpsd.socket", "gpsd.service"]
    cmd = ["systemctl", action, *units]
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{' '.join(cmd)}: {e}"
    if p.returncode != 0:
        return False, f"{' '.join(cmd)}: exit code {p.returncode}: {(p.stderr or p.stdout).strip()}"
    return True, "ok"


# ───────────────────────── конфигуратор ─────────────────────────


class M8NConfigurator:
    def __init__(
        self,
        port: SerialLike,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.port = port
        self.log = log or (lambda s: None)
        self.clock = clock
        self.sleep = sleep
        self.parser = ubx.StreamParser()
        self.nmea_seen: list[str] = []
        self.bytes_in = 0  # всего принято байт (любых): тишина на линии = модуль не подключён

    # ── ввод/вывод ──
    def _read_items(self) -> list[ubx.UbxMessage | str]:
        data = self.port.read(512)
        if not data:
            return []
        self.bytes_in += len(data)
        items = self.parser.feed(data)
        for it in items:
            if isinstance(it, str):
                self.nmea_seen.append(it)
        del self.nmea_seen[:-200]
        return items

    def _drain(self) -> None:
        self.port.reset_input_buffer()
        self.parser = ubx.StreamParser()

    def send(self, data: bytes) -> None:
        self.port.write(data)
        with contextlib.suppress(Exception):
            self.port.flush()

    def wait_for(self, pred: Callable[[ubx.UbxMessage | str], Any], timeout: float) -> Any:
        """Читать, пока pred(item) не вернёт не-None; вернуть его результат (или None по таймауту)."""
        end = self.clock() + timeout
        while self.clock() < end:
            for it in self._read_items():
                r = pred(it)
                if r is not None:
                    return r
        return None

    def send_and_ack(self, data: bytes, timeout: float = 1.0, retries: int = 2) -> bool | None:
        """True = ACK, False = NAK, None = нет ответа."""
        cls, msg_id = data[2], data[3]
        for _ in range(retries + 1):
            self.send(data)
            r = self.wait_for(
                lambda it: it.is_ack_for(cls, msg_id) if isinstance(it, ubx.UbxMessage) else None, timeout
            )
            if r is not None:
                return r
        return None

    def poll(self, cls: int, msg_id: int, payload: bytes = b"", timeout: float = 1.0, retries: int = 2):
        for _ in range(retries + 1):
            self.send(ubx.frame(cls, msg_id, payload))

            def match(it):
                if isinstance(it, ubx.UbxMessage):
                    if it.key == (cls, msg_id) and (it.payload or not payload):
                        return it
                    if it.is_ack_for(cls, msg_id) is False:
                        return False
                return None

            r = self.wait_for(match, timeout)
            if r is False:
                return None
            if r is not None:
                return r
        return None

    # ── скорость порта ──
    def probe(self, timeout: float = 1.2) -> str | None:
        """Есть ли осмысленные данные на текущей скорости: 'ubx' (ответ на MON-VER) | 'nmea' | None."""
        self._drain()
        self.send(ubx.mon_ver_poll())
        found_nmea = False

        def pred(it):
            nonlocal found_nmea
            if isinstance(it, ubx.UbxMessage):
                return "ubx"
            found_nmea = True
            return None

        r = self.wait_for(pred, timeout)
        return r or ("nmea" if found_nmea else None)

    def detect_baud(self, first: int | None = None, budget_s: float = DETECT_BUDGET_S) -> int | None:
        """Перебор скоростей. Быстрый выход: если за первые две попытки на RX не пришло ни одного байта —
        модуль не подключён (подключённый M8N шлёт NMEA постоянно, на чужой скорости — хотя бы «мусор»).
        Общий лимит времени — budget_s."""
        cands = ([first] if first else []) + [b for b in BAUD_CANDIDATES if b != first]
        start = self.clock()
        for i, baud in enumerate(cands):
            if self.clock() - start > budget_s:
                self.log(f"baud detection: time budget {budget_s:.0f} s exceeded")
                return None
            self.port.baudrate = baud
            self.sleep(0.05)
            kind = self.probe()
            self.log(f"baud {baud}: {kind or 'no data'}")
            if kind:
                return baud
            if i == 1 and self.bytes_in == 0:
                self.log("RX line silent - receiver not connected")
                return None
        return None

    def set_baud(self, current: int, target: int) -> bool:
        """CFG-PRT на новую скорость. ACK приходит (или теряется) на старой скорости — проверяем на новой."""
        self.send(ubx.cfg_prt_uart(target))
        self.sleep(0.3)
        self.port.baudrate = target
        self.sleep(0.1)
        for _ in range(3):
            if self.probe() == "ubx":
                return True
        self.port.baudrate = current  # не вышло — вернуться
        return False

    # ── частота ──
    def measure_rate(self, seconds: float = 3.0) -> float:
        """Сколько навигационных эпох в секунду реально выдаёт модуль (по RMC/GGA, NAV-PVT)."""
        self._drain()
        self.wait_for(lambda it: True if isinstance(it, str) and it[3:6] == "RMC" else None, 1.5)
        counts = {"RMC": 0, "GGA": 0, "PVT": 0}
        start = self.clock()
        end = start + seconds
        while self.clock() < end:
            for it in self._read_items():
                if isinstance(it, str):
                    t = it[3:6]
                    if t in counts:
                        counts[t] += 1
                elif it.key == (ubx.CLS_NAV, ubx.ID_NAV_PVT):
                    counts["PVT"] += 1
        elapsed = max(1e-6, self.clock() - start)
        return max(counts.values()) / elapsed

    # ── основная процедура ──
    def configure(
        self,
        baud: int = 115200,
        rate_hz: int = 10,
        constellations: list[str] | None = None,
        dyn_model: str = "automotive",
        save: bool = True,
        enable_pvt: bool | None = None,
        measure_s: float = 3.0,
        static_hold_cm_s: int = 50,
        static_hold_max_dist_m: int = 20,
    ) -> dict:
        constellations = list(constellations or ["gps", "glonass", "galileo"])
        res: dict[str, Any] = {"ok": False, "steps": [], "warnings": []}
        steps, warns = res["steps"], res["warnings"]

        def step(name: str, ok: bool | None, info: str = "") -> bool:
            steps.append({"step": name, "ok": bool(ok), "info": info})
            self.log(f"{name}: {'OK' if ok else 'FAILED'} {info}".rstrip())
            return bool(ok)

        found = self.detect_baud(first=baud)
        if found is None:
            step("baud detection", False, "no response at any baud rate")
            res["error"] = "GPS receiver not connected or not responding (check RX/TX wiring and power)"
            return res
        res["baud_found"] = found
        step("baud detection", True, str(found))

        ver = self.poll(ubx.CLS_MON, ubx.ID_MON_VER)
        if ver is not None:
            res["version"] = ubx.parse_mon_ver(ver.payload)

        if found != baud and not step("port baud rate", self.set_baud(found, baud), f"{found} -> {baud}"):
            res["error"] = f"failed to switch baud rate to {baud}"
            return res
        res["baud"] = baud

        # созвездия
        self._configure_gnss(constellations, res, step)
        constellations = res.get("constellations", constellations)

        # динамическая модель
        dyn = ubx.DYN_MODELS.get(dyn_model, 4)
        step("dynamic model", self.send_and_ack(ubx.cfg_nav5(dyn)), dyn_model)
        step("static hold", self.send_and_ack(ubx.cfg_nav5_static_hold(static_hold_cm_s, static_hold_max_dist_m)),
             f"{static_hold_cm_s} cm/s, {static_hold_max_dist_m} m")

        # NMEA: каждую эпоху — GGA/RMC/VTG; раз в секунду — GSA/GSV (иначе 115200 не хватит при 10 Гц)
        per_sec = max(1, rate_hz)
        msg_rates = {"GGA": 1, "RMC": 1, "VTG": 1, "GSA": per_sec, "GSV": per_sec, "GLL": 0, "ZDA": 0}
        bad = [n for n, r in msg_rates.items()
               if not self.send_and_ack(ubx.cfg_msg(ubx.CLS_NMEA_STD, ubx.NMEA_IDS[n], r))]
        step("NMEA messages", not bad, ("no ACK: " + ",".join(bad)) if bad else "")
        if enable_pvt is not None:
            step("NAV-PVT", self.send_and_ack(ubx.cfg_msg(ubx.CLS_NAV, ubx.ID_NAV_PVT, 1 if enable_pvt else 0)),
                 "on" if enable_pvt else "off")

        # частота: ставим и проверяем фактическую; не держит — снижаем
        cands = rate_candidates(rate_hz, constellations)
        if cands[0] < rate_hz:
            warns.append(f"{rate_hz} Hz is not supported with {'+'.join(constellations)} (max {cands[0]} Hz)")
        achieved = None
        for r in cands:
            ack = self.send_and_ack(ubx.cfg_rate(r))
            if not ack:
                step(f"rate {r} Hz", False, "NAK" if ack is False else "no response")
                continue
            measured = self.measure_rate(measure_s) if measure_s > 0 else float(r)
            ok = measured >= 0.85 * r
            step(f"rate {r} Hz", ok, f"measured {measured:.1f} Hz")
            res.setdefault("rate_measured", {})[r] = round(measured, 2)
            if ok:
                achieved = r
                break
        if achieved is None:
            res["error"] = "failed to set the navigation rate"
            return res
        if achieved != rate_hz:
            warns.append(f"rate reduced: {rate_hz} -> {achieved} Hz")
        res["rate_hz"] = achieved

        if save and not step("save to flash (CFG-CFG)", self.send_and_ack(ubx.cfg_cfg_save(), timeout=2.0)):
            warns.append("settings were not saved to flash - factory defaults return after power-off")
        res["ok"] = True
        return res

    def _configure_gnss(self, constellations: list[str], res: dict, step) -> None:
        warns = res["warnings"]
        msg = self.poll(ubx.CLS_CFG, ubx.ID_CFG_GNSS)
        try:
            current = ubx.parse_cfg_gnss(msg.payload) if msg else ubx.GnssConfig.default()
        except ValueError:
            current = ubx.GnssConfig.default()
        if msg is None:
            warns.append("CFG-GNSS: poll not answered, using the factory channel table")
        before = [c for c in current.enabled_names() if c in MAJOR_GNSS]

        attempts = [list(constellations)]
        # запасные варианты: не больше 3 систем; без Galileo (старые прошивки < 3.01); только GPS+GLONASS
        if len([c for c in constellations if c in MAJOR_GNSS]) > 3:
            attempts.append([c for c in constellations if c != "beidou"][:3])
        if "galileo" in constellations:
            attempts.append([c for c in constellations if c != "galileo"])
        attempts.append(["gps", "glonass"])
        seen: list[list[str]] = []
        for want in attempts:
            if not want or want in seen:
                continue
            seen.append(want)
            fallback = f"constellations: {'+'.join(constellations)} rejected, using {'+'.join(want)}"
            if sorted(want) == sorted(before):
                step("constellations", True, f"{'+'.join(want)} (unchanged)")
                if want != constellations:
                    warns.append(fallback)
                res["constellations"] = want
                return
            cfg = ubx.gnss_config_for(current, want)
            ack = self.send_and_ack(ubx.build_cfg_gnss(cfg), timeout=1.5)
            if ack:
                step("constellations", True, "+".join(want))
                if want != constellations:
                    warns.append(fallback)
                res["constellations"] = want
                # смена GNSS применяется после перезапуска GNSS-подсистемы (конфигурация в RAM остаётся)
                self.send(ubx.cfg_rst(0x0000, 0x02))
                self.sleep(1.5)
                self._drain()
                self.probe()
                return
            step("constellations", False, f"{'+'.join(want)}: {'NAK' if ack is False else 'no response'}")
        res["constellations"] = before or constellations
        warns.append("constellations unchanged")


def configure_receiver(
    device: str = "/dev/gps0",
    baud: int = 115200,
    rate_hz: int = 10,
    constellations: list[str] | None = None,
    dyn_model: str = "automotive",
    *,
    save: bool = True,
    enable_pvt: bool | None = None,
    manage_gpsd: bool = True,
    static_hold_cm_s: int = 50,
    static_hold_max_dist_m: int = 20,
    log: Callable[[str], None] | None = None,
    open_port: Callable[[str, int], SerialLike] = open_serial,
) -> dict:
    """Полная процедура: остановить gpsd → настроить → запустить gpsd. Возвращает dict с ok/steps/warnings."""
    log = log or (lambda s: None)
    result: dict[str, Any] = {"ok": False}
    stopped = False
    try:
        if manage_gpsd:
            ok, info = gpsd_control("stop")
            log(f"stopping gpsd: {info}")
            if not ok:
                # gpsd держит порт — параллельное чтение даст случайные сбои; лучше честно отказаться
                result["error"] = f"failed to stop gpsd (sudo rights needed): {info}"
                return result
            stopped = True
            time.sleep(0.5)
        try:
            port = open_port(device, baud)
        except Exception as e:  # noqa: BLE001
            result["error"] = f"cannot open {device}: {e}"
            return result
        try:
            cfgr = M8NConfigurator(port, log=log)
            r = cfgr.configure(baud, rate_hz, constellations, dyn_model, save=save, enable_pvt=enable_pvt,
                               static_hold_cm_s=static_hold_cm_s, static_hold_max_dist_m=static_hold_max_dist_m)
            r["warnings"] = result.get("warnings", []) + r.get("warnings", [])
            result = r
        finally:
            with contextlib.suppress(Exception):
                port.close()
    finally:
        if manage_gpsd and stopped:
            ok, info = gpsd_control("start")
            log(f"starting gpsd: {info}")
            if not ok:
                result.setdefault("warnings", []).append(f"failed to start gpsd: {info}")
    return result
