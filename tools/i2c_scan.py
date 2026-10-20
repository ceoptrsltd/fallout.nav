#!/usr/bin/env python3
"""Scan an I2C bus (default 1) and try to identify chips by their ID registers.

    python tools/i2c_scan.py            # bus 1
    python tools/i2c_scan.py --bus 0

Needs smbus2 (apt: python3-smbus2) and access to /dev/i2c-N (group i2c).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Callable
from typing import Any

# address -> list of (description, identify(bus, addr) -> bool | str)
IdFn = Callable[[Any, int], "bool | str"]


def _reg(bus: Any, addr: int, reg: int) -> int:
    return bus.read_byte_data(addr, reg)


def _ist8310(bus: Any, addr: int) -> bool | str:
    v = _reg(bus, addr, 0x00)
    return v == 0x10 or f"WHO_AM_I=0x{v:02X} (IST8310 expects 0x10)"


def _qmc5883l(bus: Any, addr: int) -> bool:
    return _reg(bus, addr, 0x0D) == 0xFF


def _hmc5883l(bus: Any, addr: int) -> bool:
    return bytes(bus.read_i2c_block_data(addr, 0x0A, 3)) == b"H43"


def _ak09916(bus: Any, addr: int) -> bool:
    return _reg(bus, addr, 0x01) == 0x09


def _ak8963(bus: Any, addr: int) -> bool:
    return _reg(bus, addr, 0x00) == 0x48


def _mpu(bus: Any, addr: int) -> bool | str:
    v = _reg(bus, addr, 0x75)
    names = {0x68: "MPU6050/9250?", 0x71: "MPU9250", 0x70: "MPU6500", 0x12: "ICM20602", 0x98: "ICM20689"}
    return names.get(v, False) or False


def _bmp(bus: Any, addr: int) -> bool | str:
    v = _reg(bus, addr, 0xD0)
    return {0x58: "BMP280", 0x60: "BME280", 0x55: "BMP180", 0x50: "BMP388"}.get(v, False) or False


def _ms5611(bus: Any, addr: int) -> bool:
    return True  # нет регистра ID — только адрес


KNOWN: dict[int, list[tuple[str, IdFn]]] = {
    0x0C: [("AK09916 magnetometer", _ak09916), ("AK8963 magnetometer", _ak8963)],
    0x0D: [("QMC5883L magnetometer", _qmc5883l)],
    0x0E: [("IST8310 magnetometer (Holybro M8N compass)", _ist8310)],
    0x1E: [("HMC5883L magnetometer", _hmc5883l)],
    0x68: [("IMU / RTC", _mpu)],
    0x69: [("IMU", _mpu)],
    0x76: [("pressure sensor", _bmp), ("MS5611 baro?", _ms5611)],
    0x77: [("pressure sensor", _bmp), ("MS5611 baro?", _ms5611)],
}


def probe_address(bus: Any, addr: int) -> bool:
    try:
        if 0x30 <= addr <= 0x37 or 0x50 <= addr <= 0x5F:
            bus.read_byte(addr)
        else:
            bus.write_quick(addr)
        return True
    except OSError:
        return False


def identify(bus: Any, addr: int) -> str:
    for desc, fn in KNOWN.get(addr, []):
        try:
            r = fn(bus, addr)
        except OSError:
            continue
        if r is True:
            return desc
        if isinstance(r, str) and r:
            return f"{desc}: {r}" if "expects" in r else r
    return "unknown"


def scan(bus: Any) -> list[int]:
    return [a for a in range(0x03, 0x78) if probe_address(bus, a)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="I2C scan with chip identification")
    ap.add_argument("--bus", type=int, default=1)
    a = ap.parse_args(argv)
    try:
        from smbus2 import SMBus
    except ImportError:
        print("python module smbus2 is not installed (apt install python3-smbus2)")
        return 2
    try:
        bus = SMBus(a.bus)
    except OSError as e:
        print(f"I2C bus {a.bus} not available ({e}); enable I2C: dtparam=i2c_arm=on, check group i2c")
        return 2
    try:
        found = scan(bus)
        print("     " + " ".join(f"{i:2x}" for i in range(16)))
        for row in range(0, 0x80, 16):
            cells = []
            for col in range(16):
                ad = row + col
                if ad < 0x03 or ad > 0x77:
                    cells.append("  ")
                else:
                    cells.append(f"{ad:02x}" if ad in found else "--")
            print(f"{row:02x}:  " + " ".join(cells))
        print()
        if not found:
            print(f"No devices found on I2C-{a.bus}: compass NOT CONNECTED? (check SDA/SCL, 5V and GND)")
            return 1
        for ad in found:
            print(f"0x{ad:02X}: {identify(bus, ad)}")
        if 0x0E not in found:
            print("Note: IST8310 (0x0E) not found - the Holybro M8N compass is not connected")
        return 0
    finally:
        with contextlib.suppress(Exception):
            bus.close()


if __name__ == "__main__":
    sys.exit(main())
