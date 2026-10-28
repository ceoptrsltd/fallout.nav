"""python -m fnav.camera [--fake] [--camera rear] — сервис камеры и регистратора (передняя или задняя)."""

from __future__ import annotations

import argparse
import logging
import sys

from .rtsp import DEFAULT_URL


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m fnav.camera", description="fallout.nav camera + dashcam recorder")
    ap.add_argument("--fake", action="store_true", help="synthetic source instead of the camera (dev PC)")
    ap.add_argument("--camera", choices=("front", "rear"), default="front",
                    help="front: Picamera2; rear: IP camera over RTSP (settings.rear_camera)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None, help="default 8081 (front) / 8083 (rear)")
    ap.add_argument("--rtsp-url", default=None, help="MediaMTX publish URL (default: path cam / rear)")
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
    from .service import REAR_PORT, REAR_RTSP_URL, CameraService

    rear = args.camera == "rear"
    rtsp_url = None if args.no_rtsp else (args.rtsp_url or (REAR_RTSP_URL if rear else DEFAULT_URL))
    port = args.port or (REAR_PORT if rear else 8081)
    if args.fake and not args.no_rtsp:
        logging.getLogger("fnav.camera").info("fake mode: RTSP publishing to %s (retries if MediaMTX is absent)",
                                              rtsp_url)
    service = CameraService(fake=args.fake, rtsp_url=rtsp_url, segment_s_override=args.segment_seconds,
                            camera_id=args.camera)
    app = create_app(service)
    uvicorn.run(app, host=args.host, port=port, log_level="warning", access_log=False,
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
