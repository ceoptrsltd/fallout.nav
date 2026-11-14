"""Fossibot (платформа Sydpower: F2400, F3600 Pro, AFERIY…) по Bluetooth LE — только чтение (docs/contracts/power.md).

Похоже на Modbus поверх BLE. Запрос — запись с подтверждением в c304: чтение 80 input-регистров с 0
``11 04 00 00 00 50 A6 F2`` (CRC16/Modbus, но старшим байтом вперёд). Подписаться надо на c305 (notify, туда приходит
ответ) и обязательно на c306 (indicate) — без неё станция не отвечает. Ответ: ``11 04 00 00 00 50 <80 регистров
big-endian> <CRC>``, 168 байт; регистр N — байты 6 + 2N. Пароль и сопряжение не нужны; Wi-Fi и приложение станции
не мешают.

Ничего не записывать: запись некоторых настроек (например, 0 в регистр 68) превращала такие станции в кирпич.
Протокол: github.com/dandwhelan/fossibot-bluetooth, github.com/Ylianst/ESP-FBot.

Здесь только протокол; подключение — fnav/core/ble.py, опрос — fnav/core/power.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from fnav.core.daly import crc16

WRITE_UUID = "0000c304-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"
INDICATE_UUID = "0000c306-0000-1000-8000-00805f9b34fb"
ADDR = 0x11
REGS = 80
FRAME_LEN = 6 + 2 * REGS + 2  # 168


def read_request(start: int = 0, count: int = REGS) -> bytes:
    p = bytes([ADDR, 0x04, start >> 8, start & 0xFF, count >> 8, count & 0xFF])
    c = crc16(p)
    return p + bytes([c >> 8, c & 0xFF])


REQUEST = read_request()  # 11 04 00 00 00 50 A6 F2
HEADER = REQUEST[:6]


class Frames:
    """Склейка notify-пакетов в ответ: начинается с заголовка запроса, длина FRAME_LEN."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def clear(self) -> None:
        self.buf.clear()

    def feed(self, chunk: bytes) -> bytes | None:
        if not self.buf:
            i = chunk.find(HEADER[:2])
            if i < 0:
                return None
            chunk = chunk[i:]
        self.buf.extend(chunk)
        if len(self.buf) < FRAME_LEN:
            return None
        frame = bytes(self.buf[:FRAME_LEN])
        self.buf.clear()
        return frame if crc_ok(frame) else None


def crc_ok(frame: bytes) -> bool:
    """CRC16/Modbus всего кадра, кроме двух последних байт, старшим байтом вперёд (проверено на F3600 Pro)."""
    c = crc16(frame[:-2])
    return frame[-2:] == bytes([c >> 8, c & 0xFF])


@dataclass
class FossibotReading:
    soc_pct: float
    input_w: int  # вход всего: сеть + солнце/авто
    ac_in_w: int
    dc_in_w: int
    output_w: int  # выход всего (как в приложении)
    ac_out_w: int
    ac_out_v: float
    ac_out_hz: float
    usb_w: float  # USB-A1/A2 + USB-C1…C4
    to_full_min: int
    to_empty_min: int
    capacity_ah: float
    charging: bool  # вход > 0 (флаг регистра 48 не проверен на зарядке)
    status: int  # регистр 48: 0x8000 — заряжается, 0x4000 — нет (по документации)


def parse(frame: bytes) -> FossibotReading | None:
    if len(frame) != FRAME_LEN or frame[:2] != HEADER[:2] or not crc_ok(frame):
        return None
    r = [int.from_bytes(frame[6 + 2 * i:8 + 2 * i], "big") for i in range(REGS)]
    soc = r[56] / 10
    if not 0 <= soc <= 100:
        return None
    usb = sum(r[i] for i in (30, 31, 34, 35, 36, 37)) / 10
    return FossibotReading(
        soc_pct=soc, input_w=r[6], ac_in_w=r[3], dc_in_w=r[4], output_w=r[39], ac_out_w=r[20],
        ac_out_v=r[18] / 10, ac_out_hz=r[19] / 10, usb_w=round(usb, 1), to_full_min=r[58], to_empty_min=r[59],
        capacity_ah=r[54] / 10, charging=r[6] > 0, status=r[48],
    )
