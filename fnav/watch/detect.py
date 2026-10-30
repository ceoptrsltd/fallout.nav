"""Нейросети, один поток. Люди — NanoDet-Plus-m-1.5x 416 через NCNN (пакет ncnn), а без него — запасной
NanoDet-Plus-m 320 через OpenCV DNN; лица — YuNet 2023mar через OpenCV.

Почему 1.5x 416 (сравнение 30.10 на записях охраны, docs/contracts/vision.md «Выбор детектора людей»): ночью тёмный
силуэт у капота он видит вдвое чаще, чем m 320 (61 % кадров против 29 % при пороге 0,4), без ложных людей в пустых
записях; на CM4 — ~450 мс на кадр в NCNN (m 320 в OpenCV — ~215 мс).

Модели — fnav/watch/models (README.md: источники, лицензии, sha256). Декодирование NanoDet и NMS — свои, как в
nanodet/model/head/nanodet_plus_head.py: выход (1, N, 80 + 4·8), классы уже после sigmoid; рамка — расстояния
от центра ячейки (x·stride, y·stride) до сторон, распределение по 8 бинам → softmax → матожидание × stride.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("fnav.watch.detect")

MODELS_DIR = Path(__file__).resolve().parent / "models"
NANODET_NCNN = "nanodet-plus-m-1.5x_416_ncnn"  # основной: .param + .bin (NCNN)
NANODET_NCNN_SIZE = 416
NANODET_FILE = "nanodet-plus-m_320.onnx"  # запасной (нет пакета ncnn): OpenCV DNN
YUNET_FILE = "face_detection_yunet_2023mar.onnx"

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 — доли кадра
PERSON = (0,)
VEHICLES = (2, 3, 5, 7)  # COCO: car, motorcycle, bus, truck — пятно машины у фургона не «человек стоит»


@dataclass
class Det:
    box: Box
    score: float

    @property
    def h(self) -> float:
        return self.box[3] - self.box[1]


@dataclass
class Face:
    box: Box
    score: float
    landmarks: list[tuple[float, float]]  # глаз правый, левый, нос, рот правый, левый (доли кадра)
    contrast: float = 0.0  # СКО яркости внутри рамки (отражения в стекле — бледные)


def init_cv() -> None:
    import cv2

    cv2.setNumThreads(1)  # один поток: охрана не должна отнимать ядра у записи и экрана


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    """Жадный NMS; boxes (n, 4) x1,y1,x2,y2."""
    order = np.argsort(-scores)
    keep: list[int] = []
    area = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        ov = inter / np.maximum(1e-9, area[i] + area[rest] - inter)
        order = rest[ov <= iou]
    return keep


class NanoDet:
    """Детектор людей через OpenCV DNN (ONNX) — запасной. fit="stretch" — кадр 4:3 растягивается в квадрат входа
    (так модель обучена: keep_ratio False), "letterbox" — с полями."""

    SIZE = 320
    STRIDES = (8, 16, 32, 64)
    REG_MAX = 7
    MEAN = (103.53, 116.28, 123.675)  # BGR
    STD = 57.63  # среднее из (57.375, 57.12, 58.395): разница < 1,2 %
    name = "NanoDet-Plus-m 320 (OpenCV DNN)"

    def __init__(self, path: Path | None = None, *, conf: float = 0.4, iou: float = 0.5,
                 fit: str = "stretch") -> None:
        import cv2

        self.path = Path(path) if path else MODELS_DIR / NANODET_FILE
        self.net = cv2.dnn.readNetFromONNX(str(self.path))
        self.conf, self.iou, self.fit = conf, iou, fit
        self.priors = centre_priors(self.SIZE, self.STRIDES)
        self.vehicles: list[Det] = []  # машины с последнего кадра (тот же прогон сети, бесплатно)

    def _input(self, bgr: np.ndarray) -> tuple[np.ndarray, float, float]:
        """Блоб (1,3,320,320) и масштабы по x/y: пиксель входа → доля кадра."""
        import cv2

        h, w = bgr.shape[:2]
        size = self.SIZE
        if self.fit == "letterbox":
            k = size / max(w, h)
            nw, nh = round(w * k), round(h * k)
            img = np.zeros((size, size, 3), np.uint8)
            img[:] = self.MEAN  # поле = среднее → после нормализации ноль
            img[:nh, :nw] = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
            sx, sy = 1 / nw, 1 / nh
        else:
            img = cv2.resize(bgr, (size, size), interpolation=cv2.INTER_AREA)
            sx = sy = 1 / size
        blob = cv2.dnn.blobFromImage(img, 1 / self.STD, (size, size), self.MEAN, swapRB=False, crop=False)
        return blob, sx, sy

    def detect(self, bgr: np.ndarray) -> list[Det]:
        blob, sx, sy = self._input(bgr)
        self.net.setInput(blob)
        out = self.net.forward()[0]  # (N, 112)
        return self.decode(out, sx, sy)

    def decode(self, out: np.ndarray, sx: float, sy: float) -> list[Det]:
        self.vehicles = decode_nanodet(out, self.priors, self.conf, self.iou, sx, sy, VEHICLES)
        return decode_nanodet(out, self.priors, self.conf, self.iou, sx, sy)


class NanoDetNcnn:
    """Детектор людей через NCNN (основной): NanoDet-Plus-m-1.5x 416, один поток, fp32 (у A72 нет fp16-арифметики;
    bf16-хранение быстрее на ~6 %, но сдвигает оценки до 0,03 — не берём). Вход — кадр, растянутый в size×size
    (INTER_AREA), BGR, нормировка по каналам внутри NCNN; выход — тот же (N, 112), что у ONNX."""

    STRIDES = (8, 16, 32, 64)
    MEAN = (103.53, 116.28, 123.675)  # BGR
    NORM = (1 / 57.375, 1 / 57.12, 1 / 58.395)

    def __init__(self, stem: Path | None = None, *, size: int = NANODET_NCNN_SIZE, conf: float = 0.4,
                 iou: float = 0.5) -> None:
        import ncnn

        self.stem = Path(stem) if stem else MODELS_DIR / NANODET_NCNN
        self.net = ncnn.Net()
        self.net.opt.num_threads = 1  # охрана не должна отнимать ядра у записи и экрана
        self.net.opt.use_vulkan_compute = False
        # имя с точкой («1.5x») — не with_suffix
        param, binf = self.stem.parent / (self.stem.name + ".param"), self.stem.parent / (self.stem.name + ".bin")
        if not param.is_file() or not binf.is_file():
            raise OSError(f"no NCNN model {self.stem}")
        if self.net.load_param(str(param)) or self.net.load_model(str(binf)):
            raise OSError(f"cannot load NCNN model {self.stem}")
        self._ncnn = ncnn
        self.size, self.conf, self.iou = size, conf, iou
        self.priors = centre_priors(size, self.STRIDES)
        self.name = f"NanoDet-Plus-m-1.5x {size} (NCNN)"
        self.vehicles: list[Det] = []

    def forward(self, bgr: np.ndarray) -> np.ndarray:
        import cv2

        ncnn = self._ncnn
        img = np.ascontiguousarray(cv2.resize(bgr, (self.size, self.size), interpolation=cv2.INTER_AREA))
        mat = ncnn.Mat.from_pixels(img, ncnn.Mat.PixelType.PIXEL_BGR, self.size, self.size)
        mat.substract_mean_normalize(list(self.MEAN), list(self.NORM))
        ex = self.net.create_extractor()
        ex.input("data", mat)
        ret, out = ex.extract("output")
        if ret != 0:
            raise RuntimeError(f"NCNN extract failed ({ret})")
        return np.array(out)  # (N, 112)

    def detect(self, bgr: np.ndarray) -> list[Det]:
        out = self.forward(bgr)
        k = 1 / self.size
        self.vehicles = decode_nanodet(out, self.priors, self.conf, self.iou, k, k, VEHICLES)
        return decode_nanodet(out, self.priors, self.conf, self.iou, k, k)


def person_detector(models_dir: Path | None = None) -> NanoDetNcnn | NanoDet:
    """Основной детектор людей (NCNN), а если пакета ncnn нет или модель не загрузилась — запасной (OpenCV)."""
    d = Path(models_dir) if models_dir else MODELS_DIR
    try:
        det: NanoDetNcnn | NanoDet = NanoDetNcnn(d / NANODET_NCNN)
    except ImportError:
        log.warning("people: the ncnn package is missing - fallback %s", NanoDet.name)
        det = NanoDet(d / NANODET_FILE)
    except (OSError, RuntimeError) as e:
        log.warning("people: %s - fallback %s", e, NanoDet.name)
        det = NanoDet(d / NANODET_FILE)
    log.info("people: %s", det.name)
    return det


def centre_priors(size: int = 320, strides: tuple[int, ...] = (8, 16, 32, 64)) -> np.ndarray:
    """Центры ячеек всех уровней (N, 3): cx, cy, stride — в порядке выхода сети (уровень, строка, столбец)."""
    pri = []
    for s in strides:
        n = -(-size // s)
        yy, xx = np.mgrid[0:n, 0:n]
        pri.append(np.stack([xx.reshape(-1) * s, yy.reshape(-1) * s, np.full(n * n, s)], axis=1))
    return np.concatenate(pri).astype(np.float32)


def decode_nanodet(out: np.ndarray, priors: np.ndarray, conf: float, iou: float, sx: float, sy: float,
                   classes: tuple[int, ...] = PERSON, reg_max: int = 7) -> list[Det]:
    """Выход (N, 80 + 4·(reg_max+1)) → рамки классов classes (по умолчанию люди); sx, sy — пиксель входа → доля
    кадра."""
    score = out[:, classes[0]] if len(classes) == 1 else out[:, list(classes)].max(axis=1)
    idx = np.flatnonzero(score >= conf)
    if idx.size == 0:
        return []
    reg = out[idx, 80:80 + 4 * (reg_max + 1)].reshape(-1, 4, reg_max + 1)
    reg = np.exp(reg - reg.max(axis=2, keepdims=True))
    bins = np.arange(reg_max + 1, dtype=np.float32)
    dist = (reg / reg.sum(axis=2, keepdims=True)) @ bins  # (n, 4): l, t, r, b в шагах сетки
    pri = priors[idx]
    dist *= pri[:, 2:3]
    boxes = np.stack([pri[:, 0] - dist[:, 0], pri[:, 1] - dist[:, 1],
                      pri[:, 0] + dist[:, 2], pri[:, 1] + dist[:, 3]], axis=1)
    keep = nms(boxes, score[idx], iou)
    res = []
    for i in keep:
        x1, y1, x2, y2 = boxes[i]
        box = (float(np.clip(x1 * sx, 0, 1)), float(np.clip(y1 * sy, 0, 1)),
               float(np.clip(x2 * sx, 0, 1)), float(np.clip(y2 * sy, 0, 1)))
        if box[2] > box[0] and box[3] > box[1]:
            res.append(Det(box, float(score[idx[i]])))
    return res


def face_ok(f: Face, aspect: float) -> bool:
    """Проверка точек лица: глаза над ртом, на разумном расстоянии, нос между ними, наклон головы ≤ ~30°.

    aspect — ширина/высота кадра (координаты в долях, переводим в пиксели по смыслу)."""
    x1, y1, x2, y2 = f.box
    w, h = (x2 - x1) * aspect, y2 - y1
    if w <= 0 or h <= 0 or not 0.5 < w / h < 1.4:
        return False
    pts = [(x * aspect, y) for x, y in f.landmarks]
    (rex, rey), (lex, ley), (nx, ny), (_, rmy), (_, lmy) = pts
    mx1, my1, mx2, my2 = x1 * aspect - 0.15 * w, y1 - 0.15 * h, x2 * aspect + 0.15 * w, y2 + 0.15 * h
    if not all(mx1 <= px <= mx2 and my1 <= py <= my2 for px, py in pts):
        return False
    eye_d = float(np.hypot(lex - rex, ley - rey))
    if not 0.2 * w <= eye_d <= 0.8 * w or abs(ley - rey) > 0.6 * eye_d:
        return False
    eye_y, mouth_y = (rey + ley) / 2, (rmy + lmy) / 2
    if mouth_y - eye_y < 0.15 * h:
        return False
    if not eye_y - 0.1 * h <= ny <= mouth_y + 0.1 * h:
        return False
    return min(rex, lex) - 0.2 * w <= nx <= max(rex, lex) + 0.2 * w


class YuNet:
    """Детектор лиц на центральной вырезке кадра, уменьшенной до in_w по ширине."""

    def __init__(self, path: Path | None = None, *, score: float = 0.8, in_w: int = 192,
                 region: Box = (0.2, 0.05, 0.8, 0.7)) -> None:
        import cv2

        self.path = Path(path) if path else MODELS_DIR / YUNET_FILE
        self.score, self.in_w, self.region = score, in_w, region
        self.det = cv2.FaceDetectorYN.create(str(self.path), "", (in_w, in_w), score, 0.3, 20)
        self._size: tuple[int, int] | None = None

    def detect(self, bgr: np.ndarray) -> list[Face]:
        import cv2

        h, w = bgr.shape[:2]
        rx1, ry1, rx2, ry2 = self.region
        px1, py1, px2, py2 = int(rx1 * w), int(ry1 * h), int(rx2 * w), int(ry2 * h)
        crop = bgr[py1:py2, px1:px2]
        cw, ch = crop.shape[1], crop.shape[0]
        iw = self.in_w
        ih = max(16, round(iw * ch / cw))
        if self._size != (iw, ih):
            self.det.setInputSize((iw, ih))
            self._size = (iw, ih)
        small = cv2.resize(crop, (iw, ih), interpolation=cv2.INTER_AREA)
        _, faces = self.det.detect(small)
        if faces is None:
            return []
        kx, ky = cw / iw, ch / ih
        gray = None
        res = []
        for f in faces:
            fx, fy, fw, fh = (float(v) for v in f[:4])
            box = ((px1 + fx * kx) / w, (py1 + fy * ky) / h, (px1 + (fx + fw) * kx) / w, (py1 + (fy + fh) * ky) / h)
            lm = [((px1 + f[4 + 2 * i] * kx) / w, (py1 + f[5 + 2 * i] * ky) / h) for i in range(5)]
            if gray is None:
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gx1, gy1 = max(0, int(fx)), max(0, int(fy))
            patch = gray[gy1:max(gy1 + 1, int(fy + fh)), gx1:max(gx1 + 1, int(fx + fw))]
            res.append(Face(box, float(f[14]), lm, float(patch.std()) if patch.size else 0.0))
        return res


def load_detectors(models_dir: Path | None = None) -> tuple[NanoDetNcnn | NanoDet, YuNet]:
    init_cv()
    d = Path(models_dir) if models_dir else MODELS_DIR
    return person_detector(d), YuNet(d / YUNET_FILE)
