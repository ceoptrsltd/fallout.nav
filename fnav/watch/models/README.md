# Models of fnav-watch

Loaded by `fnav/watch/detect.py` through OpenCV DNN (`opencv-python-headless`), one thread. Files are used as
downloaded, without changes.

| File | What | Source | Size, bytes | sha256 | License |
|---|---|---|---|---|---|
| `nanodet-plus-m_320.onnx` | NanoDet-Plus-m, 320×320, COCO 80 classes (only class 0 "person" is used) | https://github.com/RangiLyu/nanodet/releases/download/v1.0.0-alpha-1/nanodet-plus-m_320.onnx | 4793615 | `4f12723cce3d48e47ca92cb925ba74d97a965c069208edca660bbb9f7ce2c610` | Apache-2.0, `LICENSE-nanodet.txt` (from https://github.com/RangiLyu/nanodet) |
| `face_detection_yunet_2023mar.onnx` | YuNet face detector (2023mar), used through `cv2.FaceDetectorYN` | https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx | 232589 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | MIT, `LICENSE-yunet.txt` (from https://huggingface.co/opencv/face_detection_yunet) |

NanoDet output `(1, 2125, 112)`: 80 class scores (after sigmoid) + 4 × 8 distance bins per cell, cells of strides
8/16/32/64 at `(x·stride, y·stride)`; the decoder and NMS are in `detect.py` (`decode_nanodet`, `nms`).

Measured on the CM4 (Cortex-A72, 1 thread, OpenCV 5.0.0 and 4.14.0 give the same numbers): NanoDet ~290 ms CPU per
frame, YuNet on a 192×156 crop ~20 ms. Details: docs/contracts/vision.md.
