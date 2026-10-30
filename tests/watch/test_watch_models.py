"""Модели (fnav/watch/models): загрузка (NCNN, запасной OpenCV), пробный прогон, декодирование NanoDet и NMS,
проверка точек лица."""

from __future__ import annotations

import hashlib
import importlib.util

import numpy as np
import pytest
from watchhelpers import face

from fnav.watch import detect
from fnav.watch.detect import (
    MODELS_DIR,
    NANODET_FILE,
    NANODET_NCNN,
    YUNET_FILE,
    NanoDet,
    centre_priors,
    decode_nanodet,
    face_ok,
    nms,
)

cv2 = pytest.importorskip("cv2")

SHA256 = {  # models/README.md
    NANODET_FILE: "4f12723cce3d48e47ca92cb925ba74d97a965c069208edca660bbb9f7ce2c610",
    NANODET_NCNN + ".param": "c0b49ca9f97e48e495ae72b5de131aca9065a4a5fbc76c0ef977d95671c0759c",
    NANODET_NCNN + ".bin": "84ceb4cd5b0f51272a92de8af122506ef8d314a3d39bd2781faa2584cda30a44",
    YUNET_FILE: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
}
needs_ncnn = pytest.mark.skipif(importlib.util.find_spec("ncnn") is None, reason="ncnn")
have_models = all((MODELS_DIR / f).is_file() for f in SHA256)
needs_models = pytest.mark.skipif(not have_models, reason="models are not in fnav/watch/models")


@pytest.fixture(scope="module")
def detectors():
    return detect.load_detectors()


@needs_models
def test_model_files_are_intact():
    for name, digest in SHA256.items():
        assert hashlib.sha256((MODELS_DIR / name).read_bytes()).hexdigest() == digest, name


@needs_models
@needs_ncnn
def test_smoke_inference(detectors):
    """Основной детектор людей — NanoDet-Plus-m-1.5x 416 через NCNN (имя файла с точкой — «1.5x»)."""
    nd, yn = detectors
    assert nd.name == "NanoDet-Plus-m-1.5x 416 (NCNN)" and nd.net.opt.num_threads == 1
    blank = np.full((576, 768, 3), 90, np.uint8)
    assert nd.detect(blank) == []
    assert yn.detect(blank) == []
    out = nd.forward(blank)
    assert out.shape == (3598, 112)  # 52² + 26² + 13² + 7² ячеек при 416
    assert 0.0 <= out[:, :80].min() and out[:, :80].max() <= 1.0  # классы уже после sigmoid
    assert cv2.getNumThreads() == 1


@needs_models
def test_fallback_opencv_detector(monkeypatch, tmp_path, caplog):
    """Нет пакета ncnn или файлов модели — запасной NanoDet-Plus-m 320 через OpenCV DNN."""
    nd = NanoDet()
    blank = np.full((576, 768, 3), 90, np.uint8)
    assert nd.detect(blank) == []
    blob, _, _ = nd._input(blank)
    nd.net.setInput(blob)
    out = nd.net.forward()
    assert out.shape == (1, 2125, 112)
    assert 0.0 <= out[0, :, :80].min() and out[0, :, :80].max() <= 1.0

    def no_ncnn(*_a, **_k):
        raise ImportError("No module named 'ncnn'")

    monkeypatch.setattr(detect, "NanoDetNcnn", no_ncnn)
    assert isinstance(detect.person_detector(), NanoDet) and "ncnn package is missing" in caplog.text
    monkeypatch.undo()
    (tmp_path / NANODET_FILE).write_bytes((MODELS_DIR / NANODET_FILE).read_bytes())  # без файлов NCNN
    if importlib.util.find_spec("ncnn") is not None:
        assert isinstance(detect.person_detector(tmp_path), NanoDet) and "no NCNN model" in caplog.text


def test_nanodet_decode_geometry():
    out = np.zeros((2125, 112), np.float32)
    out[:, 80:] = -10.0
    # ячейка stride 16 (вторая сетка 20×20): x=10, y=8 → центр (160, 128); расстояния l=2, t=3, r=2, b=4 шага
    i = 1600 + 8 * 20 + 10
    out[i, 0] = 0.9
    for side, k in enumerate((2, 3, 2, 4)):
        out[i, 80 + side * 8 + k] = 10.0
    out[i + 1, 0] = 0.8  # соседняя ячейка, почти та же рамка — уйдёт в NMS
    for side, k in enumerate((3, 3, 1, 4)):
        out[i + 1, 80 + side * 8 + k] = 10.0
    out[5, 1] = 0.99  # не человек (класс 1) — не берём
    dets = decode_nanodet(out, centre_priors(), 0.4, 0.5, 1 / 320, 1 / 320)
    assert len(dets) == 1
    x1, y1, x2, y2 = dets[0].box
    assert dets[0].score == pytest.approx(0.9)
    assert (x1 * 320, y1 * 320, x2 * 320, y2 * 320) == pytest.approx((128, 80, 192, 192), abs=0.1)


def test_nms():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30]], np.float32)
    assert sorted(nms(boxes, np.array([0.9, 0.8, 0.7]), 0.5)) == [0, 2]


def test_face_landmark_sanity():
    assert face_ok(face(0.5, 0.4, 0.12), 4 / 3)
    f = face(0.5, 0.4, 0.12)
    f.landmarks[1] = (f.landmarks[0][0] + 0.001, f.landmarks[0][1])  # глаза в одной точке
    assert not face_ok(f, 4 / 3)
    f = face(0.5, 0.4, 0.12)
    f.landmarks[2] = (f.landmarks[2][0], f.landmarks[2][1] + 0.2)  # нос далеко за рамкой
    assert not face_ok(f, 4 / 3)


@needs_models
def test_replay_cli_smoke(tmp_path, capsys):
    """python -m fnav.watch.replay на маленькой синтетической записи."""
    av = pytest.importorskip("av")
    from fnav.watch import replay

    path = tmp_path / "t.ts"
    with av.open(str(path), "w", format="mpegts") as c:
        s = c.add_stream("mpeg2video", rate=15)  # кодер есть в любой сборке PyAV
        s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
        rng = np.random.default_rng(0)
        for i in range(45):
            img = rng.integers(80, 90, (240, 320, 3), dtype=np.uint8)
            if i > 15:
                img[60:200, 40 + 4 * i:90 + 4 * i] = 230
            fr = av.VideoFrame.from_ndarray(img, format="rgb24")
            for p in s.encode(fr):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    assert replay.main([str(path), "--out", str(tmp_path / "out"), "--all"]) == 0
    text = capsys.readouterr().out
    assert "done:" in text and "frames" in text
    assert list((tmp_path / "out").glob("f_*.jpg"))
