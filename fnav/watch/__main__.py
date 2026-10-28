"""python -m fnav.watch — камера на охране (fnav-watch, 127.0.0.1:8082)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m fnav.watch", description="fallout.nav alarm vision (fnav-watch)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--models", type=Path, default=None, help="directory with the ONNX models")
    ap.add_argument("--ring", type=Path, default=None, help="lores frame ring of fnav-camera (default /dev/shm)")
    ap.add_argument("--ring-rear", type=Path, default=None,
                    help="lores frame ring of fnav-camera-rear (default /dev/shm/fnav-lores-rear)")
    ap.add_argument("--fps", type=float, default=4.0, help="frames per second while watching")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",  # время добавит journald
    )
    os.environ.setdefault("OMP_NUM_THREADS", "1")

    import uvicorn

    from fnav.camera.shmring import RingReader, default_path

    from .server import create_app
    from .service import WatchService, default_detectors

    service = WatchService(ring=RingReader(args.ring), ring_rear=RingReader(args.ring_rear or default_path("rear")),
                           detectors=default_detectors(args.models), fps=args.fps)
    app = create_app(service)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False,
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
