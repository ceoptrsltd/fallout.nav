"""python -m fnav.camera [--fake] — сервис камеры и регистратора."""

from __future__ import annotations

import argparse
import logging
import sys

from .rtsp import DEFAULT_URL


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m fnav.camera", description="fallout.nav camera + dashcam recorder")
    ap.add_argument("--fake", action="store_true", help="synthetic source instead of Picamera2 (dev PC)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--rtsp-url", default=DEFAULT_URL, help="MediaMTX publish URL")
    ap.add_argument("--no-rtsp", action="store_true", help="do not publish to MediaMTX")
    ap.add_argument("--segment-seconds", type=float, default=None, help="override segment length (debug)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",  # время добавит journald
    )

    import uvicorn

    from .server import create_app
    from .service import CameraService

    rtsp_url = None if args.no_rtsp else args.rtsp_url
    if args.fake and not args.no_rtsp:
        logging.getLogger("fnav.camera").info("fake mode: RTSP publishing to %s (retries if MediaMTX is absent)",
                                              rtsp_url)
    service = CameraService(fake=args.fake, rtsp_url=rtsp_url, segment_s_override=args.segment_seconds)
    app = create_app(service)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False,
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
