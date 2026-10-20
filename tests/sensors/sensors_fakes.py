"""Фейки для тестов датчиков: эмулятор NEO-M8N на «последовательном порту», шина I2C с IST8310, gpsd."""

from __future__ import annotations

import asyncio
import json
import struct

from fnav.sensors import ubx


def nmea(body: str) -> str:
    ck = 0
    for ch in body:
        ck ^= ord(ch)
    return f"${body}*{ck:02X}"


class FakeM8N:
    """Эмулятор NEO-M8N: отвечает на UBX только на своей скорости, выдаёт RMC/GGA с частотой
    min(заданная, максимум модуля). Время виртуальное: clock()/sleep() передаются в конфигуратор."""

    def __init__(self, baud: int = 38400, max_rate_multi: float = 10.0, max_rate_single: float = 18.0,
                 galileo_ok: bool = True, alive: bool = True, rate_cap: float | None = None):
        self.t = 0.0
        self.device_baud = baud
        self.baudrate = 9600
        self.alive = alive
        self.galileo_ok = galileo_ok
        self.max_multi, self.max_single = max_rate_multi, max_rate_single
        self.rate_cap = rate_cap  # «железный» предел (например, из-за загрузки CPU модуля)
        self.out = bytearray()
        self.parser = ubx.StreamParser()
        self.rate_hz = 1.0
        cfg = ubx.GnssConfig.default()
        cfg.blocks = [b.with_enabled(ubx.GNSS_NAMES[b.gnss_id] in ("gps", "sbas", "qzss", "glonass"))
                      for b in cfg.blocks]
        self.gnss = cfg
        self.next_epoch = 0.0
        self.saved = False
        self.nav5_dyn: int | None = None
        self.msg_rates: dict[tuple[int, int], int] = {}
        self.resets = 0
        self.log: list[tuple[int, int]] = []

    # ── «порт» ──
    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self._gen()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._gen()
        self.out.clear()

    def write(self, data: bytes) -> int:
        if not self.alive or self.baudrate != self.device_baud:
            return len(data)
        for it in self.parser.feed(data):
            if isinstance(it, ubx.UbxMessage):
                self.log.append(it.key)
                self._handle(it)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        self.t += 0.05
        self._gen()
        if not self.alive:
            return b""
        if self.baudrate != self.device_baud:
            self.out.clear()
            return b"\xf0\x0f\x80" if self.t % 0.2 < 0.1 else b""
        d = bytes(self.out[:size])
        del self.out[:size]
        return d

    # ── модуль ──
    def majors(self) -> list[str]:
        return [ubx.GNSS_NAMES[b.gnss_id] for b in self.gnss.blocks
                if b.enabled and ubx.GNSS_NAMES.get(b.gnss_id) in ("gps", "glonass", "galileo", "beidou")]

    def actual_rate(self) -> float:
        cap = self.max_single if len(self.majors()) <= 1 else self.max_multi
        r = min(self.rate_hz, cap)
        if self.rate_cap:
            r = min(r, self.rate_cap)
        return r

    def _gen(self) -> None:
        if not self.alive:
            return
        while self.next_epoch <= self.t:
            self.out += (nmea("GNRMC,120000.00,V,,,,,,,201026,,,N") + "\r\n").encode()
            self.next_epoch += 1.0 / self.actual_rate()

    def _ack(self, m: ubx.UbxMessage, ok: bool = True) -> None:
        self.out += ubx.frame(ubx.CLS_ACK, ubx.ID_ACK_ACK if ok else ubx.ID_ACK_NAK, bytes(m.key))

    def _handle(self, m: ubx.UbxMessage) -> None:
        if m.key == (ubx.CLS_MON, ubx.ID_MON_VER):
            p = b"ROM CORE 3.01 (107888)".ljust(30, b"\0") + b"00080000".ljust(10, b"\0")
            p += b"FWVER=SPG 3.01".ljust(30, b"\0") + b"PROTVER=18.00".ljust(30, b"\0")
            self.out += ubx.frame(ubx.CLS_MON, ubx.ID_MON_VER, p)
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_PRT) and len(m.payload) == 20:
            self._ack(m)
            self.device_baud = struct.unpack_from("<I", m.payload, 8)[0]
            self.out.clear()  # ACK ушёл на старой скорости — потерян
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_GNSS):
            if not m.payload:
                self.out += ubx.build_cfg_gnss(self.gnss)
                return
            new = ubx.parse_cfg_gnss(m.payload)
            majors = [ubx.GNSS_NAMES[b.gnss_id] for b in new.blocks
                      if b.enabled and ubx.GNSS_NAMES.get(b.gnss_id) in ("gps", "glonass", "galileo", "beidou")]
            if len(majors) > 3 or ("galileo" in majors and not self.galileo_ok):
                self._ack(m, False)
                return
            self.gnss = new
            self._ack(m)
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_RATE):
            meas = struct.unpack_from("<H", m.payload, 0)[0]
            self.rate_hz = 1000.0 / meas
            self.next_epoch = self.t
            self._ack(m)
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_NAV5):
            self.nav5_dyn = m.payload[2]
            self._ack(m)
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_MSG):
            self.msg_rates[(m.payload[0], m.payload[1])] = m.payload[2]
            self._ack(m)
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_CFG):
            self.saved = True
            self._ack(m)
        elif m.key == (ubx.CLS_CFG, ubx.ID_CFG_RST):
            self.resets += 1


