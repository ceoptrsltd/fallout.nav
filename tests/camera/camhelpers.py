from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fnav.models import CameraSettings, LoresSettings

FPS = 15


def small_cam(**kw) -> CameraSettings:
    base = {"out_w": 160, "out_h": 120, "fps": FPS, "gop": 15, "bitrate_kbps": 500, "fsync_interval_s": 1.0,
            "lores": LoresSettings(w=96, h=72, fps=15)}
    base.update(kw)
    return CameraSettings(**base)


def encode_frames(n: int, gop: int = 15, w: int = 160, h: int = 120, fps: int = FPS) -> list[tuple[bytes, bool, int]]:
    """n кадров H.264 (libx264): [(au, keyframe, ts_us)] с метками времени i/fps."""
    import av

    from fnav.camera.sources import open_h264_encoder

    cc, _ = open_h264_encoder(w, h, fps, gop, 300)
    out = []
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    for i in range(n):
        img = np.roll(noise, i * 3, axis=1)
        vf = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        vf.pts = i
        for p in cc.encode(vf):
            out.append((bytes(p), bool(p.is_keyframe), int(p.pts * 1_000_000 / fps)))
    for p in cc.encode(None):
        out.append((bytes(p), bool(p.is_keyframe), int(p.pts * 1_000_000 / fps)))
    return out


# синтетические AU (без настоящего кодирования) — для логики регистратора
SPS = b"\x00\x00\x00\x01\x67\x42\x00\x1e\xab"
PPS = b"\x00\x00\x00\x01\x68\xce\x38\x80"


def synth_au(key: bool, i: int) -> bytes:
    body = bytes([(i * 7 + k) & 0xFF for k in range(300)])
    if key:
        return SPS + PPS + b"\x00\x00\x00\x01\x65" + body
    return b"\x00\x00\x00\x01\x41" + body


def decode_file(path: Path) -> tuple[int, int, list]:
    """(кадров, ошибок декодирования, список кадров av) — без исключений."""
    import av

    frames = []
    errors = 0
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        for pkt in c.demux(s):
            try:
                frames.extend(pkt.decode())
            except av.error.FFmpegError:
                errors += 1
    return len(frames), errors, frames
