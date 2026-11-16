"""Daly BMS по Bluetooth LE — только чтение (docs/contracts/power.md).

Новый протокол Daly: Modbus RTU поверх BLE. Сервис fff0, ответы — notify fff1, запросы — write fff2 (без ответа).
Запрос: чтение 62 регистров с 0 (функция 0x03, адрес 0xD2), CRC16/Modbus младшим байтом вперёд. Ответ
``D2 03 7C <62 регистра big-endian> <CRC>`` — бывает несколькими notify, их склеиваем. Пароль и сопряжение не нужны;
BMS держит одно подключение (пока подключены мы — приложение Daly на телефоне не подключится).

Запись — только MOSFET заряда и разряда (mosfet_request): функция 0x06, регистр 0xA5 — заряд, 0xA6 — разряд, 0/1;
так делает само приложение Daly (перехват трафика в fl4p/batmon-ha issue #356, драйвер daly2 — проверено на BMS),
пароль не нужен. Регистры 0x0C/0x0D из описания Daly отвечают, но MOSFET не переключают — не используем. Состояние —
регистры 53 (заряд) и 54 (разряд), 52 — балансировка. Ничего другого не записываем: это настройки BMS.

Здесь только протокол; подключение — fnav/core/ble.py, опрос — fnav/core/power.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
ADDR = 0xD2
REGS = 62
CHARGE_MIN_A = 0.3  # ток заряда меньше — не зарядка (шум датчика тока)


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

MOSFET_REGS = {"charge": 0xA5, "discharge": 0xA6}  # единственное, что можно записать


def mosfet_request(which: str, on: bool) -> bytes:
    """MOSFET заряда/разряда: ``D2 06 00 A5|A6 00 <0|1> <CRC младшим вперёд>``. Другое — KeyError."""
    p = bytes([ADDR, 0x06, 0x00, MOSFET_REGS[which], 0x00, 1 if on else 0])
    c = crc16(p)
    return p + bytes([c & 0xFF, c >> 8])


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
    current_a: float  # плюс — заряд (замер 2026-11-16: +33,4 А на зарядке, заряд растёт; регистр 57 — мощность, Вт)
    soc_pct: float
    cells_mv: list[int] = field(default_factory=list)
    temps_c: list[int] = field(default_factory=list)
    cell_min_mv: int | None = None
    cell_max_mv: int | None = None
    delta_mv: int | None = None
    cycles: int | None = None
    charge_on: bool | None = None  # MOSFET заряда (регистр 53)
    discharge_on: bool | None = None  # MOSFET разряда (регистр 54)
    balancing: bool | None = None  # регистр 52

    @property
    def charging(self) -> bool:
        return self.current_a >= CHARGE_MIN_A

    @property
    def input_w(self) -> int:
        """Мощность зарядки, Вт (не заряжается — 0)."""
        return round(self.voltage_v * self.current_a) if self.charging else 0


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
        charge_on=bool(r[53]),
        discharge_on=bool(r[54]),
        balancing=bool(r[52]),
    )
