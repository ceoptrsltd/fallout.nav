"""Магнитометры на I2C: IST8310 (Holybro M8N), QMC5883L, HMC5883L; автоопределение; поток опроса.

smbus2 импортируется лениво (на Windows его нет). Значения — мкТл.

Оси. Драйвер отдаёт значения в осях чипа, затем таблица AXES переводит их в оси корпуса FRD
(x — вперёд по стрелке модуля, y — вправо, z — вниз). Оси самого IST8310 (по драйверу PX4):
«x вперёд, y вправо, z вверх» — левая тройка, поэтому z инвертируется. На GPS-модулях (Holybro и др.)
чип стоит развёрнутым: ArduPilot по умолчанию применяет к внешнему IST8310 поворот PITCH_180 поверх своей
перестановки осей, что в итоге = развороту на 180° по курсу → по умолчанию "-x,-y,-z".
Остаточный поворот (90°/180°) закрывает `compass.mount_offset_deg`. Если курс «крутится не в ту сторону»
(зеркально) — переопределить оси: settings.compass.axes="x,-y,-z" (или env FNAV_COMPASS_AXES).
Проверка на устройстве: python -m fnav.sensors.selftest (знак z и направление вращения).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from typing import Protocol

ISENTEK_SCALE = 0.3  # мкТл/LSB


class I2CBus(Protocol):
    def read_byte_data(self, addr: int, reg: int) -> int: ...

    def write_byte_data(self, addr: int, reg: int, val: int) -> None: ...

    def read_i2c_block_data(self, addr: int, reg: int, length: int) -> list[int]: ...

    def close(self) -> None: ...


def open_bus(n: int) -> I2CBus:
    from smbus2 import SMBus  # apt: python3-smbus2 / pip smbus2

    return SMBus(n)


def _s16le(lo: int, hi: int) -> int:
    v = lo | (hi << 8)
    return v - 0x10000 if v & 0x8000 else v


def _s16be(hi: int, lo: int) -> int:
    return _s16le(lo, hi)


class MagDriver:
    name = "?"
    default_address = 0

    def __init__(self, bus: I2CBus, address: int | None = None):
        self.bus = bus
        self.address = self.default_address if address is None else address

    def probe(self) -> bool:
        raise NotImplementedError

    def init(self) -> None:
        raise NotImplementedError

    def read(self) -> tuple[float, float, float] | None:
        """мкТл в осях чипа; None — данные не готовы/переполнение."""
        raise NotImplementedError


class IST8310(MagDriver):
    name = "ist8310"
    default_address = 0x0E
    WAI, WAI_VALUE = 0x00, 0x10
    STAT1, DATA, CNTL1, CNTL2, AVGCNTL, PDCNTL = 0x02, 0x03, 0x0A, 0x0B, 0x41, 0x42
    SINGLE = 0x01

    def __init__(self, bus: I2CBus, address: int | None = None, sleep: Callable[[float], None] = time.sleep):
        super().__init__(bus, address)
        self.sleep = sleep
        self._pending = False

    def probe(self) -> bool:
        return self.bus.read_byte_data(self.address, self.WAI) == self.WAI_VALUE

    def init(self) -> None:
        a = self.address
        self.bus.write_byte_data(a, self.CNTL2, 0x01)  # программный сброс
        self.sleep(0.01)
        self.bus.write_byte_data(a, self.AVGCNTL, 0x24)  # усреднение 16× по X/Z и по Y (меньше шума)
        self.bus.write_byte_data(a, self.PDCNTL, 0xC0)  # «нормальная» длительность импульса (как в PX4)
        self._trigger()

    def _trigger(self) -> None:
        self.bus.write_byte_data(self.address, self.CNTL1, self.SINGLE)
        self._pending = True

    def read(self) -> tuple[float, float, float] | None:
        a = self.address
        if not self._pending:
            self._trigger()
        # ждём DRDY до ~30 мс (обычно уже готово: измерение запускается сразу после предыдущего чтения)
        for _ in range(10):
            if self.bus.read_byte_data(a, self.STAT1) & 0x01:
                break
            self.sleep(0.003)
        else:
            self._trigger()
            return None
        d = self.bus.read_i2c_block_data(a, self.DATA, 6)
        self._trigger()  # конвейер: следующее измерение идёт, пока мы спим
        x, y, z = _s16le(d[0], d[1]), _s16le(d[2], d[3]), _s16le(d[4], d[5])
        if max(abs(x), abs(y), abs(z)) >= 32760:
            return None
        return x * ISENTEK_SCALE, y * ISENTEK_SCALE, z * ISENTEK_SCALE


class QMC5883L(MagDriver):
    name = "qmc5883l"
    default_address = 0x0D
    SCALE = 100.0 / 3000.0  # диапазон ±8 Гс: 3000 LSB/Гс, 1 Гс = 100 мкТл

    def probe(self) -> bool:
        return self.bus.read_byte_data(self.address, 0x0D) == 0xFF

    def init(self) -> None:
        self.bus.write_byte_data(self.address, 0x0B, 0x01)  # SET/RESET period
        self.bus.write_byte_data(self.address, 0x09, 0x1D)  # непрерывно, 200 Гц, ±8 Гс, OSR 512

    def read(self) -> tuple[float, float, float] | None:
        st = self.bus.read_byte_data(self.address, 0x06)
        if st & 0x02:  # переполнение
            self.bus.read_i2c_block_data(self.address, 0x00, 6)
            return None
        if not st & 0x01:
            return None
        d = self.bus.read_i2c_block_data(self.address, 0x00, 6)
        return (_s16le(d[0], d[1]) * self.SCALE, _s16le(d[2], d[3]) * self.SCALE, _s16le(d[4], d[5]) * self.SCALE)


class HMC5883L(MagDriver):
    name = "hmc5883l"
    default_address = 0x1E
    SCALE = 100.0 / 1090.0  # ±1.3 Гс

    def probe(self) -> bool:
        return bytes(self.bus.read_i2c_block_data(self.address, 0x0A, 3)) == b"H43"

    def init(self) -> None:
        self.bus.write_byte_data(self.address, 0x00, 0x70)  # 8 усреднений, 15 Гц
        self.bus.write_byte_data(self.address, 0x01, 0x20)  # 1090 LSB/Гс
        self.bus.write_byte_data(self.address, 0x02, 0x00)  # непрерывный режим

    def read(self) -> tuple[float, float, float] | None:
        d = self.bus.read_i2c_block_data(self.address, 0x03, 6)  # X, Z, Y (big endian)
        x, z, y = _s16be(d[0], d[1]), _s16be(d[2], d[3]), _s16be(d[4], d[5])
        if -4096 in (x, y, z):
            return None
        return x * self.SCALE, y * self.SCALE, z * self.SCALE


DRIVERS: dict[str, type[MagDriver]] = {"ist8310": IST8310, "qmc5883l": QMC5883L, "hmc5883l": HMC5883L}
AUTO_ORDER = ("ist8310", "qmc5883l", "hmc5883l")

# оси чипа → оси корпуса FRD
AXES: dict[str, str] = {
    "ist8310": "-x,-y,-z",
    "qmc5883l": "x,-y,-z",  # типичные платы: x вперёд, y влево, z вверх (правая тройка)
    "hmc5883l": "x,-y,-z",
}


def parse_axes(spec: str) -> list[tuple[int, float]]:
    """'x,-y,-z' → [(0,1),(1,-1),(2,-1)]: body[i] = sign * chip[idx]."""
    parts = [p.strip().lower() for p in spec.split(",")]
    if len(parts) != 3:
        raise ValueError(f"axes: expected 3 items, got {spec!r}")
    out = []
    for p in parts:
        sign = -1.0 if p.startswith("-") else 1.0
        name = p.lstrip("+-")
        if name not in ("x", "y", "z"):
            raise ValueError(f"axes: unknown axis {p!r}")
        out.append(("xyz".index(name), sign))
    if sorted(i for i, _ in out) != [0, 1, 2]:
        raise ValueError(f"axes: each axis must appear exactly once: {spec!r}")
    return out


def axes_spec_for(chip: str, override: str = "") -> str:
    """Действующая строка осей: settings.compass.axes > env FNAV_COMPASS_AXES > таблица AXES.
    Неверная строка пропускается (берётся следующий источник)."""
    for spec in (override, os.environ.get("FNAV_COMPASS_AXES", "")):
        if spec and spec.strip():
            try:
                parse_axes(spec)
            except ValueError:
                continue
            return ",".join(p.strip().lower() for p in spec.split(","))
    return AXES.get(chip, "x,y,z")


def axes_for(chip: str, override: str = "") -> list[tuple[int, float]]:
    return parse_axes(axes_spec_for(chip, override))


def map_axes(v: tuple[float, float, float], axes: list[tuple[int, float]]) -> tuple[float, float, float]:
    return (axes[0][1] * v[axes[0][0]], axes[1][1] * v[axes[1][0]], axes[2][1] * v[axes[2][0]])


def detect(bus: I2CBus, chip: str = "auto", address: int | None = None) -> MagDriver | None:
    names = AUTO_ORDER if chip == "auto" else (chip,)
    for name in names:
        cls = DRIVERS[name]
        addrs = [address] if (address is not None) else [cls.default_address]
        if chip == "auto" and address is not None and address != cls.default_address:
            addrs.append(cls.default_address)
        for a in addrs:
            drv = cls(bus, a)
            try:
                if drv.probe():
                    return drv
            except OSError:
                continue
    return None


class CompassReader(threading.Thread):
    """Поток опроса. Сам переподключается; ошибки → on_error(текст), восстановление → on_error(None)."""

    def __init__(
        self,
        get_cfg: Callable[[], tuple],  # (chip, bus, address, rate_hz[, axes_override])
        on_sample: Callable[[tuple[float, float, float], str], None],
        on_error: Callable[[str | None], None],
        bus_opener: Callable[[int], I2CBus] = open_bus,
    ):
        super().__init__(name="fnav-compass", daemon=True)
        self.get_cfg = get_cfg
        self.on_sample = on_sample
        self.on_error = on_error
        self.bus_opener = bus_opener
        self._stop_ev = threading.Event()
        self._reinit = threading.Event()
        self.driver: MagDriver | None = None
        self.axes_spec = ""
        self.samples = 0

    def stop(self) -> None:
        self._stop_ev.set()

    def request_reinit(self) -> None:
        self._reinit.set()

    def _close(self, bus: I2CBus | None) -> None:
        self.driver = None
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.close()

    def run(self) -> None:
        bus: I2CBus | None = None
        backoff = 1.0
        fails = 0
        while not self._stop_ev.is_set():
            cfg = self.get_cfg()
            chip, bus_no, addr, rate = cfg[:4]
            axes_override = cfg[4] if len(cfg) > 4 else ""
            if self._reinit.is_set():
                self._reinit.clear()
                self._close(bus)
                bus = None
            if self.driver is None:
                try:
                    if bus is None:
                        bus = self.bus_opener(bus_no)
                    drv = detect(bus, chip, addr)
                    if drv is None:
                        want = chip if chip != "auto" else "IST8310/QMC5883L/HMC5883L"
                        raise OSError(f"Compass: {want} not connected (nothing found on I2C-{bus_no})")
                    drv.init()
                    self.driver = drv
                    axes = axes_for(drv.name, axes_override)
                    self.axes_spec = axes_spec_for(drv.name, axes_override)
                    backoff, fails = 1.0, 0
                    self.on_error(None)
                except ImportError:
                    self.on_error("Compass: python module smbus2 is not installed")
                    self._stop_ev.wait(30.0)
                    continue
                except Exception as e:  # noqa: BLE001 — OSError, ошибки шины, нет /dev/i2c-N
                    self.on_error(str(e) if str(e).startswith("Compass") else f"Compass: {e}")
                    self._close(bus)
                    bus = None
                    self._stop_ev.wait(backoff)
                    backoff = min(10.0, backoff * 2)
                    continue
            t0 = time.monotonic()
            try:
                v = self.driver.read()
                fails = 0
                if v is not None:
                    self.samples += 1
                    self.on_sample(map_axes(v, axes), self.driver.name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                if fails >= 3:
                    self.on_error(f"Compass: not responding ({e.__class__.__name__}: {e})")
                    self._close(bus)
                    bus = None
                    self._stop_ev.wait(1.0)
                    continue
            period = 1.0 / max(1, rate)
            self._stop_ev.wait(max(0.0, period - (time.monotonic() - t0)))
        self._close(bus)
