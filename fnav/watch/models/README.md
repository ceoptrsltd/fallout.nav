# Models of fnav-watch

Loaded by `fnav/watch/detect.py`, one thread: people — NanoDet-Plus-m-1.5x 416 through NCNN (`ncnn`), and when
the package or the files are missing, the fallback NanoDet-Plus-m 320 through OpenCV DNN; faces — YuNet through
OpenCV DNN (`opencv-python-headless`). Files are used as downloaded, without changes (the NCNN pair is unpacked
from the release zip).

| File | What | Source | Size, bytes | sha256 | License |
|---|---|---|---|---|---|
| `nanodet-plus-m-1.5x_416_ncnn.param`, `nanodet-plus-m-1.5x_416_ncnn.bin` | NanoDet-Plus-m-1.5x, 416×416, COCO 80 classes, NCNN (person detector; classes 2/3/5/7 — vehicles for the marks) | https://github.com/RangiLyu/nanodet/releases/download/v1.0.0-alpha-1/nanodet-plus-m-1.5x_416_ncnn.zip (zip sha256 `6bb45a1593b6f593952fa42d28ccc2f83347db04131a815901eedec4f58e8177`) | 23191 / 4956920 | `c0b49ca9f97e48e495ae72b5de131aca9065a4a5fbc76c0ef977d95671c0759c` / `84ceb4cd5b0f51272a92de8af122506ef8d314a3d39bd2781faa2584cda30a44` | Apache-2.0, `LICENSE-nanodet.txt` |
| `nanodet-plus-m_320.onnx` | NanoDet-Plus-m, 320×320, COCO 80 classes — fallback person detector (no `ncnn`) | https://github.com/RangiLyu/nanodet/releases/download/v1.0.0-alpha-1/nanodet-plus-m_320.onnx | 4793615 | `4f12723cce3d48e47ca92cb925ba74d97a965c069208edca660bbb9f7ce2c610` | Apache-2.0, `LICENSE-nanodet.txt` (from https://github.com/RangiLyu/nanodet) |
| `face_detection_yunet_2023mar.onnx` | YuNet face detector (2023mar), used through `cv2.FaceDetectorYN` | https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx | 232589 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | MIT, `LICENSE-yunet.txt` (from https://huggingface.co/opencv/face_detection_yunet) |

NanoDet output `(N, 112)` (416: N = 3598, 320: N = 2125): 80 class scores (after sigmoid) + 4 × 8 distance bins
per cell, cells of strides 8/16/32/64 at `(x·stride, y·stride)`; the decoder and NMS are in `detect.py`
(`decode_nanodet`, `nms`). NCNN input blob `data` (BGR, mean/norm applied in NCNN), output `output`.

Measured on the CM4 (Cortex-A72, 1 thread, 30.10.2026): NanoDet-Plus-m-1.5x 416 in NCNN ~450 ms CPU per frame;
NanoDet-Plus-m 320 in OpenCV ~215 ms; YuNet on a 192×156 crop ~20 ms. Why this model and the comparison:
docs/contracts/vision.md "Choosing the person detector".
