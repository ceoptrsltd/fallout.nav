"""Кадр для конвейера: яркость (Y) для детектора движения, BGR — только когда нужна нейросеть."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Frame:
    t: float  # секунды (монотонные часы или PTS записи) — по ним все правила
    kind: str  # "yuv420" (h*3/2, stride) | "rgb" (h, w, 3) | "bgr" (h, w, 3)
    data: np.ndarray
    w: int
    h: int
    exposure: dict[str, float] = field(default_factory=dict)
    wall: float | None = None  # unix-время кадра (для событий); None — взять текущее
    _bgr: np.ndarray | None = field(default=None, repr=False)
    _y: np.ndarray | None = field(default=None, repr=False)

    @property
    def y(self) -> np.ndarray:
        """Яркость (h, w) uint8."""
        if self._y is None:
            if self.kind == "yuv420":
                self._y = self.data[:self.h, :self.w]
            else:
                import cv2

                code = cv2.COLOR_RGB2GRAY if self.kind == "rgb" else cv2.COLOR_BGR2GRAY
                self._y = cv2.cvtColor(np.ascontiguousarray(self.data[:, :, :3]), code)
        return self._y

    def i420(self) -> np.ndarray:
        """Плотный I420 (h*3/2, w): у Picamera2 строки могут быть с запасом (stride > w)."""
        d = self.data
        w, h = self.w, self.h
        if d.shape[1] == w:
            return d
        stride = d.shape[1]
        y = d[:h, :w]
        u = d[h:h + h // 4].reshape(h // 2, stride // 2)[:, :w // 2]
        v = d[h + h // 4:h + h // 2].reshape(h // 2, stride // 2)[:, :w // 2]
        return np.concatenate([y.reshape(-1), u.reshape(-1), v.reshape(-1)]).reshape(h * 3 // 2, w)

    @property
    def bgr(self) -> np.ndarray:
        if self._bgr is None:
            import cv2

            if self.kind == "yuv420":
                self._bgr = cv2.cvtColor(self.i420(), cv2.COLOR_YUV2BGR_I420)
            elif self.kind == "rgb":
                self._bgr = cv2.cvtColor(np.ascontiguousarray(self.data[:, :, :3]), cv2.COLOR_RGB2BGR)
            else:
                self._bgr = self.data
        return self._bgr

    @property
    def exposure_gain(self) -> float | None:
        """Выдержка × усиление — скачок значит смену освещения/AE (фары), а не движение."""
        e = self.exposure
        if "ExposureTime" not in e or "AnalogueGain" not in e:
            return None
        v = e["ExposureTime"] * e["AnalogueGain"] * e.get("DigitalGain", 1.0)
        return v if math.isfinite(v) and v > 0 else None
