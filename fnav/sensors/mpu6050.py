"""MPU-6050 (модуль GY-521) на I2C: акселерометр — для охраны и уровня (docs/contracts/alarm.md §1–2, level.md),
гироскоп — для курса (fnav/sensors/motion.py, hub.py).

Шина — та же, что у компаса (fnav.sensors.compass: I2CBus, open_bus; smbus2 импортируется лениво). Адрес 0x68
(AD0 на модуле подтянут к GND) или 0x69. Акселерометр ±2 g и гироскоп ±250 °/с, DLPF 94 Гц (общий), 200 Гц → FIFO:
12 байт на отсчёт (ax, ay, az, gx, gy, gz). FIFO (1024 байта ≈ 0,4 с) читается ~20 раз в секунду кусками ≤ 32 байт
(предел блока SMBus, кратно 12), переполнение → сброс FIFO. Шина: 2400 байт/с ≈ 22 % от 100 кГц — своя I2C-3
(или вместе с компасом на I2C-1, у него ~2 %).

Сбой FIFO. Поток чтения задержался дольше ~0,4 с (процессор занят) — FIFO переполняется, и поток байт съезжает
посреди пачки: дальше на месте ускорений — угловые скорости или куски чисел, охране это «удар» 0,9–3 g на пустом
месте. Поэтому: переполнение проверяется и до чтения, и после (флаг INT_STATUS, разрешён в INT_ENABLE); пачка с
бессмыслицей (медиана |a| не около 1 g или тяжесть «повернулась» на 50°+ при той же величине — сдвиг на 2–4 байта)
выбрасывается; а каждая пачка отдаётся только после того, как следующая прочиталась чисто (+50 мс): у сбоя испорчен
и конец предыдущей пачки, её тоже выбрасываем. Сколько выброшено и самая долгая пауза между чтениями — в лог раз в
10 минут (AccelReader).

WHO_AM_I: 0x68 — оригинал; родственники и клоны (0x70 MPU-6500, 0x71 MPU-9250, 0x73 MPU-9255, 0x72, 0x98 …)
принимаются, значение пишется в лог и в статус. У семейства MPU-6500 фильтр акселерометра — в отдельном регистре
ACCEL_CONFIG2 (у оригинала по этому адресу ничего нужного нет — ему его не пишем). 0x00/0xFF — на шине никого.

Отсчёты — в g, в осях датчика (как подписано на плате X/Y; Z — от платы, в сторону компонентов).
"""

from __future__ import annotations

import contextlib
import logging
import math
import random
import struct
import threading
import time
from collections.abc import Callable
from typing import Protocol

from fnav.sensors.compass import I2CBus, open_bus

log = logging.getLogger("fnav.sensors.mpu6050")

Vec = tuple[float, float, float]

ADDRESS = 0x68
SMPLRT_DIV, CONFIG, GYRO_CONFIG, ACCEL_CONFIG, ACCEL_CONFIG2 = 0x19, 0x1A, 0x1B, 0x1C, 0x1D
FIFO_EN, INT_ENABLE, INT_STATUS, USER_CTRL, PWR_MGMT_1, PWR_MGMT_2 = 0x23, 0x38, 0x3A, 0x6A, 0x6B, 0x6C
FIFO_COUNTH, FIFO_R_W, WHO_AM_I = 0x72, 0x74, 0x75

