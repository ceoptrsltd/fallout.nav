"""Один поток чтения MPU-6050 на всех потребителей: охрана (fnav/core/alarm.py), уровень (fnav/core/level.py) и курс
(гироскоп — fnav/sensors/hub.py attach_imu).

Датчик открывается один раз (шина и адрес — settings.alarm.sensor_bus / sensor_address). Потребители подписываются
subscribe(cb): cb(samples, dt) вызывается в потоке чтения ~20 раз в секунду с пачкой отсчётов (g, оси датчика) —
считать там же и быстро, в event loop отдавать только готовое. subscribe_motion(cb): cb(samples, gyro, dt) — те же
отсчёты и угловые скорости (°/с, оси датчика) той же пачки; только у датчика с гироскопом.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fnav.models import AlarmSettings
from fnav.sensors.mpu6050 import RATE_HZ, AccelReader, AccelSource, FakeAccel, Vec, open_mpu6050

log = logging.getLogger("fnav.core.imu")

Consumer = Callable[[list[Vec], float], None]
MotionConsumer = Callable[[list[Vec], list[Vec], float], None]
SourceOpener = Callable[[int, int], AccelSource]
BUS_FALLBACK = (3, 1)  # где ещё искать датчик: своя шина (pins 7/29) и шина компаса (pins 3/5)
READ_PERIOD_S = 0.05  # FIFO с гироскопом (12 байт на отсчёт) вмещает ~0,4 с — читаем почаще, с запасом на задержки


class ImuService:
    def __init__(self, get_settings: Callable[[], AlarmSettings], mock: bool = False,
                 open_source: SourceOpener | None = None, retry_s: float = 10.0) -> None:
        self.get_settings = get_settings
        self.fake: FakeAccel | None = None
        if open_source is None:
            if mock:
                self.fake = FakeAccel()
                open_source = self._open_fake
            else:
                open_source = open_mpu6050
        self._open = open_source
        self.retry_s = retry_s
        s = get_settings()
        self._cfg = (s.sensor_bus, s.sensor_address)
        self.address = s.sensor_address
        self.bus: int | None = None  # шина, на которой датчик нашёлся
        self.rate_hz = RATE_HZ
        self.ok = False
        self.gyro = False  # у датчика есть гироскоп (отсчёты идут и в subscribe_motion)
        self.model: str | None = None
        self.error: str | None = None
        self._consumers: list[Consumer] = []
        self._motion: list[MotionConsumer] = []
        self._reader: AccelReader | None = None

    def _open_fake(self, _bus: int, address: int) -> AccelSource:
        assert self.fake is not None
        self.fake.address = address
        return self.fake

    @property
    def running(self) -> bool:
        return self._reader is not None and self._reader.is_alive()

    def subscribe(self, cb: Consumer) -> None:
        self._consumers.append(cb)

    def subscribe_motion(self, cb: MotionConsumer) -> None:
        self._motion.append(cb)

    def start(self) -> None:
        if self._reader is not None:
            return
        self._reader = AccelReader(self._open_any, self.feed, self._on_state, period_s=READ_PERIOD_S,
                                   retry_s=self.retry_s, on_gyro=self.feed_gyro)
        self._reader.start()

    def _open_any(self) -> AccelSource:
        """Датчик — на своей шине (sensor_bus, по умолчанию I2C-3: pins 7/29), иначе — на шинах из BUS_FALLBACK
        (I2C-1, pins 3/5, вместе с компасом): подключить можно к любой, ищем сами."""
        bus, address = self._cfg
        errors: list[str] = []
        for b in dict.fromkeys((bus, *BUS_FALLBACK)):
            try:
                src = self._open(b, address)
            except OSError as e:
                errors.append(str(e) if b == bus else f"I2C-{b}: {e.strerror or e}")
                continue
            self.bus = b
            return src
        self.bus = None
        raise OSError("; ".join(errors))

    async def stop(self) -> None:
        r, self._reader = self._reader, None
        if r is not None:
            r.stop()
            await asyncio.to_thread(r.join, 3.0)
        self.ok = False

    def apply_settings(self, s: AlarmSettings) -> None:
        cfg = (s.sensor_bus, s.sensor_address)
        if cfg == self._cfg:
            return
        self._cfg = cfg
        self.address = s.sensor_address
        log.info("imu: sensor moved to 0x%02X on I2C-%d", s.sensor_address, s.sensor_bus)
        if self._reader is not None:
            self._reader.request_reinit()

    # ── из потока чтения ──
    def _on_state(self, src: AccelSource | None, error: str | None) -> None:
        was = (self.ok, self.error)
        if src is not None:
            self.ok, self.model, self.address, self.error = True, src.model, src.address, None
            self.rate_hz = src.rate_hz
            self.gyro = bool(getattr(src, "has_gyro", False))
        else:
            self.ok, self.gyro, self.error = False, False, error
        if (self.ok, self.error) != was:
            if self.ok:
                log.info("imu: %s at 0x%02X", self.model, self.address)
            else:
                log.warning("imu: %s (retrying every %.0f s)", error, self.retry_s)

    def feed(self, samples: list[Vec]) -> None:
        """Пачка отсчётов → всем потребителям (поток чтения; тесты зовут напрямую)."""
        dt = 1.0 / self.rate_hz
        for cb in self._consumers:
            try:
                cb(samples, dt)
            except Exception:
                log.exception("imu consumer")

    def feed_gyro(self, samples: list[Vec], gyro: list[Vec]) -> None:
        """Та же пачка с угловыми скоростями (°/с) → потребителям движения (поток чтения; тесты зовут напрямую)."""
        dt = 1.0 / self.rate_hz
        for cb in self._motion:
            try:
                cb(samples, gyro, dt)
            except Exception:
                log.exception("imu motion consumer")
