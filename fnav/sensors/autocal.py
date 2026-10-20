"""Автокалибровка компаса по GPS-курсу во время езды.

Точки: (GPS-курс, сырое поле компаса в осях FRD, склонение), только при «чистых» условиях:
fix 3d, скорость ≥ auto_min_speed_mps, hdop ≤ auto_max_hdop, почти прямолинейное движение
(наклон регрессии GPS-курса за ~3 с ≤ auto_max_turn_rate_dps), свежие замеры компаса, синхронизированные
с GPS-курсом с учётом задержки GPS, без выбросов |B| и без тряски. Не чаще 1 точки/с, не больше SECTOR_CAP
точек на 10°-сектор GPS-курса (FIFO — старые точки вытесняются новыми: калибровка адаптируется к переносу
модуля).

Подгонка:
  1) hard/soft iron — эллипс по сырым (x, y) (calibration.fit_calibration(force="2d"));
  2) девиация относительно GPS — робастный МНК δ(θ) = A + B·sinθ + C·cosθ + D·sin2θ + E·cos2θ,
     δ_obs = wrap(gps − θ − склонение − mount_offset_deg) (так итоговая формула курса
     θ + склонение + δ(θ) + mount_offset_deg воспроизводит GPS; A поглощает ошибку установки).
Решение «применить/сохранить» — сравнением СКО курса относительно GPS на одних и тех же точках.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any

import numpy as np

from fnav.models import AutoCalStatus, CompassCalibration, CompassSettings, GpsFix
from fnav.sensors.calibration import CalibrationError, fit_calibration

SECTORS = 36
SECTOR_CAP = 30  # точек на сектор (FIFO)
MIN_PER_SECTOR = 2  # сектор «покрыт», если в нём столько точек
MIN_POINTS = 40
GPS_LATENCY_S = 0.2  # GPS-курс относится к моменту ~0.2 с раньше получения TPV
SYNC_HALF_S = 0.2  # окно усреднения компаса вокруг момента GPS-эпохи
TURN_WINDOW_S = 3.0
TURN_MIN_SPAN_S = 2.0
MAX_COURSE_RESID_DEG = 3.0  # разброс курса вокруг прямой в окне (S-образные манёвры)
MIN_POINT_INTERVAL_S = 1.0
B_TOL = 0.10  # |B| в пределах ±10 % от медианы точек того же курса
MAX_AXIS_STD_UT = 3.0  # тряска/помехи в окне синхронизации
MAX_APPLY_RMS_DEG = 8.0
APPLY_MARGIN_DEG = 0.2
STATE_VERSION = 1


def wrap180(a: np.ndarray | float) -> np.ndarray | float:
    return (np.asarray(a) + 180.0) % 360.0 - 180.0 if isinstance(a, np.ndarray) else (a + 180.0) % 360.0 - 180.0


def headings_after(cal: CompassCalibration, xyz: np.ndarray) -> np.ndarray:
    """Магнитный курс θ (°, 0..360) после offset/matrix для массива сырых точек FRD."""
    off = np.asarray(cal.offset, dtype=float)
    m = np.asarray(cal.matrix, dtype=float)
    c = (xyz - off) @ m.T
    return np.degrees(np.arctan2(-c[:, 1], c[:, 0])) % 360.0


def deviation_array(theta_deg: np.ndarray, coeffs: list[float]) -> np.ndarray:
    if not coeffs:
        return np.zeros_like(theta_deg)
    k = list(coeffs) + [0.0] * (5 - len(coeffs))
    t = np.radians(theta_deg)
    return k[0] + k[1] * np.sin(t) + k[2] * np.cos(t) + k[3] * np.sin(2 * t) + k[4] * np.cos(2 * t)


def predict_true(cal: CompassCalibration, xyz: np.ndarray, decl: np.ndarray, mount: float) -> np.ndarray:
    th = headings_after(cal, xyz)
    return (th + decl + deviation_array(th, cal.deviation) + mount) % 360.0


def trimmed_rms(err: np.ndarray, trim: float = 0.05) -> float:
    if len(err) == 0:
        return float("nan")
    a = np.sort(np.abs(err))
    keep = max(1, math.ceil(len(a) * (1 - trim)))
    return float(np.sqrt(np.mean(a[:keep] ** 2)))


def _basis(theta_deg: np.ndarray, nterms: int) -> np.ndarray:
    t = np.radians(theta_deg)
    cols = [np.ones_like(t), np.sin(t), np.cos(t), np.sin(2 * t), np.cos(2 * t)]
    return np.column_stack(cols[:nterms])


def fit_deviation(theta: np.ndarray, delta_obs: np.ndarray, nterms: int = 5) -> tuple[list[float], float, np.ndarray]:
    """Робастный МНК гармоник: (коэффициенты [A..E], СКО остатков по inliers, маска inliers)."""
    # центрирование по круговому среднему — устойчиво к δ около ±180°
    m = math.degrees(math.atan2(np.mean(np.sin(np.radians(delta_obs))), np.mean(np.cos(np.radians(delta_obs)))))
    y = wrap180(delta_obs - m)
    X = _basis(theta, nterms)
    inl = np.ones(len(y), dtype=bool)
    beta = np.zeros(nterms)
    for _ in range(3):
        beta, *_ = np.linalg.lstsq(X[inl], y[inl], rcond=None)
        r = y - X @ beta
        sigma = 1.4826 * float(np.median(np.abs(r[inl] - np.median(r[inl]))))
        new = np.abs(r) <= max(3.0 * sigma, 1.0)
        if new.sum() < max(10, nterms * 3) or np.array_equal(new, inl):
            break
        inl = new
    r = y - X @ beta
    rms = float(np.sqrt(np.mean(r[inl] ** 2)))
    coeffs = [float(wrap180(beta[0] + m))] + [float(b) for b in beta[1:]] + [0.0] * (5 - nterms)
    return coeffs, rms, inl


class AutoCalibrator:
    """Состояние автокалибровки. on_gps() — из event loop, add_mag() — из потока компаса, fit() — из потока."""

    def __init__(self, cfg: CompassSettings, wall=time.time):
        self.cfg = cfg
        self.wall = wall
        self._lock = threading.Lock()
        self._mag: deque[tuple[float, float, float, float]] = deque(maxlen=400)
        self._hist: deque[tuple[float, float, float, float | None, bool]] = deque()
        self._sec: list[deque[tuple[float, float, float, float, float, float]]] = [
            deque(maxlen=SECTOR_CAP) for _ in range(SECTORS)
        ]
        self._last_point_t = -math.inf
        self.rejects: Counter[str] = Counter()
        self.last_reject = ""
        self.new_points = 0
        self.dirty = False
        self.candidate: CompassCalibration | None = None
        self.fit_info: dict[str, Any] = {}
        self.message = ""
        self.last_saved_iso: str | None = None
        self.last_saved_wall: float | None = None
        self.applied = False

    # ── входные данные ──
    def add_mag(self, t: float, v: tuple[float, float, float]) -> None:
        self._mag.append((t, float(v[0]), float(v[1]), float(v[2])))

    def reset(self) -> None:
        with self._lock:
            for d in self._sec:
                d.clear()
            self._hist.clear()
            self.candidate = None
            self.fit_info = {}
            self.new_points = 0
            self.dirty = True
            self.rejects.clear()
            self.last_reject = ""
            self._last_point_t = -math.inf

    def drop_before(self, wall_t: float) -> int:
        """Удалить точки старше момента wall_t (например, после ручной калибровки — модуль могли переставить)."""
        n = 0
        with self._lock:
            for i, d in enumerate(self._sec):
                keep = [p for p in d if p[0] >= wall_t]
                n += len(d) - len(keep)
                self._sec[i] = deque(keep, maxlen=SECTOR_CAP)
            if n:
                self.candidate = None
                self.dirty = True
        return n

    def _reject(self, why: str) -> str:
        self.rejects[why] += 1
        self.last_reject = why
        return why

    def on_gps(self, t: float, fix: GpsFix, decl: float | None) -> str | None:
        """Обработать TPV (t — monotonic момента приёма). None = точка принята, иначе причина отказа.

        Точка берётся из СЕРЕДИНЫ окна ~3 с, и только если всё окно — прямая (до и после точки):
        так начало поворота, замеченное с запаздыванием, в выборку не попадает. Курс точки — значение
        линейной регрессии в середине окна (подавляет шум GPS-курса)."""
        c = self.cfg
        course = fix.course_deg
        speed = fix.speed_mps or 0.0
        h = self._hist
        if fix.fix == "none" or course is None or speed < 1.5:
            h.clear()
            return self._reject("stopped / no fix")
        if h and t - h[-1][0] > 1.5:  # разрыв в данных
            h.clear()
        h.append((t, float(course), float(speed), fix.hdop, fix.fix == "3d"))
        while h and t - h[0][0] > TURN_WINDOW_S:
            h.popleft()
        span = h[-1][0] - h[0][0]
        if len(h) < 3 or span < TURN_MIN_SPAN_S:
            return "warming up"
        if not all(e[4] for e in h):
            return self._reject("no 3D fix")
        if min(e[2] for e in h) < c.auto_min_speed_mps:
            return self._reject("too slow")
        hd = [e[3] for e in h if e[3] is not None]
        if hd and max(hd) > c.auto_max_hdop:
            return self._reject("poor HDOP")
        ts = np.array([e[0] for e in h])
        cs = np.array([e[1] for e in h])
        u = cs[0] + np.concatenate([[0.0], np.cumsum(wrap180(np.diff(cs)))])  # развёртка через 0/360
        slope, icpt = np.polyfit(ts - ts[0], u, 1)
        resid = u - (slope * (ts - ts[0]) + icpt)
        if abs(slope) > c.auto_max_turn_rate_dps or float(np.max(np.abs(resid))) > MAX_COURSE_RESID_DEG:
            return self._reject("turning")
        # обе половины окна тоже прямые (хвост поворота на краю окна мало влияет на общий наклон)
        mid = (ts[0] + ts[-1]) / 2
        for part in (ts <= mid, ts >= mid):
            if part.sum() >= 3 and abs(np.polyfit(ts[part] - ts[0], u[part], 1)[0]) > 1.5 * c.auto_max_turn_rate_dps:
                return self._reject("turning")
        if decl is None:
            return self._reject("declination unknown")
        tc = (ts[0] + ts[-1]) / 2  # середина окна
        if tc - self._last_point_t < MIN_POINT_INTERVAL_S:
            return "rate limit"  # не ошибка — прореживание
        course_c = float((slope * (tc - ts[0]) + icpt) % 360.0)
        te = tc - GPS_LATENCY_S  # GPS-эпоха, к которой относится курс
        win = [m for m in list(self._mag) if abs(m[0] - te) <= SYNC_HALF_S]
        if len(win) < 2:
            return self._reject("no compass data")
        arr = np.array([m[1:] for m in win])
        if float(np.max(arr.std(axis=0))) > MAX_AXIS_STD_UT:
            return self._reject("compass unsteady")
        v = arr.mean(axis=0)
        sec = int(course_c // (360 / SECTORS)) % SECTORS
        # |B| сравниваем с точками того же курса: из-за hard iron сырой |B| сильно зависит от курса
        bmag = float(np.linalg.norm(v))
        bs = self._b_median(sec)
        if bs is not None and abs(bmag - bs) > B_TOL * bs:
            return self._reject("|B| outlier")
        wall_c = self.wall() - (t - tc)
        with self._lock:
            self._sec[sec].append((wall_c, course_c, float(v[0]), float(v[1]), float(v[2]), float(decl)))
            self.new_points += 1
            self.dirty = True
        self._last_point_t = tc
        return None

    def _b_median(self, sec: int) -> float | None:
        with self._lock:
            near = [p for k in (sec - 1, sec, sec + 1) for p in self._sec[k % SECTORS]]
        if len(near) < 5:
            return None
        return float(np.median([math.sqrt(p[2] ** 2 + p[3] ** 2 + p[4] ** 2) for p in near]))

    # ── точки и покрытие ──
    def points(self) -> np.ndarray:
        with self._lock:
            rows = [p for d in self._sec for p in d]
        return np.array(rows, dtype=float).reshape(-1, 6)

    def sector_counts(self) -> list[int]:
        with self._lock:
            return [len(d) for d in self._sec]

    def coverage_pct(self) -> float:
        cnt = self.sector_counts()
        return 100.0 * sum(1 for n in cnt if n >= MIN_PER_SECTOR) / SECTORS

    # ── подгонка ──
    def fit(self, mount_offset_deg: float) -> CompassCalibration | None:
        P = self.points()
        self.new_points = 0
        cov = self.coverage_pct()
        if len(P) < MIN_POINTS or cov < self.cfg.auto_min_coverage_pct:
            self.message = (f"Collecting: drive straight at >= {self.cfg.auto_min_speed_mps:g} m/s in different "
                            f"directions (coverage {cov:.0f}% of {self.cfg.auto_min_coverage_pct:.0f}% needed)")
            return None
        gps, xyz, decl = P[:, 1], P[:, 2:5], P[:, 5]
        try:
            stage1, info1 = fit_calibration(xyz, force="2d")
        except CalibrationError as e:
            self.message = f"Hard/soft-iron fit failed: {e}"
            return None
        theta = headings_after(stage1, xyz)
        d_obs = wrap180(gps - theta - decl - mount_offset_deg)
        nterms = 5 if cov >= 75 else 3  # вторая гармоника плохо обусловлена при неполном покрытии
        coeffs, rms, inl = fit_deviation(theta, d_obs, nterms)
        # проверка «зеркальных» осей: курс крутится в обратную сторону
        mirror = wrap180(gps - decl - mount_offset_deg + theta)
        mm = math.degrees(math.atan2(np.mean(np.sin(np.radians(mirror))), np.mean(np.cos(np.radians(mirror)))))
        mirror_rms = trimmed_rms(wrap180(mirror - mm))
        quality = max(0.0, min(1.0, 1.0 - rms / 10.0)) * max(0.0, min(1.0, cov / 80.0))
        cal = CompassCalibration(
            offset=stage1.offset,
            matrix=stage1.matrix,
            fitted_at=datetime.fromtimestamp(self.wall(), UTC).isoformat(timespec="seconds"),
            quality=round(quality, 3),
            deviation=[round(x, 3) for x in coeffs],
            source="auto",
            samples=len(P),
            coverage_pct=round(cov, 1),
            rms_deg=round(rms, 2),
        )
        self.fit_info = {"stage1": info1, "nterms": nterms, "inliers": int(inl.sum()), "mirror_rms": mirror_rms}
        self.candidate = cal
        if rms > MAX_APPLY_RMS_DEG:
            if mirror_rms < rms / 2:
                self.message = (f"Large residual ({rms:.1f} deg) and the compass looks mirrored: "
                                "set compass.axes (e.g. flip the sign of y)")
            else:
                self.message = f"Large residual ({rms:.1f} deg): check compass mounting / magnetic interference"
        return cal

    def rms_of(self, cal: CompassCalibration, mount_offset_deg: float) -> float:
        P = self.points()
        if len(P) == 0:
            return float("nan")
        err = wrap180(P[:, 1] - predict_true(cal, P[:, 2:5], P[:, 5], mount_offset_deg))
        return trimmed_rms(err)

    def decide(self, saved: CompassCalibration, effective: CompassCalibration, mount_offset_deg: float,
               now_wall: float | None = None) -> tuple[CompassCalibration | None, CompassCalibration | None]:
        """→ (калибровка, которую применить сейчас | None, калибровка, которую сохранить | None)."""
        now_wall = self.wall() if now_wall is None else now_wall
        cand = self.candidate
        apply = None
        if cand is not None and cand.rms_deg is not None and cand.rms_deg <= MAX_APPLY_RMS_DEG \
                and cand.coverage_pct >= self.cfg.auto_min_coverage_pct and cand != effective:
            r_cand = self.rms_of(cand, mount_offset_deg)
            r_eff = self.rms_of(effective, mount_offset_deg)
            if not math.isfinite(r_eff) or r_cand < r_eff - APPLY_MARGIN_DEG:
                apply = cand
                self.message = f"Fitted: rms {r_cand:.1f} deg vs GPS (was {r_eff:.1f}), applied"
            else:
                self.message = f"Fitted: rms {r_cand:.1f} deg, current calibration is as good ({r_eff:.1f})"
        target = apply or (effective if effective.source == "auto" else None)
        save = None
        if target is not None and target != saved:
            due = self.last_saved_wall is None or now_wall - self.last_saved_wall >= self.cfg.auto_save_interval_s
            if due:
                r_saved = self.rms_of(saved, mount_offset_deg)
                r_t = self.rms_of(target, mount_offset_deg)
                if not math.isfinite(r_saved) or r_saved - r_t >= max(0.5, 0.15 * r_saved):
                    save = target
                    self.last_saved_wall = now_wall
                    self.last_saved_iso = datetime.fromtimestamp(now_wall, UTC).isoformat(timespec="seconds")
        return apply, save

    # ── состояние ──
    def status(self, enabled: bool, applied: bool, compass_ok: bool, effective: CompassCalibration) -> AutoCalStatus:
        cnt = self.sector_counts()
        cov = 100.0 * sum(1 for n in cnt if n >= MIN_PER_SECTOR) / SECTORS
        if not enabled:
            state, msg = "disabled", "Auto-calibration is off"
        elif not compass_ok:
            state, msg = "error", "Compass not available"
        elif self.candidate is not None:
            state, msg = "fitted", self.message
        else:
            state = "collecting"
            msg = self.message or (f"Collecting: drive straight at >= {self.cfg.auto_min_speed_mps:g} m/s "
                                   f"in different directions (coverage {cov:.0f}%)")
            if self.last_reject:
                msg += f"; last sample skipped: {self.last_reject}"
        rms = self.candidate.rms_deg if self.candidate is not None else effective.rms_deg
        return AutoCalStatus(enabled=enabled, state=state, samples=sum(cnt), coverage_pct=round(cov, 1),
                             rms_deg=rms, applied=applied, last_saved=self.last_saved_iso, message=msg,
                             sectors=cnt)

    def to_state(self) -> dict:
        with self._lock:
            pts = [[round(x, 4) for x in p] for d in self._sec for p in d]
            self.dirty = False
        return {"version": STATE_VERSION, "points": pts, "last_saved": self.last_saved_iso,
                "last_saved_wall": self.last_saved_wall}

    def load_state(self, d: dict) -> int:
        if not isinstance(d, dict) or d.get("version") != STATE_VERSION:
            return 0
        n = 0
        with self._lock:
            for p in d.get("points", []):
                if isinstance(p, list) and len(p) == 6 and all(isinstance(x, (int, float)) for x in p):
                    self._sec[int(p[1] // (360 / SECTORS)) % SECTORS].append(tuple(float(x) for x in p))
                    n += 1
            self.new_points = n
        self.last_saved_iso = d.get("last_saved")
        lw = d.get("last_saved_wall")
        self.last_saved_wall = float(lw) if isinstance(lw, (int, float)) else None
        return n
