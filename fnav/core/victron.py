"""Victron SmartSolar (солнечный контроллер заряда) — «Instant readout»: показания в вещании Bluetooth LE,
зашифрованы ключом устройства (приложение VictronConnect → Product info → Instant readout details).
docs/contracts/power.md «Третья станция».

Данные производителя 0x02E1:
    10 xx | model(u16) | тип(1) | iv(u16) | байт проверки ключа | шифртекст
тип 0x01 — солнечный контроллер. Шифр AES-CTR, счётчик — iv (little-endian, 128 бит); первый байт после iv должен
совпасть с первым байтом ключа. Расшифрованное (little-endian, поля подряд):
    состояние(1) ошибка(1) напряжение(s16, 0.01 В) ток заряда(s16, 0.1 А) выработка за день(u16, 0.01 кВт·ч)
    мощность панелей(u16, Вт) ток нагрузки(9 бит, 0.1 А)
Значения «нет данных»: 0x7FFF, 0xFFFF, 0x1FF. Формат — по keshavdv/victron-ble (проверено на устройстве).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

COMPANY_ID = 0x02E1
SOLAR_CHARGER = 0x01
STATES = {0: "off", 1: "low_power", 2: "fault", 3: "bulk", 4: "absorption", 5: "float", 6: "storage",
          7: "equalize", 245: "starting", 247: "equalize", 252: "external"}
CHARGING_STATES = frozenset({"bulk", "absorption", "float", "storage", "equalize"})


@dataclass(frozen=True)
class VictronReading:
    voltage_v: float | None
    current_a: float | None  # ток заряда батареи
    pv_power_w: int | None  # мощность панелей
    yield_today_wh: int | None
    load_a: float | None
    state: str
    error: int

    @property
    def charging(self) -> bool:
        return self.state in CHARGING_STATES and (self.current_a or 0) > 0


def key_ok(key: str) -> bool:
    try:
        return len(bytes.fromhex(key)) == 16
    except ValueError:
        return False


def decrypt(raw: bytes, key: bytes) -> bytes | None:
    """Расшифрованная часть пакета или None (не тот пакет, не тот ключ, нет библиотеки cryptography)."""
    if len(raw) < 9 or raw[0] != 0x10 or raw[7] != key[0]:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return None
    iv = struct.unpack_from("<H", raw, 5)[0]
    data = raw[8:]
    # счётчик little-endian: для первого блока (показания короче 16 байт) это просто iv в 16 байтах
    dec = Cipher(algorithms.AES(key), modes.CTR(iv.to_bytes(16, "little"))).decryptor()
    return dec.update(data[:16].ljust(16, b"\0"))[:len(data)]


def parse(raw: bytes, key: str) -> VictronReading | None:
    """Пакет вещания (данные производителя 0x02E1) → показания; None — не солнечный контроллер или не расшифровался."""
    if not key_ok(key) or len(raw) < 8 + 10 or raw[4] != SOLAR_CHARGER:
        return None
    d = decrypt(raw, bytes.fromhex(key))
    if d is None or len(d) < 10:
        return None
    state, error, volts, amps, yld, pv = struct.unpack_from("<BBhhHH", d, 0)
    load = (d[10] | (d[11] << 8)) & 0x1FF if len(d) >= 12 else 0x1FF
    return VictronReading(
        voltage_v=None if volts == 0x7FFF else round(volts / 100, 2),
        current_a=None if amps == 0x7FFF else round(amps / 10, 1),
        pv_power_w=None if pv == 0xFFFF else pv,
        yield_today_wh=None if yld == 0xFFFF else yld * 10,
        load_a=None if load == 0x1FF else round(load / 10, 1),
        state=STATES.get(state, f"state {state}"), error=error)


def encrypt_frame(key: str, iv: int, state: int, volts: float, amps: float, yield_wh: int, pv_w: int,
                  load_a: float = 0.0) -> bytes:
    """Собрать пакет вещания (тесты и --mock)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    k = bytes.fromhex(key)
    plain = struct.pack("<BBhhHH", state, 0, round(volts * 100), round(amps * 10), yield_wh // 10, pv_w)
    plain += struct.pack("<H", round(load_a * 10) & 0x1FF)
    enc = Cipher(algorithms.AES(k), modes.CTR(iv.to_bytes(16, "little"))).encryptor()
    return bytes([0x10, 0x02]) + struct.pack("<HBH", 0xA053, SOLAR_CHARGER, iv) + bytes([k[0]]) + enc.update(plain)