GENUINE = 0x68
KNOWN = {0x68: "MPU-6050", 0x70: "MPU-6500", 0x71: "MPU-9250", 0x73: "MPU-9255"}
RATE_HZ = 200.0
LSB_PER_G = 16384.0  # ±2 g
LSB_PER_DPS = 131.0  # ±250 °/с
FRAME = 12  # байт на отсчёт в FIFO: акселерометр X, Y, Z и гироскоп X, Y, Z по 2 байта
FRAME_ACCEL = 6  # без гироскопа (gyro=False)
CHUNK = 24  # байт за одно чтение FIFO: ≤ 32 (блок SMBus) и кратно 12 (и 6)
FIFO_SIZE = 1024  # у MPU-6500 — 512, но там срабатывает флаг переполнения
FIFO_OFLOW = 0x10  # INT_STATUS: FIFO переполнился (сбрасывается чтением); INT_ENABLE: разрешить этот флаг
BAD_BATCHES_MAX = 10  # столько подряд пачек с бессмыслицей (сбой выравнивания FIFO) — датчик «не отвечает»
TURNED_DEG = 50.0  # медиана пачки дальше от тяжести (при той же величине) — FIFO съехал на 2–4 байта: оси переставлены
TURNED_SAME_G = 0.3  # «та же величина»: |медиана| отличается от тяжести меньше (удар её меняет, перестановка — нет)
TURNED_MIN = 4  # в пачке меньше отсчётов — поворот не проверяем (один отсчёт удара может «повернуться»)
GRAVITY_TAU_S = 2.0  # тяжесть для этой проверки — EMA медиан чистых пачек
STATS_EVERY_S = 600.0  # сводка сбоев FIFO в лог не чаще
LATE_S = 0.3  # пауза между чтениями дольше — «поздно» (FIFO с гироскопом полон через ~0,43 с)


def model_name(who_am_i: int) -> str:
    if who_am_i == GENUINE:
        return KNOWN[GENUINE]
    return f"{KNOWN.get(who_am_i, 'MPU-6050 clone')} (WHO_AM_I 0x{who_am_i:02X})"


def parse_fifo(raw: bytes) -> list[Vec]:
    """Байты FIFO (X, Y, Z акселерометра, big-endian int16) → отсчёты в g. Хвост короче 6 байт — отброшен."""
    n = len(raw) - len(raw) % 6
    return [(x / LSB_PER_G, y / LSB_PER_G, z / LSB_PER_G) for x, y, z in struct.iter_unpack(">hhh", raw[:n])]


def parse_fifo_imu(raw: bytes) -> tuple[list[Vec], list[Vec]]:
    """Байты FIFO по 12 (акселерометр, затем гироскоп; big-endian int16) → (отсчёты в g, угловые скорости в °/с).
    Хвост короче 12 байт — отброшен."""
    n = len(raw) - len(raw) % FRAME
    acc: list[Vec] = []
    gyr: list[Vec] = []
    for ax, ay, az, gx, gy, gz in struct.iter_unpack(">hhhhhh", raw[:n]):
        acc.append((ax / LSB_PER_G, ay / LSB_PER_G, az / LSB_PER_G))
        gyr.append((gx / LSB_PER_DPS, gy / LSB_PER_DPS, gz / LSB_PER_DPS))
    return acc, gyr