class FakeIST8310Bus:
    """Шина I2C с IST8310 на 0x0E: отдаёт заданное поле (мкТл, оси чипа)."""

    def __init__(self, field=(30.0, -6.0, 45.0), present: bool = True):
        self.field = field
        self.present = present
        self.regs: dict[int, int] = {}
        self.writes: list[tuple[int, int, int]] = []
        self.closed = False

    def _check(self, addr: int) -> None:
        if not self.present or addr != 0x0E:
            raise OSError(121, "Remote I/O error")

    def read_byte_data(self, addr: int, reg: int) -> int:
        self._check(addr)
        if reg == 0x00:
            return 0x10
        if reg == 0x02:
            return 0x01
        return self.regs.get(reg, 0)

    def write_byte_data(self, addr: int, reg: int, val: int) -> None:
        self._check(addr)
        self.regs[reg] = val
        self.writes.append((addr, reg, val))

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]:
        self._check(addr)
        out = b"".join(struct.pack("<h", round(v / 0.3)) for v in self.field)
        return list(out[:length])

    def write_quick(self, addr: int) -> None:
        self._check(addr)

    def read_byte(self, addr: int) -> int:
        self._check(addr)
        return 0

    def close(self) -> None:
        self.closed = True


class FakeGpsd:
    """Мини-gpsd: на ?WATCH отвечает VERSION/DEVICES и затем шлёт заданные строки."""

    def __init__(self, lines: list[str] | None = None, nmea_lines: list[str] | None = None,
                 close_after: bool = False, devices: list[dict] | None = None):
        self.lines = lines or []
        self.nmea_lines = nmea_lines or []
        self.close_after = close_after
        self.devices = [{"class": "DEVICE", "path": "/dev/ttyAMA0", "driver": "u-blox", "bps": 115200}] \
            if devices is None else devices
        self.watches: list[str] = []
        self.connections = 0
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self, port: int = 0) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            writer.write(b'{"class":"VERSION","release":"3.25","rev":"3.25","proto_major":3,"proto_minor":15}\n')
            cmd = (await reader.readline()).decode().strip()
            self.watches.append(cmd)
            writer.write((json.dumps({"class": "DEVICES", "devices": self.devices}) + "\n").encode())
            is_nmea = '"nmea":true' in cmd
            for line in self.nmea_lines if is_nmea else self.lines:
                writer.write((line + "\n").encode())
            await writer.drain()
            if self.close_after:
                return
            await reader.read()  # держим соединение, пока клиент не уйдёт
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
