"""Детектор движения — ворота для нейросетей. Работает на каждом кадре, ~1 мс на A72.

Y → 192×144 (INTER_AREA), фон — скользящее среднее; порог = k × шум кадра (медиана |кадр − фон|), поэтому сам
подстраивается под ночное усиление до 16×, и не ниже 10 % яркости фона (мерцание фонарей, блики); шум втрое
выше обычного — значит, изменился весь кадр. Внутри переднего плана фон почти не учится (человек, вставший у
капота, остаётся «движением»). Открытие 3×3, пятна от 0,5 % кадра, движение — в 2 из 3 кадров. Изменилось > 25 %
кадра или скакнула выдержка × усиление (фары, AE) — фон заново и 1 с тишины. Метка времени в углу кадра и
капот (ниже ближней линии) не смотрим.

Для правила «стоит у фургона» у каждого пятна есть подробности (Blob): заполненность рамки, доля пикселей рамки,
изменившихся с прошлого кадра (жив ли он: стоящая машина — нет), доля пикселей ярче фона и яркость (свет фар и
блики — светлые, силуэт ночью — тёмный).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

GW, GH = 192, 144
TIMESTAMP_RECT = (0.0, 0.0, 0.3, 0.08)  # наложенное время (fnav/camera/jpeg.py TimestampOverlay)


@dataclass
class Blob:
    box: tuple[float, float, float, float]  # доли кадра x1,y1,x2,y2
    fill: float  # площадь пятна / площадь рамки
    active: float  # доля пикселей рамки, изменившихся с прошлого кадра
    bright: float  # доля пикселей пятна ярче фона
    level: float  # средняя яркость пятна, 0…255

    @property
    def h(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2


@dataclass
class Motion:
    moving: bool = False  # движение подтверждено (2 из 3 кадров)
    blobs: list[tuple[float, float, float, float]] = field(default_factory=list)  # доли кадра x1,y1,x2,y2
    details: list[Blob] = field(default_factory=list)  # те же пятна подробно
    frac: float = 0.0  # доля изменившихся пикселей в зоне
    thr: float = 0.0
    noise: float = 0.0
    reset: bool = False  # фон пересобран (свет / AE)

    def touches(self, rect: tuple[float, float, float, float]) -> bool:
        x1, y1, x2, y2 = rect
        return any(b[0] < x2 and b[2] > x1 and b[1] < y2 and b[3] > y1 for b in self.blobs)


class MotionGate:
    def __init__(self, *, k: float = 4.0, min_thr: float = 6.0, max_thr: float = 50.0, alpha: float = 0.05,
                 alpha_fg: float = 0.004, min_blob: float = 0.005, global_frac: float = 0.25,
                 exp_jump: float = 1.5, quiet_s: float = 1.0, bottom: float = 1.0, rel: float = 0.1) -> None:
        self.k, self.min_thr, self.max_thr, self.rel = k, min_thr, max_thr, rel
        self.alpha, self.alpha_fg = alpha, alpha_fg
        self.min_blob, self.global_frac = min_blob, global_frac
        self.exp_jump, self.quiet_s = exp_jump, quiet_s
        self._kernel = None
        self.set_bottom(bottom)
        self.reset()

    def set_bottom(self, bottom: float) -> None:
        """Нижняя граница зоны (доля высоты): ниже — капот и приборная панель."""
        bottom = min(1.0, max(0.2, bottom))
        roi = np.full((GH, GW), 255, np.uint8)
        roi[round(bottom * GH):, :] = 0
        x1, y1, x2, y2 = TIMESTAMP_RECT
        roi[int(y1 * GH):int(np.ceil(y2 * GH)), int(x1 * GW):int(np.ceil(x2 * GW))] = 0
        self.roi = roi
        self._roi_px = max(1, int(np.count_nonzero(roi)))
        self._roi_idx = np.flatnonzero(roi.reshape(-1)[::6]) * 6  # подвыборка для медианы шума
        self.bottom = bottom

    def reset(self) -> None:
        self.bg: np.ndarray | None = None
        self._prev: np.ndarray | None = None  # прошлый кадр (живое ли пятно)
        self._exp: float | None = None
        self._quiet_until = -1e18
        self._hist: deque[bool] = deque(maxlen=3)
        self._noise: float | None = None  # обычный шум (скользящее среднее)

    def _rebuild(self, small: np.ndarray, t: float) -> None:
        self.bg = small.copy()
        self._quiet_until = t + self.quiet_s
        self._hist.clear()

    def update(self, y: np.ndarray, t: float, exposure_gain: float | None = None) -> Motion:
        import cv2

        small = cv2.resize(y, (GW, GH), interpolation=cv2.INTER_AREA).astype(np.float32)
        prev, self._prev = self._prev, small
        out = Motion()
        exp_prev, self._exp = self._exp, exposure_gain
        if self.bg is None:
            self._rebuild(small, t)
            out.reset = True
            return out
        if exposure_gain and exp_prev and max(exposure_gain / exp_prev, exp_prev / exposure_gain) > self.exp_jump:
            self._rebuild(small, t)
            out.reset = True
            return out
        diff = cv2.absdiff(small, self.bg)
        sample = diff.reshape(-1)[self._roi_idx]
        med = float(np.partition(sample, sample.size // 2)[sample.size // 2])
        out.noise = 1.4826 * med
        if self._noise is not None and out.noise > max(3.0, 3.0 * self._noise):
            # изменилось больше половины кадра (свет, AE без метаданных) — это не движение
            self._rebuild(small, t)
            out.reset = True
            return out
        self._noise = out.noise if self._noise is None else self._noise + (out.noise - self._noise) * 0.1
        out.thr = thr = min(self.max_thr, max(self.min_thr, self.k * out.noise))
        # яркие места (фонари, блики на стекле) мерцают сильнее шума: порог не ниже доли яркости фона
        mask = cv2.compare(diff, cv2.max(self.bg * self.rel, thr), cv2.CMP_GT)
        mask = cv2.bitwise_and(mask, self.roi)
        out.frac = cv2.countNonZero(mask) / self._roi_px
        if out.frac > self.global_frac:
            self._rebuild(small, t)
            out.reset = True
            return out
        if self._kernel is None:
            self._kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel) if out.frac else mask
        quiet = t < self._quiet_until
        empty = not out.frac or not cv2.countNonZero(mask)
        if not empty and not quiet:
            self._blobs(out, small, prev, mask, thr)  # до обновления фона: яркость — относительно него
        if empty or quiet:  # обычный случай (сцена неподвижна) — без масок, вдвое дешевле
            cv2.accumulateWeighted(small, self.bg, 0.5 if quiet else self.alpha)
        else:
            # фон: снаружи переднего плана — быстро, внутри — очень медленно (машина, вставшая рядом, уйдёт в фон)
            fg = cv2.dilate(mask, self._kernel, iterations=2)
            cv2.accumulateWeighted(small, self.bg, self.alpha, mask=cv2.bitwise_not(fg))
            cv2.accumulateWeighted(small, self.bg, self.alpha_fg, mask=fg)
        if quiet:
            return out
        self._hist.append(bool(out.blobs))
        out.moving = sum(self._hist) >= 2
        return out

    def _blobs(self, out: Motion, small: np.ndarray, prev: np.ndarray | None, mask: np.ndarray, thr: float) -> None:
        import cv2

        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        min_px = self.min_blob * self._roi_px
        bg = self.bg
        assert bg is not None
        for i in range(1, n):
            x, yy, w, h, area = (int(v) for v in stats[i])
            if area < min_px:
                continue
            box = (x / GW, yy / GH, (x + w) / GW, (yy + h) / GH)
            out.blobs.append(box)
            cur = small[yy:yy + h, x:x + w]
            inside = labels[yy:yy + h, x:x + w] == i
            px = cur[inside]
            active = 0.0
            if prev is not None:
                active = float(np.count_nonzero(np.abs(cur - prev[yy:yy + h, x:x + w]) > thr)) / (w * h)
            bright = float(np.count_nonzero(px > bg[yy:yy + h, x:x + w][inside])) / max(1, px.size)
            out.details.append(Blob(box, area / (w * h), active, bright, float(px.mean()) if px.size else 0.0))
