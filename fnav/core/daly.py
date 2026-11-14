"""Daly BMS по Bluetooth LE — только чтение (docs/contracts/power.md).

Новый протокол Daly: Modbus RTU поверх BLE. Сервис fff0, ответы — notify fff1, запросы — write fff2 (без ответа).
Запрос: чтение 62 регистров с 0 (функция 0x03, адрес 0xD2), CRC16/Modbus младшим байтом вперёд. Ответ
``D2 03 7C <62 регистра big-endian> <CRC>`` — бывает несколькими notify, их склеиваем. Пароль и сопряжение не нужны;
BMS держит одно подключение (пока подключены мы — приложение Daly на телефоне не подключится).

Записей (функции 0x06/0x10) не шлём никогда: они меняют настройки BMS и могут отключить MOSFET.

Здесь только протокол; подключение — fnav/core/ble.py, опрос — fnav/core/power.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
ADDR = 0xD2
REGS = 62


def crc16(data: bytes) -> int:
    """CRC16/Modbus: полином 0xA001 (отражённый), начальное 0xFFFF."""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def read_request(start: int = 0, count: int = REGS) -> bytes:
    p = bytes([ADDR, 0x03, start >> 8, start & 0xFF, count >> 8, count & 0xFF])
    c = crc16(p)
    return p + bytes([c & 0xFF, c >> 8])


REQUEST = read_request()  # D2 03 00 00 00 3E D7 B9


class Frames:
    """Склейка notify-пакетов в кадр ответа ``D2 03 <n> <n байт> <CRC>``; мусор до начала кадра отбрасывается."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def clear(self) -> None:
        self.buf.clear()

    def feed(self, chunk: bytes) -> bytes | None:
        self.buf.extend(chunk)
        while True:
            i = self.buf.find(bytes([ADDR, 0x03]))
            if i < 0:
                del self.buf[:-1]  # последний байт может оказаться началом кадра
                return None
            del self.buf[:i]
            if len(self.buf) < 3:
                return None
            n = 5 + self.buf[2]
            if len(self.buf) < n:
                return None
            frame = bytes(self.buf[:n])
            if crc16(frame[:-2]) == frame[-2] | (frame[-1] << 8):
                del self.buf[:n]
                return frame
            del self.buf[:1]  # не сошлась CRC — это было не начало кадра, ищем дальше


@dataclass
class DalyReading:
    voltage_v: float
    current_a: float  # знак (заряд/разряд) не проверен на устройстве
    soc_pct: float
    cells_mv: list[int] = field(default_factory=list)
    temps_c: list[int] = field(default_factory=list)
    cell_min_mv: int | None = None
    cell_max_mv: int | None = None
    delta_mv: int | None = None
    cycles: int | None = None


def parse(frame: bytes) -> DalyReading | None:
    """Кадр ответа на REQUEST → показания; чужой/битый кадр → None."""
    if len(frame) < 5 or frame[0] != ADDR or frame[1] != 0x03 or len(frame) != 5 + frame[2]:
        return None
    if crc16(frame[:-2]) != frame[-2] | (frame[-1] << 8):
        return None
    r = [int.from_bytes(frame[3 + 2 * i:5 + 2 * i], "big") for i in range(frame[2] // 2)]
    if len(r) < 57:
        return None
    n_cells = min(r[49], 32)
    n_temps = min(r[50], 8)
    return DalyReading(
        voltage_v=r[40] / 10,
        current_a=round((r[41] - 30000) / 10, 1),
        soc_pct=r[42] / 10,
        cells_mv=[v for v in r[0:n_cells]],
        temps_c=[t - 40 for t in r[32:32 + n_temps] if t != 0xFF],
        cell_max_mv=r[43] or None,
        cell_min_mv=r[44] or None,
        cycles=r[51],
        delta_mv=r[56],
    )
