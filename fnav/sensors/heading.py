"""Курс: магнитометр → градусы (с компенсацией наклона), слияние компас/GPS/гироскоп, сглаживание, задний ход.
Чистые функции + маленькие классы состояния.

Соглашение осей «корпуса» (после таблицы осей в compass.py): x — вперёд (стрелка модуля), y — вправо,
z — вниз (FRD). Для горизонтального модуля курс = atan2(-y, x).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise


def norm360(deg: float) -> float:
    d = math.fmod(deg, 360.0)
    if d < 0:
        d += 360.0
    return 0.0 if d >= 360.0 else d


def angle_diff(a: float, b: float) -> float:
    """a − b в диапазоне (−180, 180]."""
    d = norm360(a - b)
    return d - 360.0 if d > 180.0 else d


def mag_heading_deg(x: float, y: float) -> float | None:
    """Магнитный курс горизонтального модуля по осям FRD (без компенсации наклона)."""
    if x == 0.0 and y == 0.0:
        return None
    return norm360(math.degrees(math.atan2(-y, x)))


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def tilt_mag_heading_deg(m: Sequence[float], up: Sequence[float]) -> float | None:
    """Магнитный курс оси x модуля с компенсацией наклона. m — поле в осях FRD модуля (z тоже исправлена: без
    смещения hard iron по z компенсация хуже, чем без неё), up — «вверх» в тех же осях (по акселерометру).
    Горизонт без углов: d = −up, восток E = d × m, север N = E × d, курс = atan2(E·x, N·x). Горизонтально
    (up = (0, 0, −1)) — то же, что mag_heading_deg."""
    n = math.sqrt(up[0] ** 2 + up[1] ** 2 + up[2] ** 2)
    if n < 1e-9:
        return None
    d = (-up[0] / n, -up[1] / n, -up[2] / n)
    e = _cross(d, m)
    if e[0] * e[0] + e[1] * e[1] + e[2] * e[2] < 1e-18:
        return None
    nn = _cross(e, d)
    return norm360(math.degrees(math.atan2(e[0], nn[0])))


def compass_up(acc_v: Sequence[float], phi_deg: float) -> tuple[float, float, float]:
    """«Вверх» в осях FRD модуля компаса по ускорению в осях машины (aF, aR, aU): модуль повёрнут относительно машины
    на phi по часовой (его x смотрит на курс машины + phi; phi = −(mount_offset_deg + A девиации)), его z — вниз."""
    af, ar, au = acc_v
    p = math.radians(phi_deg)
    c, s = math.cos(p), math.sin(p)
    return (af * c + ar * s, -af * s + ar * c, -au)


def deviation_deg(theta_deg: float, coeffs: list[float] | None) -> float:
    """Остаточная девиация δ(θ) = A + B·sinθ + C·cosθ + D·sin2θ + E·cos2θ (пусто = 0)."""
    if not coeffs:
        return 0.0
    k = list(coeffs) + [0.0] * (5 - len(coeffs))
    t = math.radians(theta_deg)
    return k[0] + k[1] * math.sin(t) + k[2] * math.cos(t) + k[3] * math.sin(2 * t) + k[4] * math.cos(2 * t)


def true_mag_heading(magnetic_deg: float | None, declination_deg: float, mount_offset_deg: float,
                     deviation: list[float] | None = None) -> float | None:
    """Магнитный курс θ → от истинного севера: θ + склонение (восточное +) + δ(θ) + поправка установки."""
    if magnetic_deg is None:
        return None
    return norm360(magnetic_deg + declination_deg + deviation_deg(magnetic_deg, deviation) + mount_offset_deg)


def gps_weight(speed_mps: float | None, gps_above_mps: float, blend_band_mps: float) -> float:
    """Вес GPS-курса 0..1: ниже (порог − band/2) — 0, выше (порог + band/2) — 1, между — smoothstep."""
    if speed_mps is None:
        return 0.0
    lo = gps_above_mps - blend_band_mps / 2
    hi = gps_above_mps + blend_band_mps / 2
    if hi <= lo:
        return 1.0 if speed_mps >= gps_above_mps else 0.0
    if speed_mps <= lo:
        return 0.0
    if speed_mps >= hi:
        return 1.0
    u = (speed_mps - lo) / (hi - lo)
    return u * u * (3 - 2 * u)


GPS_TRUST_MIN_MPS = 1.0  # с известной точностью скорости курс по GPS не раньше этой скорости
GPS_TRUST_EPS_K = 3.0  # и не раньше 3·eps: ошибка курса ≈ atan(eps / v) ≤ ~20°, а с гироскопом и подтяжкой — меньше


def gps_trust(speed_mps: float | None, eps_mps: float | None, gps_above_mps: float, blend_band_mps: float) -> float:
    """Доверие GPS-курсу 0..1. Известна точность скорости eps (gpsd TPV eps / u-blox NAV-PVT sAcc) — полностью
    выше max(1 м/с, 3·eps), переход — ниже на ширину зоны; нет — как раньше: gps_weight по настройкам."""
    if eps_mps is None or not math.isfinite(eps_mps) or eps_mps < 0:
        return gps_weight(speed_mps, gps_above_mps, blend_band_mps)
    hi = max(GPS_TRUST_MIN_MPS, GPS_TRUST_EPS_K * eps_mps)
    lo = max(0.0, hi - blend_band_mps)
    return gps_weight(speed_mps, (hi + lo) / 2, hi - lo)


def blend_angles(a_deg: float, b_deg: float, w_b: float) -> float:
    """Круговое смешивание через единичные векторы (без скачка 359→0)."""
    ax, ay = math.cos(math.radians(a_deg)), math.sin(math.radians(a_deg))
    bx, by = math.cos(math.radians(b_deg)), math.sin(math.radians(b_deg))
    x = ax * (1 - w_b) + bx * w_b
    y = ay * (1 - w_b) + by * w_b
    if abs(x) < 1e-12 and abs(y) < 1e-12:  # противоположные направления 50/50 — берём более весомое
        return b_deg if w_b >= 0.5 else a_deg
    return norm360(math.degrees(math.atan2(y, x)))


# минимальная скорость, при которой GPS-курс вообще осмыслен (если компаса нет)
GPS_ONLY_MIN_MPS = 1.0


def fuse(
    mag_deg: float | None,
    gps_deg: float | None,
    speed_mps: float | None,
    gps_above_mps: float,
    blend_band_mps: float,
    w: float | None = None,
) -> tuple[float | None, str]:
    """Мгновенный (несглаженный) курс и его источник: gps | mag | blend | none. w — готовое доверие GPS-курсу
    (gps_trust), иначе gps_weight по порогам."""
    if w is None:
        w = gps_weight(speed_mps, gps_above_mps, blend_band_mps)
    if gps_deg is None:
        w = 0.0
    if mag_deg is None:
        # компаса нет: GPS-курс, если хоть немного едем
        if gps_deg is not None and (speed_mps or 0.0) >= min(GPS_ONLY_MIN_MPS, gps_above_mps):
            return norm360(gps_deg), "gps"
        return None, "none"
    if w <= 0.0:
        return norm360(mag_deg), "mag"
    if w >= 1.0:
        return norm360(gps_deg), "gps"  # type: ignore[arg-type]
    return blend_angles(mag_deg, gps_deg, w), "blend"  # type: ignore[arg-type]


@dataclass
class CompassAnchor:
    """Привязка компаса к GPS-курсу.

    Пока едем и GPS-курс надёжен, запоминаем поправку offset = GPS − компас (круговое сглаживание с tau_s).
    На малой скорости и на стоянке курс = компас + offset: в момент остановки он равен последнему
    GPS-курсу (нет скачка, даже если компас ещё не откалиброван), а дальше компас отрабатывает только
    повороты машины."""

    tau_s: float = 5.0
    x: float | None = None
    y: float | None = None

    def reset(self) -> None:
        self.x = self.y = None

    @property
    def offset_deg(self) -> float | None:
        if self.x is None or self.y is None or (abs(self.x) < 1e-12 and abs(self.y) < 1e-12):
            return None
        return angle_diff(math.degrees(math.atan2(self.y, self.x)), 0.0)

    def learn(self, mag_deg: float, gps_deg: float, dt: float) -> None:
        d = math.radians(angle_diff(gps_deg, mag_deg))
        tx, ty = math.cos(d), math.sin(d)
        if self.x is None or self.y is None or self.tau_s <= 0:
            self.x, self.y = tx, ty
            return
        alpha = 1.0 - math.exp(-max(0.0, dt) / self.tau_s)
        self.x += (tx - self.x) * alpha
        self.y += (ty - self.y) * alpha
        n = math.hypot(self.x, self.y)
        if n > 1e-9:
            self.x, self.y = self.x / n, self.y / n

    def apply(self, mag_deg: float | None) -> float | None:
        off = self.offset_deg
        if mag_deg is None or off is None:
            return mag_deg
        return norm360(mag_deg + off)


@dataclass
class HeadingSmoother:
    """Экспоненциальное сглаживание направления (вектором) с постоянной времени tau, с.

    update(None, dt) — удержание последнего значения (машина стоит, компаса нет)."""

    tau_s: float = 0.5
    x: float | None = None
    y: float | None = None

    def reset(self) -> None:
        self.x = self.y = None

    def set(self, deg: float) -> None:
        """Встать на значение (переход с курса по гироскопу — без скачка)."""
        self.x, self.y = math.cos(math.radians(deg)), math.sin(math.radians(deg))

    @property
    def value(self) -> float | None:
        if self.x is None or self.y is None or (abs(self.x) < 1e-12 and abs(self.y) < 1e-12):
            return None
        return norm360(math.degrees(math.atan2(self.y, self.x)))

    def update(self, deg: float | None, dt: float) -> float | None:
        if deg is None:
            return self.value
        tx, ty = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        if self.x is None or self.y is None or self.tau_s <= 0:
            self.x, self.y = tx, ty
            return self.value
        alpha = 1.0 - math.exp(-max(0.0, dt) / self.tau_s)
        self.x += (tx - self.x) * alpha
        self.y += (ty - self.y) * alpha
        n = math.hypot(self.x, self.y)
        if n > 1e-9:
            self.x, self.y = self.x / n, self.y / n
        else:  # ровно противоположное — прыгаем к новому
            self.x, self.y = tx, ty
        return self.value


class TpvTrack:
    """Последние сырые TPV (без фильтра «стоим»): скорость поворота GPS-курса (поправку компаса учим только на прямой)
    и продольное ускорение по скорости (поправка наклона на разгоне и торможении)."""

    def __init__(self, window_s: float = 1.2) -> None:
        self.window_s = window_s
        self._h: deque[tuple[float, float, float | None]] = deque()

    def clear(self) -> None:
        self._h.clear()

    def add(self, t: float, speed: float, course: float | None) -> None:
        if self._h and t <= self._h[-1][0]:
            return
        if self._h and t - self._h[-1][0] > 1.0:  # разрыв в данных
            self._h.clear()
        self._h.append((t, speed, course))
        while t - self._h[0][0] > self.window_s:
            self._h.popleft()

    @staticmethod
    def _slope(ts: list[float], ys: list[float]) -> float:
        n = len(ts)
        mt, my = sum(ts) / n, sum(ys) / n
        sxx = sum((t - mt) ** 2 for t in ts)
        return 0.0 if sxx <= 0 else sum((t - mt) * (y - my) for t, y in zip(ts, ys, strict=True)) / sxx

    def course_rate(self, min_span_s: float = 0.6) -> float | None:
        """°/с (регрессия по окну, через 0/360); точек мало — None."""
        pts = [(t, c) for t, _, c in self._h if c is not None]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < min_span_s:
            return None
        ys = [pts[0][1]]
        for (_, a), (_, b) in pairwise(pts):
            ys.append(ys[-1] + angle_diff(b, a))
        return self._slope([t for t, _ in pts], ys)

    def accel(self, min_span_s: float = 0.6) -> float | None:
        """dv/dt, м/с²; точек мало — None."""
        h = self._h
        if len(h) < 3 or h[-1][0] - h[0][0] < min_span_s:
            return None
        return self._slope([e[0] for e in h], [e[1] for e in h])


# Курс по гироскопу (комплементарный фильтр): между обновлениями курс += рысканье·dt, подтяжка к ориентирам.
GYRO_GPS_TAU_S = 1.0  # к GPS-курсу, пока ему доверяем (быстрые повороты держит гироскоп, а не GPS с его задержкой)
GYRO_MAG_TAU_S = 20.0  # к компасу на малой скорости и без GPS (паркинг, тоннель): железо рядом сбивает его надолго,
# а гироскоп после стоянки уходит на ~0,05 °/с — за 20 с это 1°
GYRO_MAG_STILL_TAU_S = 120.0  # на стоянке курс не меняется: компас (инвертор, соседняя машина) — едва-едва
GYRO_MAG_FAR_DEG = 45.0  # компас дальше этого от курса на ходу — подозрительно: подтяжка вчетверо медленнее
GYRO_MAG_JUMP_DEG = 6.0  # компас ушёл от гироскопа на столько за окно — помеха (грузовик, трамвай): не слушаем
GYRO_MAG_JUMP_WINDOW_S = 0.5
GYRO_MAG_HOLD_S = 3.0
GYRO_BIAS_KI = 0.02  # 1/с²: остаток смещения гироскопа по GPS-курсу на прямой
GYRO_BIAS_MAX_DPS = 2.0
GYRO_STRAIGHT_DPS = 3.0
GYRO_REF_AGREE_DEG = 20.0  # сверенный компас ближе этого — курс тоже сверен
GPS_LATENCY_S = 0.15  # GPS-курс относится к эпохе ~0,15 с до приёма TPV (решение M8N + UART + gpsd): сравниваем с
# курсом по гироскопу того момента, иначе на повороте 15 °/с курс тянется назад на ~2–4°


@dataclass
class GyroHeading:
    """Курс по гироскопу с подтяжкой к GPS-курсу (пока ему доверяем) и к компасу (медленно; на стоянке — едва-едва;
    скачки компаса, которых не видит гироскоп, — помеха, пропускаем). referenced — курс сверен с GPS или со сверенным
    компасом (для заднего хода: только по такому курсу можно сказать, что GPS-скорость смотрит назад)."""

    psi: float | None = None
    referenced: bool = False
    bias_dps: float = 0.0  # остаток смещения по рысканью (вычитается), °/с
    source: str = "none"
    _e_hist: deque = field(default_factory=deque)
    _psi_hist: deque = field(default_factory=deque)  # (t, курс) за последнюю секунду — для запаздывающего GPS
    _hold_until: float = -math.inf

    def reset(self) -> None:
        self.psi, self.referenced, self.source = None, False, "none"
        self._e_hist.clear()
        self._psi_hist.clear()
        self._hold_until = -math.inf

    def init(self, deg: float, referenced: bool, source: str = "mag") -> None:
        self.psi, self.referenced, self.source = norm360(deg), referenced, source
        self._e_hist.clear()
        self._psi_hist.clear()

    def _psi_at(self, t: float, psi: float) -> float:
        """Курс в момент t (из истории; раньше истории — самый старый), psi — нынешний."""
        for tt, pp in reversed(self._psi_hist):
            if tt <= t:
                return pp
        return self._psi_hist[0][1] if self._psi_hist else psi

    def update(self, now: float, dt: float, d_gyro: float, rate_dps: float | None, mag: float | None, mag_ok: bool,
               gps: float | None, gps_w: float, still: bool, gps_lag_s: float = 0.0) -> float | None:
        """d_gyro — поворот по гироскопу за шаг, °; mag — компас (с поправкой привязки), mag_ok — компас сверен;
        gps_w — доверие GPS-курсу (0 — не брать: задний ход, курс напротив); gps_lag_s — к какому моменту назад
        относится GPS-курс (поправка — по разнице с курсом того момента)."""
        wg = gps_w if gps is not None else 0.0
        if self.psi is None:
            if gps is not None and wg >= 1.0:
                self.init(gps, True, "gps")
            elif mag is not None:
                self.init(mag, mag_ok, "mag")
            return self.psi
        psi = norm360(self.psi + d_gyro - self.bias_dps * dt)
        if wg > 0.0:
            then = self._psi_at(now - gps_lag_s, psi) if gps_lag_s > 0 else psi
            e = angle_diff(gps, then)  # type: ignore[arg-type]
            psi = norm360(psi + e * min(1.0, wg * dt / GYRO_GPS_TAU_S))
            if wg >= 1.0:
                self.referenced = True
                if rate_dps is not None and abs(rate_dps) < GYRO_STRAIGHT_DPS:
                    b = self.bias_dps - GYRO_BIAS_KI * e * dt
                    self.bias_dps = max(-GYRO_BIAS_MAX_DPS, min(GYRO_BIAS_MAX_DPS, b))
        if mag is not None and wg < 1.0:
            e = angle_diff(mag, psi)
            h = self._e_hist
            h.append((now, e))
            while now - h[0][0] > GYRO_MAG_JUMP_WINDOW_S:
                h.popleft()
            if now - h[0][0] >= GYRO_MAG_JUMP_WINDOW_S * 0.8 and abs(angle_diff(e, h[0][1])) > GYRO_MAG_JUMP_DEG:
                self._hold_until = now + GYRO_MAG_HOLD_S
            if now >= self._hold_until:
                tau = GYRO_MAG_STILL_TAU_S if still else GYRO_MAG_TAU_S
                if not still and abs(e) > GYRO_MAG_FAR_DEG:
                    tau *= 4
                psi = norm360(psi + e * min(1.0, (1.0 - wg) * dt / tau))
                if mag_ok and abs(e) <= GYRO_REF_AGREE_DEG:
                    self.referenced = True
        else:
            self._e_hist.clear()
        self.psi = psi
        ph = self._psi_hist
        ph.append((now, psi))
        while now - ph[0][0] > 1.0:
            ph.popleft()
        if wg >= 1.0:
            self.source = "gps"
        elif wg > 0.0:
            self.source = "blend" if mag is not None else "gps"
        elif mag is not None:
            self.source = "mag"
        return psi


# Задний ход: скорость по GPS смотрит против курса машины (компас/гироскоп), подтверждение — трогание назад по
# акселерометру. Курс GPS + 180° — только проверка, курс машины по нему не считается.
REV_MIN_MPS = 0.4  # медленнее направление скорости не определить (и Static Hold M8N держит 0 до 0,5 м/с)
REV_STOP_MPS = 0.3  # стоим
REV_MAX_MPS = 5.0  # быстрее фургон задом не ездит: выше — вперёд, что бы ни показывал курс
REV_BACK_DEG = 110.0  # |GPS-курс − курс машины| ≥ этого — едем назад
REV_FWD_DEG = 70.0  # ≤ этого — вперёд; между — неясно (боковой снос антенны в крутом повороте)
REV_ON_ACC_S = 0.4  # «назад» столько подряд, если и акселерометр видел трогание назад
REV_ON_GYRO_S = 1.0  # без акселерометра, курс по гироскопу
REV_ON_MAG_S = 1.5  # курс только по компасу (без датчика движения): помехи компаса — дольше
REV_OFF_S = 0.5  # «вперёд» или «стоим» столько — задний ход кончился
REV_ACC_MPS = 0.25  # продольная «скорость» по акселерометру: ≤ −этого — трогались назад, ≥ — вперёд
REV_LEVER_M = 2.0  # плечо антенны от оси поворота: в крутом повороте на малой скорости курс GPS ≠ курс машины
REV_LEVER_K = 0.7  # снос ω·плечо больше этой доли скорости — направление неясно
REV_IMU_STILL_S = 2.0  # датчик движения неподвижен столько — машина стоит, сколько бы ни «ехал» GPS (не включаем)


@dataclass
class ReverseDetector:
    reversing: bool = False
    _back_since: float | None = None
    _off_since: float | None = None

    def reset(self) -> None:
        self.reversing, self._back_since, self._off_since = False, None, None

    def update(self, now: float, speed: float | None, course: float | None, eps: float | None,
               heading: float | None, confirmed: bool, gyro: bool, rate_dps: float | None,
               v_long: float | None, imu_still_s: float | None) -> bool:
        """speed, course — сырой TPV, свежий (иначе None); heading — курс машины, не подтянутый к этому GPS-курсу;
        confirmed — курс сверен (иначе по GPS не судим); gyro — курс держит гироскоп; rate_dps — рысканье;
        v_long — продольная «скорость» по акселерометру (None — нет калибровки «вперёд»); imu_still_s — сколько
        секунд датчик движения неподвижен (None — датчика нет)."""
        moving = speed is not None and speed >= max(REV_MIN_MPS, 2.0 * eps if eps else 0.0)
        stopped = speed is None or speed < REV_STOP_MPS
        too_fast = speed is not None and speed > REV_MAX_MPS
        ev = None
        if moving and not too_fast and course is not None and heading is not None and confirmed:
            lever = abs(math.radians(rate_dps)) * REV_LEVER_M if rate_dps is not None else 0.0
            if lever <= REV_LEVER_K * speed:  # type: ignore[operator]
                d = abs(angle_diff(course, heading))
                ev = "back" if d >= REV_BACK_DEG else ("fwd" if d <= REV_FWD_DEG else None)
        acc = None
        if v_long is not None:
            acc = "back" if v_long <= -REV_ACC_MPS else ("fwd" if v_long >= REV_ACC_MPS else None)
        imu_still = imu_still_s is not None and imu_still_s >= REV_IMU_STILL_S
        if not self.reversing:
            if ev == "fwd" or not moving or too_fast or acc == "fwd" or imu_still:
                self._back_since = None
            elif ev == "back":
                if self._back_since is None:
                    self._back_since = now
                need = REV_ON_ACC_S if acc == "back" else (REV_ON_GYRO_S if gyro else REV_ON_MAG_S)
                if now - self._back_since >= need - 1e-9:
                    self.reversing, self._off_since = True, None
        elif too_fast:
            self.reset()
        elif stopped or ev == "fwd":  # датчик «неподвижен» не снимает: ровный ход без тряски для него тоже покой
            if self._off_since is None:
                self._off_since = now
            if now - self._off_since >= REV_OFF_S - 1e-9:
                self.reset()
        else:
            self._off_since = None
        return self.reversing
