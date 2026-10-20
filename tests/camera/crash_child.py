"""Дочерний процесс для теста обрыва: пишет синтетику в реальном времени, пока его не убьют.

Печатает: «F <кадров отдано регистратору>» и «S <байт на диске> <кадров на диске> <путь>» после fsync.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from camhelpers import small_cam

from fnav.camera.recorder import Recorder, TimeSync
from fnav.camera.sources import FakeSource


class Sink:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.n = 0

    def on_video(self, au: bytes, keyframe: bool, ts_us: int) -> None:
        self.rec.push(au, keyframe, ts_us)
        self.n += 1
        print(f"F {self.n}", flush=True)

    def on_lores(self, frame) -> None:
        pass

    def wants_lores(self) -> bool:
        return False

    def on_error(self, msg: str) -> None:
        print(f"E {msg}", flush=True)


def main() -> None:
    root = Path(sys.argv[1])
    fsync_s = float(sys.argv[2])
    cam = small_cam(fsync_interval_s=fsync_s)
    rec = Recorder(root, segment_s=600, fsync_interval_s=fsync_s, gop=cam.gop, fps=cam.fps,
                   time_sync=TimeSync(lambda: True),
                   on_sync=lambda p, size, frames: print(f"S {size} {frames} {p}", flush=True))
    rec.start()
    src = FakeSource(cam, Sink(rec), realtime=True)
    src.start()
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
