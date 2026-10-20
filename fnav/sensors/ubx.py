"""UBX-протокол u-blox: сборка/разбор кадров, контрольная сумма, сообщения CFG для NEO-M8N.

Только stdlib. Кадр:  B5 62 | class | id | len(u2 LE) | payload | CK_A CK_B
Контрольная сумма — 8-битный Fletcher по class..payload.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SYNC1, SYNC2 = 0xB5, 0x62

# классы/идентификаторы
CLS_NAV, CLS_ACK, CLS_CFG, CLS_MON = 0x01, 0x05, 0x06, 0x0A
CLS_NMEA_STD = 0xF0  # стандартные NMEA-сообщения (для CFG-MSG)

ID_ACK_NAK, ID_ACK_ACK = 0x00, 0x01
ID_CFG_PRT, ID_CFG_MSG, ID_CFG_RST, ID_CFG_RATE, ID_CFG_CFG = 0x00, 0x01, 0x04, 0x08, 0x09
ID_CFG_NAV5, ID_CFG_GNSS = 0x24, 0x3E
ID_MON_VER = 0x04
ID_NAV_PVT = 0x07

# NMEA-сообщения: (class, id) для CFG-MSG
NMEA_IDS = {"GGA": 0x00, "GLL": 0x01, "GSA": 0x02, "GSV": 0x03, "RMC": 0x04, "VTG": 0x05, "ZDA": 0x08}

# gnssId в CFG-GNSS
GNSS_IDS = {"gps": 0, "sbas": 1, "galileo": 2, "beidou": 3, "imes": 4, "qzss": 5, "glonass": 6}
GNSS_NAMES = {v: k for k, v in GNSS_IDS.items()}

# динамические модели CFG-NAV5
DYN_MODELS = {"portable": 0, "stationary": 2, "pedestrian": 3, "automotive": 4, "sea": 5,
              "airborne1g": 6, "airborne2g": 7, "airborne4g": 8}

UART1 = 1


def checksum(data: bytes) -> bytes:
    """Fletcher-8 по class, id, length, payload."""
    a = b = 0
    for byte in data:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def frame(cls: int, msg_id: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<BBH", cls, msg_id, len(payload)) + payload
    return bytes((SYNC1, SYNC2)) + body + checksum(body)


@dataclass
class UbxMessage:
    cls: int
    id: int
    payload: bytes = b""

    @property
    def key(self) -> tuple[int, int]:
        return self.cls, self.id

    def is_ack_for(self, cls: int, msg_id: int) -> bool | None:
        """True = ACK, False = NAK, None = это не ответ на (cls, msg_id)."""
        if self.cls != CLS_ACK or len(self.payload) < 2 or tuple(self.payload[:2]) != (cls, msg_id):
            return None
        if self.id == ID_ACK_ACK:
            return True
        if self.id == ID_ACK_NAK:
            return False
        return None


def parse_frame(buf: bytes) -> UbxMessage:
    """Разобрать ровно один кадр (с синхробайтами). ValueError при ошибке."""
    if len(buf) < 8 or buf[0] != SYNC1 or buf[1] != SYNC2:
        raise ValueError("no UBX sync bytes")
    cls, msg_id, length = struct.unpack_from("<BBH", buf, 2)
    if len(buf) != 8 + length:
        raise ValueError("bad frame length")
    if checksum(buf[2 : 6 + length]) != buf[6 + length : 8 + length]:
        raise ValueError("bad checksum")
    return UbxMessage(cls, msg_id, bytes(buf[6 : 6 + length]))


def nmea_checksum_ok(line: str) -> bool:
    line = line.strip()
    if not line.startswith(("$", "!")) or "*" not in line:
        return False
    body, _, ck = line[1:].rpartition("*")
    try:
        want = int(ck[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


@dataclass
class StreamParser:
    """Потоковый разбор смеси UBX и NMEA. feed(bytes) → список UbxMessage | str (строка NMEA)."""

    max_buf: int = 16384
    buf: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[UbxMessage | str]:
        self.buf += data
        out: list[UbxMessage | str] = []
        b = self.buf
        while b:
            if b[0] == SYNC1:
                if len(b) < 2:
                    break
                if b[1] != SYNC2:
                    del b[0]
                    continue
                if len(b) < 6:
                    break
                length = b[4] | (b[5] << 8)
                if length > 4096:  # мусор
                    del b[0]
                    continue
                if len(b) < 8 + length:
                    break
                try:
                    out.append(parse_frame(bytes(b[: 8 + length])))
                    del b[: 8 + length]
                except ValueError:
                    del b[0]
                continue
            if b[0] in (0x24, 0x21):  # '$' / '!'
                nl = b.find(b"\n")
                if nl < 0:
                    if len(b) > 200:
                        del b[0]
                        continue
                    break
                line = bytes(b[:nl]).decode("ascii", "replace").strip()
                del b[: nl + 1]
                if nmea_checksum_ok(line):
                    out.append(line)
                continue
            del b[0]
        if len(b) > self.max_buf:
            del b[: len(b) - self.max_buf]
        return out


# ───────────────────────── сообщения ─────────────────────────


def poll(cls: int, msg_id: int, payload: bytes = b"") -> bytes:
    return frame(cls, msg_id, payload)


def mon_ver_poll() -> bytes:
    return frame(CLS_MON, ID_MON_VER)


def parse_mon_ver(payload: bytes) -> dict:
    def s(b: bytes) -> str:
        return b.split(b"\0", 1)[0].decode("ascii", "replace").strip()

    sw = s(payload[0:30]) if len(payload) >= 30 else ""
    hw = s(payload[30:40]) if len(payload) >= 40 else ""
    ext = [s(payload[i : i + 30]) for i in range(40, len(payload) - 29, 30)]
    return {"sw": sw, "hw": hw, "ext": ext}


def cfg_prt_uart(baud: int, in_proto: int = 0x0003, out_proto: int = 0x0003, port: int = UART1) -> bytes:
    """CFG-PRT для UART: 8N1, протоколы in/out — битовая маска (1=UBX, 2=NMEA, 4=RTCM2)."""
    mode = 0x000008D0  # 8 бит, без чётности, 1 стоп
    payload = struct.pack("<BBHIIHHHH", port, 0, 0, mode, baud, in_proto, out_proto, 0, 0)
    return frame(CLS_CFG, ID_CFG_PRT, payload)


def cfg_rate(rate_hz: float, nav_rate: int = 1, time_ref: int = 1) -> bytes:
    meas_ms = max(25, round(1000.0 / rate_hz))
    return frame(CLS_CFG, ID_CFG_RATE, struct.pack("<HHH", meas_ms, nav_rate, time_ref))


def cfg_nav5(dyn_model: int) -> bytes:
    """CFG-NAV5: меняем только динамическую модель (маска 0x0001)."""
    payload = bytearray(36)
    struct.pack_into("<HB", payload, 0, 0x0001, dyn_model)
    return frame(CLS_CFG, ID_CFG_NAV5, bytes(payload))


def cfg_msg(cls: int, msg_id: int, rate: int) -> bytes:
    """CFG-MSG (короткая форма): частота сообщения на текущем порту (0 = выкл, N = каждую N-ю эпоху)."""
    return frame(CLS_CFG, ID_CFG_MSG, struct.pack("<BBB", cls, msg_id, rate))


def cfg_msg_ports(cls: int, msg_id: int, uart1_rate: int) -> bytes:
    """CFG-MSG (длинная форма, 6 портов): I2C, UART1, UART2, USB, SPI, reserved."""
    return frame(CLS_CFG, ID_CFG_MSG, struct.pack("<BB6B", cls, msg_id, 0, uart1_rate, 0, 0, 0, 0))


def cfg_cfg_save(devices: int = 0x17) -> bytes:
    """CFG-CFG: сохранить всё текущее (save mask = все секции) в BBR/Flash/EEPROM/SPI (deviceMask)."""
    return frame(CLS_CFG, ID_CFG_CFG, struct.pack("<IIIB", 0, 0x00001F1F, 0, devices))


def cfg_rst(nav_bbr_mask: int = 0x0000, reset_mode: int = 0x02) -> bytes:
    """CFG-RST: 0x02 = программный сброс только GNSS (ACK не присылается)."""
    return frame(CLS_CFG, ID_CFG_RST, struct.pack("<HBB", nav_bbr_mask, reset_mode, 0))


# ── CFG-GNSS ──

# Заводские блоки NEO-M8N (gnssId, resTrkCh, maxTrkCh, sigCfgMask)
DEFAULT_GNSS_BLOCKS = [
    (0, 8, 16, 0x01),  # GPS L1C/A
    (1, 1, 3, 0x01),  # SBAS
    (2, 4, 8, 0x01),  # Galileo E1
    (3, 8, 16, 0x01),  # BeiDou B1I
    (4, 0, 8, 0x01),  # IMES
    (5, 0, 3, 0x05),  # QZSS L1C/A + L1SAIF
    (6, 8, 14, 0x01),  # GLONASS L1
]


@dataclass
class GnssBlock:
    gnss_id: int
    res_trk: int
    max_trk: int
    flags: int

    @property
    def enabled(self) -> bool:
        return bool(self.flags & 0x01)

    def with_enabled(self, on: bool) -> GnssBlock:
        sig = (self.flags >> 16) & 0xFF or 0x01
        return GnssBlock(self.gnss_id, self.res_trk, self.max_trk, (sig << 16) | (1 if on else 0))


@dataclass
class GnssConfig:
    num_trk_hw: int = 0
    num_trk_use: int = 0xFF
    blocks: list[GnssBlock] = field(default_factory=list)

    @classmethod
    def default(cls) -> GnssConfig:
        return cls(32, 32, [GnssBlock(g, r, m, (s << 16) | 1) for g, r, m, s in DEFAULT_GNSS_BLOCKS])

    def enabled_names(self) -> list[str]:
        return [GNSS_NAMES.get(b.gnss_id, str(b.gnss_id)) for b in self.blocks if b.enabled]


def parse_cfg_gnss(payload: bytes) -> GnssConfig:
    if len(payload) < 4:
        raise ValueError("CFG-GNSS too short")
    _ver, hw, use, n = struct.unpack_from("<BBBB", payload, 0)
    blocks = []
    for i in range(n):
        off = 4 + 8 * i
        if off + 8 > len(payload):
            break
        g, r, m, _res, flags = struct.unpack_from("<BBBBI", payload, off)
        blocks.append(GnssBlock(g, r, m, flags))
    return GnssConfig(hw, use, blocks)


def build_cfg_gnss(cfg: GnssConfig) -> bytes:
    payload = struct.pack("<BBBB", 0, cfg.num_trk_hw, cfg.num_trk_use, len(cfg.blocks))
    for b in cfg.blocks:
        payload += struct.pack("<BBBBI", b.gnss_id, b.res_trk, b.max_trk, 0, b.flags)
    return frame(CLS_CFG, ID_CFG_GNSS, payload)


def gnss_config_for(current: GnssConfig, wanted: list[str]) -> GnssConfig:
    """Включить нужные системы, выключить остальные основные. SBAS/QZSS — как у GPS (рекомендация u-blox:
    QZSS включать вместе с GPS), IMES — выключен.  Проверка суммы резервных каналов ≤ numTrkChUse."""
    want = set(wanted)
    gps_on = "gps" in want
    blocks: list[GnssBlock] = []
    have = {b.gnss_id for b in current.blocks}
    src = list(current.blocks) + [
        GnssBlock(g, r, m, (s << 16)) for g, r, m, s in DEFAULT_GNSS_BLOCKS if g not in have
    ]
    for b in src:
        name = GNSS_NAMES.get(b.gnss_id, "")
        if name in ("gps", "glonass", "galileo", "beidou"):
            on = name in want
        elif name in ("sbas", "qzss"):
            on = gps_on
        else:
            on = False
        blocks.append(b.with_enabled(on))
    use = current.num_trk_use if current.num_trk_use not in (0, 0xFF) else (current.num_trk_hw or 32)
    hw = current.num_trk_hw or 32
    # резервные каналы включённых систем не должны превышать число каналов
    total_res = sum(b.res_trk for b in blocks if b.enabled)
    if total_res > use:
        blocks = [GnssBlock(b.gnss_id, min(b.res_trk, 4), b.max_trk, b.flags) for b in blocks]
    return GnssConfig(hw, use, sorted(blocks, key=lambda b: b.gnss_id))


def parse_nav_pvt(payload: bytes) -> dict:
    """Минимальный разбор NAV-PVT (для самопроверки/измерения частоты)."""
    if len(payload) < 92:
        raise ValueError("NAV-PVT too short")
    itow, year, month, day, hour, minute, sec = struct.unpack_from("<IHBBBBB", payload, 0)
    fix_type = payload[20]
    num_sv = payload[23]
    lon, lat = struct.unpack_from("<ii", payload, 24)
    g_speed, head_mot = struct.unpack_from("<ii", payload, 60)
    return {
        "itow": itow,
        "fix_type": fix_type,
        "num_sv": num_sv,
        "lat": lat * 1e-7,
        "lon": lon * 1e-7,
        "speed_mps": g_speed / 1000.0,
        "heading_deg": head_mot * 1e-5,
        "time": f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{sec:02d}Z",
    }
