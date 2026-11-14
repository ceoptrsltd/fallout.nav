"""Свинцовый аккумулятор 12 V — модуль XIAO ESP32-C6 (firmware/lead-battery), третья станция (docs/contracts/power.md).

Модуль меряет напряжение аккумулятора и входа 12 V, при 12 V на входе включает зарядное устройство (реле) и отдаёт
это по BLE. CMD: 0x01 — прислать STATUS сейчас (notify; сам — раз в 2 с); 0x10 / 0x11 / 0x12 — режим зарядки: авто
(по входу 12 V) / включить / выключить вручную (до перезагрузки модуля; после неё — авто).
STATUS, 14 байт LE: ``'F' 'L' ver flags vbat_mV(u16) vin_mV(u16) uptime_s(u32) crc16(u16)``; flags: bit0 — на входе
12 V, bit1 — зарядка включена (реле), bits 2–3 — режим (0 авто, 1 вкл, 2 выкл); CRC16/Modbus первых 12 байт.

Заряд, % — оценка по напряжению (таблица покоя свинцово-кислотного 12 V); при зарядке напряжение выше покоя,
и оценка завышена — поэтому рядом всегда флаг зарядки.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from itertools import pairwise

from fnav.core.daly import crc16

SERVICE_UUID = "6e4c0001-f4a5-4c3a-9f39-1d2b5f0c7a01"
STATUS_UUID = "6e4c0002-f4a5-4c3a-9f39-1d2b5f0c7a01"
CMD_UUID = "6e4c0003-f4a5-4c3a-9f39-1d2b5f0c7a01"
REQUEST = b"\x01"
FRAME_LEN = 14
MODE_CMDS = {"auto": 0x10, "on": 0x11, "off": 0x12}  # единственное, что можно записать, кроме REQUEST
MODES = ("auto", "on", "off")


def mode_request(mode: str) -> bytes:
    """Режим зарядки: авто / вручную вкл / вручную выкл. Другое — KeyError."""
    return bytes([MODE_CMDS[mode]])

# напряжение покоя 12 V свинцово-кислотного → заряд, % (по возрастанию)
SOC_TABLE = ((11.31, 0), (11.51, 10), (11.66, 20), (11.81, 30), (11.96, 40), (12.10, 50), (12.24, 60), (12.37, 70),
             (12.50, 80), (12.62, 90), (12.73, 100))


def soc_from_voltage(v: float) -> float:
    if v <= SOC_TABLE[0][0]:
        return 0.0
    for (v0, s0), (v1, s1) in pairwise(SOC_TABLE):
        if v <= v1:
            return round(s0 + (s1 - s0) * (v - v0) / (v1 - v0), 1)
    return 100.0


class Frames:
    """Каждое уведомление — целый кадр."""

    def clear(self) -> None:
        pass

    def feed(self, chunk: bytes) -> bytes | None:
        return chunk if len(chunk) == FRAME_LEN and chunk[:2] == b"FL" else None


@dataclass
class LeadReading:
    voltage_v: float
    soc_pct: float  # оценка по напряжению
    input_v: float
    input_on: bool  # на входе есть 12 V
    charging: bool  # реле зарядного устройства включено
    uptime_s: int
    mode: str = "auto"  # auto — по входу 12 V; on / off — вручную


def parse(frame: bytes) -> LeadReading | None:
    if len(frame) != FRAME_LEN or frame[:2] != b"FL" or crc16(frame[:12]) != frame[12] | (frame[13] << 8):
        return None
    _ver, flags, vb, vi, up = struct.unpack_from("<BBHHI", frame, 2)
    v = vb / 1000
    return LeadReading(voltage_v=round(v, 2), soc_pct=soc_from_voltage(v), input_v=round(vi / 1000, 2),
                       input_on=bool(flags & 1), charging=bool(flags & 2), uptime_s=up,
                       mode=MODES[(flags >> 2) & 3] if (flags >> 2) & 3 < len(MODES) else "auto")


def frame(vbat: float, vin: float, flags: int, uptime_s: int = 0) -> bytes:
    """Кадр как у модуля (тесты, --mock)."""
    body = b"FL" + struct.pack("<BBHHI", 1, flags, round(vbat * 1000), round(vin * 1000), uptime_s)
    c = crc16(body)
    return body + bytes([c & 0xFF, c >> 8])