def plausible(samples: list[Vec]) -> bool:
    """Датчик в машине чувствует тяжесть: медиана |a| около 1 g. Иначе FIFO сбился (выравнивание по 6 байт)."""
    if not samples:
        return True
    mags = sorted((x * x + y * y + z * z) ** 0.5 for x, y, z in samples)
    return 0.5 <= mags[len(mags) // 2] <= 1.5


def median_vec(samples: list[Vec]) -> Vec:
    """Медиана по каждой оси (один отсчёт удара её не двигает)."""
    m = len(samples) // 2
    x, y, z = (sorted(s[k] for s in samples)[m] for k in range(3))
    return (x, y, z)


def turned(samples: list[Vec], gravity: Vec | None) -> bool:
    """Пачка с переставленными осями (FIFO съехал на 2–4 байта: на месте X — Y или Z): медиана той же величины, что
    тяжесть, но повёрнута от неё на TURNED_DEG+. Машина так не поворачивается за 50 мс, а удар меняет и величину."""
    if gravity is None or len(samples) < TURNED_MIN:
        return False
    m = median_vec(samples)
    mm, gg = math.hypot(*m), math.hypot(*gravity)
    if mm < 1e-6 or gg < 1e-6 or abs(mm - gg) >= TURNED_SAME_G:
        return False
    cos = (m[0] * gravity[0] + m[1] * gravity[1] + m[2] * gravity[2]) / (mm * gg)
    return cos < math.cos(math.radians(TURNED_DEG))


class AccelSource(Protocol):
    """Источник отсчётов. С гироскопом — ещё has_gyro = True и read_imu() → (акселерометр, гироскоп в °/с)."""

    model: str
    address: int
    rate_hz: float

    def read(self) -> list[Vec]: ...

    def close(self) -> None: ...


class Mpu6050:
    rate_hz = RATE_HZ

    def __init__(self, bus: I2CBus, address: int = ADDRESS, sleep: Callable[[float], None] = time.sleep,
                 gyro: bool = True):
        self.bus = bus
        self.address = address
        self.sleep = sleep
        self.has_gyro = gyro
        self.frame = FRAME if gyro else FRAME_ACCEL
        self.who_am_i: int | None = None
        self.overflows = 0  # переполнений FIFO (до чтения и во время него)
        self.misaligned = 0  # пачек с бессмыслицей (FIFO съехал)
        self.dropped = 0  # отсчётов выброшено из-за сбоев (и придержанных, и испорченных)
        self.last_bad: str | None = None  # начало последней пачки с бессмыслицей (hex) — для лога
        self._bad = 0
        self._held: tuple[list[Vec], list[Vec]] | None = None  # чистая пачка ждёт, пока следующая прочитается
        self._gravity: Vec | None = None  # EMA медиан чистых пачек — для turned()

    @property
    def model(self) -> str:
        return model_name(self.who_am_i) if self.who_am_i is not None else "MPU-6050"

    def probe(self) -> int:
        """WHO_AM_I; нет ответа на шине — OSError."""
        self.who_am_i = self.bus.read_byte_data(self.address, WHO_AM_I) & 0xFF
        return self.who_am_i

    def init(self) -> None:
        a, w = self.address, self.bus.write_byte_data
        w(a, PWR_MGMT_1, 0x80)  # сброс
        self.sleep(0.1)
        if self.has_gyro:
            w(a, PWR_MGMT_1, 0x01)  # проснуться; такт — PLL от гироскопа X (стабильнее внутреннего генератора)
            self.sleep(0.01)
            w(a, PWR_MGMT_2, 0x00)  # акселерометр и гироскоп X/Y/Z включены
        else:
            w(a, PWR_MGMT_1, 0x00)  # проснуться; внутренний генератор (гироскоп выключен — PLL от него не берём)
            self.sleep(0.01)
            w(a, PWR_MGMT_2, 0x07)  # гироскоп X/Y/Z — standby
        w(a, CONFIG, 0x02)  # DLPF_CFG = 2: акселерометр 94 Гц, гироскоп 98 Гц, внутренняя частота 1 кГц
        if self.who_am_i != GENUINE:
            w(a, ACCEL_CONFIG2, 0x02)  # MPU-6500 и клоны: фильтр акселерометра 92 Гц, 1 кГц
        w(a, SMPLRT_DIV, 4)  # 1 кГц / (1 + 4) = 200 Гц
        w(a, ACCEL_CONFIG, 0x00)  # ±2 g
        if self.has_gyro:
            w(a, GYRO_CONFIG, 0x00)  # ±250 °/с (фургон быстрее 60 °/с не поворачивает); у 6500 — и его DLPF
            w(a, FIFO_EN, 0x78)  # в FIFO — акселерометр и гироскоп X/Y/Z (12 байт на отсчёт)
        else:
            w(a, FIFO_EN, 0x08)  # в FIFO — только акселерометр (6 байт на отсчёт)
        w(a, INT_ENABLE, FIFO_OFLOW)  # флаг переполнения FIFO в INT_STATUS (вывод INT не подключён)
        self._held, self._gravity = None, None
        self.reset_fifo()

    def reset_fifo(self) -> None:
        self.bus.write_byte_data(self.address, USER_CTRL, 0x04)  # FIFO_RESET (действует при FIFO_EN = 0)
        self.bus.write_byte_data(self.address, USER_CTRL, 0x40)  # FIFO_EN

    def read(self) -> list[Vec]:
        """Всё, что накопилось в FIFO (≈ 10 отсчётов при чтении 20 раз в секунду) — акселерометр, g."""
        return self.read_imu()[0]

    def read_imu(self) -> tuple[list[Vec], list[Vec]]:
        """Пачка из FIFO: (акселерометр в g, гироскоп в °/с — пусто при gyro=False). Со сдвигом на одно чтение:
        отдаётся предыдущая пачка, когда эта прочиталась чисто; сбой — выбрасываются обе (конец предыдущей мог
        уже съехать), FIFO сбрасывается."""
        got = self._read_batch()
        if got is None:
            if self._held is not None:
                self.dropped += len(self._held[0])
            self._held = None
            return [], []
        if not got[0]:
            return [], []
        out, self._held = self._held, got
        return out if out is not None else ([], [])

    def _read_batch(self) -> tuple[list[Vec], list[Vec]] | None:
        """Всё, что накопилось в FIFO; None — сбой (переполнение, бессмыслица; FIFO уже сброшен)."""
        a = self.address
        status = self.bus.read_byte_data(a, INT_STATUS)
        hi, lo = self.bus.read_i2c_block_data(a, FIFO_COUNTH, 2)
        count = ((hi & 0xFF) << 8) | (lo & 0xFF)
        if status & FIFO_OFLOW or count >= FIFO_SIZE:
            self._overflow(count)
            return None
        n = count - count % self.frame
        if n == 0:
            return [], []
        raw = bytearray()
        while len(raw) < n:
            raw += bytes(b & 0xFF for b in self.bus.read_i2c_block_data(a, FIFO_R_W, min(CHUNK, n - len(raw))))
        if self.bus.read_byte_data(a, INT_STATUS) & FIFO_OFLOW:  # переполнился, пока читали: хвост съехал
            self._overflow(n)
            return None
        if self.has_gyro:
            samples, gyro = parse_fifo_imu(bytes(raw))
        else:
            samples, gyro = parse_fifo(bytes(raw)), []
        # FIFO сбился: на 6–10 байт — на месте ускорений угловые скорости (|a| не около 1 g), на 2–4 — оси
        # переставлены (тяжесть «повернулась»)
        if not plausible(samples) or turned(samples, self._gravity):
            self._bad += 1
            self.misaligned += 1
            self.dropped += len(samples)
            self.last_bad = raw[:2 * self.frame].hex()
            self.reset_fifo()
            if self._bad >= BAD_BATCHES_MAX:
                raise OSError("implausible readings (check the wiring)")
            return None
        self._bad = 0
        self._track_gravity(samples)
        return samples, gyro

    def _overflow(self, nbytes: int) -> None:
        """Переполнение FIFO: счёт и сброс."""
        self.overflows += 1
        self.dropped += nbytes // self.frame
        self.reset_fifo()

    def _track_gravity(self, samples: list[Vec]) -> None:
        m = median_vec(samples)
        g = self._gravity
        if g is None:
            self._gravity = m
            return
        k = 1.0 - math.exp(-len(samples) / self.rate_hz / GRAVITY_TAU_S)
        self._gravity = (g[0] + k * (m[0] - g[0]), g[1] + k * (m[1] - g[1]), g[2] + k * (m[2] - g[2]))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.bus.close()


def open_mpu6050(bus_no: int, address: int = ADDRESS, bus_opener: Callable[[int], I2CBus] = open_bus,
                 sleep: Callable[[float], None] = time.sleep, gyro: bool = True) -> Mpu6050:
    """Открыть шину, найти датчик и настроить. Нет датчика — OSError с понятным текстом (шина закрывается)."""
    bus = bus_opener(bus_no)
    dev = Mpu6050(bus, address, sleep, gyro)
    try:
        try:
            who = dev.probe()
        except OSError as e:
            raise OSError(f"MPU-6050: not found at 0x{address:02X} on I2C-{bus_no} ({e})") from e
        if who in (0x00, 0xFF):
            raise OSError(f"MPU-6050: no sensor at 0x{address:02X} on I2C-{bus_no} (WHO_AM_I 0x{who:02X})")
        dev.init()
    except BaseException:
        dev.close()
        raise
    return dev


# ───────────────────────── --mock ─────────────────────────


class FakeAccel:
    """--mock: «датчик» без железа — тяжесть с небольшим наклоном (без калибровки уровня: тангаж +1,2°,
    крен −0,6°) и шум; гироскоп — смещение нуля, шум и поворот yaw_dps (вправо > 0).
    inject_impact / inject_tilt — для /api/alarm/simulate (идут через настоящее обнаружение).
    Оси как у датчика, лежащего платой вверх стрелкой X вперёд: x — вперёд, y — влево, z — вверх."""

    model = "MPU-6050 (mock)"
    rate_hz = RATE_HZ
    has_gyro = True

    def __init__(self, address: int = ADDRESS, noise_g: float = 0.002, pitch_deg: float = 1.2,
                 roll_deg: float = -0.6, clock: Callable[[], float] = time.monotonic, seed: int | None = None,
                 gyro_bias: Vec = (0.4, -0.3, 0.2), noise_dps: float = 0.05):
        self.address = address
        self.noise_g = noise_g
        self.pitch_deg = pitch_deg
        self.roll_deg = roll_deg
        self.clock = clock
        self.gyro_bias = gyro_bias
        self.noise_dps = noise_dps
        self.yaw_dps = 0.0
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._last: float | None = None
        self._pending: list[Vec] = []  # добавки к ближайшим отсчётам (удар)

    def gravity(self) -> Vec:
        p, r = math.radians(self.pitch_deg), math.radians(self.roll_deg)
        # оси машины F, R, U → оси датчика: x = F, y = −R, z = U
        return (math.sin(p), -math.cos(p) * math.sin(r), math.cos(p) * math.cos(r))

    def read(self) -> list[Vec]:
        now = self.clock()
        with self._lock:
            if self._last is None or now - self._last > 1.0:  # первый раз / долго не читали — без «догоняния»
                self._last = now - 0.1
            n = int((now - self._last) * self.rate_hz + 1e-6)
            if n <= 0:
                return []
            self._last += n / self.rate_hz
            gx, gy, gz = self.gravity()
            out = []
            for _ in range(n):
                dx, dy, dz = self._pending.pop(0) if self._pending else (0.0, 0.0, 0.0)
                out.append((gx + dx + self._rng.gauss(0, self.noise_g), gy + dy + self._rng.gauss(0, self.noise_g),
                            gz + dz + self._rng.gauss(0, self.noise_g)))
            return out

    def read_imu(self) -> tuple[list[Vec], list[Vec]]:
        acc = self.read()
        bx, by, bz = self.gyro_bias
        n, r = self.noise_dps, self._rng
        # поворот вправо (курс растёт) — вращение вокруг оси «вниз»: у датчика z вверх, значит ωz < 0
        return acc, [(bx + r.gauss(0, n), by + r.gauss(0, n), bz - self.yaw_dps + r.gauss(0, n)) for _ in acc]

    def inject_impact(self, g: float) -> None:
        """Удар силой g: затухающее колебание вдоль x (пик |a − g| ≈ g)."""
        with self._lock:
            self._pending += [(g * k, 0.0, 0.0) for k in (1.0, -0.6, 0.35, -0.2, 0.1)]

    def inject_tilt(self, deg: float) -> None:
        """Машину наклонили на deg вбок (крен) — и так она и осталась."""
        with self._lock:
            self.roll_deg += deg

    def close(self) -> None: ...


# ───────────────────────── поток чтения ─────────────────────────


COUNTERS = ("overflows", "misaligned", "dropped")


class AccelReader(threading.Thread):
    """Поток: открыть датчик, читать раз в period_s, отдавать отсчёты: on_samples(акселерометр), а у датчика с
    гироскопом ещё on_gyro(акселерометр, гироскоп) — в этом же потоке. Нет датчика / ошибка I2C → on_state(None, текст),
    повтор раз в retry_s; заработал → on_state(источник, None). Сбои FIFO (счётчики источника) и опоздания чтения
    (пауза дольше LATE_S) — сводкой в лог не чаще STATS_EVERY_S."""

    def __init__(self, open_source: Callable[[], AccelSource], on_samples: Callable[[list[Vec]], None],
                 on_state: Callable[[AccelSource | None, str | None], None], period_s: float = 0.1,
                 retry_s: float = 10.0, on_gyro: Callable[[list[Vec], list[Vec]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(name="fnav-mpu6050", daemon=True)
        self.open_source = open_source
        self.on_samples = on_samples
        self.on_gyro = on_gyro
        self.on_state = on_state
        self.period_s = period_s
        self.retry_s = retry_s
        self.clock = clock
        self._stop_ev = threading.Event()
        self._reinit = threading.Event()
        # сводка: сбои FIFO за окно (у каждого открытия источника свои счётчики — копим разницу), опоздания
        self.counts = dict.fromkeys(COUNTERS, 0)
        self._seen = dict.fromkeys(COUNTERS, 0)
        self.late = 0
        self.gap_max = 0.0
        self._last_t0: float | None = None
        self._stats_t: float | None = None

    def note_read(self, src: object, t0: float) -> None:
        """После чтения: счётчики сбоев источника, пауза с прошлого чтения; пора — сводка в лог."""
        for k in COUNTERS:
            v = int(getattr(src, k, 0) or 0)
            self.counts[k] += max(0, v - self._seen[k])
            self._seen[k] = v
        if self._last_t0 is not None:
            gap = t0 - self._last_t0
            self.gap_max = max(self.gap_max, gap)
            if gap > LATE_S:
                self.late += 1
        self._last_t0 = t0
        if self._stats_t is None:
            self._stats_t = t0
        elif t0 - self._stats_t >= STATS_EVERY_S:
            self.flush_stats(src, t0)

    def flush_stats(self, src: object, now: float) -> None:
        c = self.counts
        if c["overflows"] or c["misaligned"] or self.late:
            bad = getattr(src, "last_bad", None) if c["misaligned"] else None
            log.warning("MPU-6050: last %.0f min: %d FIFO overflows, %d garbled reads, %d samples dropped; "
                        "%d reads late (> %.1f s apart), longest pause %.2f s%s",
                        (now - (self._stats_t or now)) / 60, c["overflows"], c["misaligned"], c["dropped"],
                        self.late, LATE_S, self.gap_max, f"; last garbled data {bad}" if bad else "")
        self.counts = dict.fromkeys(COUNTERS, 0)
        self.late, self.gap_max, self._stats_t = 0, 0.0, now

    def _opened(self) -> None:
        self._seen = dict.fromkeys(COUNTERS, 0)
        self._last_t0 = None

    def stop(self) -> None:
        self._stop_ev.set()

    def request_reinit(self) -> None:
        """Сменились шина/адрес — переоткрыть датчик."""
        self._reinit.set()

    @staticmethod
    def _text(e: BaseException) -> str:
        if isinstance(e, ImportError):
            return "MPU-6050: python module smbus2 is not installed"
        s = str(e) or type(e).__name__
        return s if s.startswith("MPU-6050") else f"MPU-6050: {s}"

    def run(self) -> None:
        src: AccelSource | None = None
        fails = 0
        while not self._stop_ev.is_set():
            if self._reinit.is_set():
                self._reinit.clear()
                if src is not None:
                    src.close()
                    src = None
            if src is None:
                try:
                    src = self.open_source()
                except Exception as e:  # noqa: BLE001 — OSError, нет /dev/i2c-N, нет smbus2
                    self.on_state(None, self._text(e))
                    self._stop_ev.wait(self.retry_s)
                    continue
                self._opened()
                self.on_state(src, None)
            t0 = self.clock()
            gyro: list[Vec] = []
            try:
                if getattr(src, "has_gyro", False):
                    samples, gyro = src.read_imu()  # type: ignore[attr-defined]
                else:
                    samples = src.read()
            except Exception as e:  # noqa: BLE001 — после сбоя посреди чтения FIFO не выровнен: открыть заново
                self.note_read(src, t0)
                src.close()
                src = None
                fails += 1
                if fails >= 3:
                    fails = 0
                    self.on_state(None, f"MPU-6050: not responding ({type(e).__name__}: {e})")
                    self._stop_ev.wait(self.retry_s)
                continue
            fails = 0
            self.note_read(src, t0)
            if samples:
                self.on_samples(samples)
                if self.on_gyro is not None and gyro and len(gyro) == len(samples):
                    self.on_gyro(samples, gyro)
            self._stop_ev.wait(max(0.0, self.period_s - (self.clock() - t0)))
        if src is not None:
            src.close()
