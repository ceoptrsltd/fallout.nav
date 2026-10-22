"""Запуск: python -m fnav.core [--mock] [--host 0.0.0.0] [--port 80] [--remote-port 8090] [--data-dir DIR]"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys

log = logging.getLogger("fnav.core")

REMOTE_PORT = 8090  # сюда приходит ssh-туннель с сервера (pi/systemd/fnav-tunnel.service)


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fnav.core", description="fallout.nav: web server and logic")
    ap.add_argument("--mock", action="store_true", help="mock sensors and mock camera (development on a PC)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--remote-port", type=int, default=int(os.environ.get("FNAV_REMOTE_PORT", REMOTE_PORT)),
                    help="127.0.0.1 port for the server tunnel (no localhost trust); 0 = off")
    ap.add_argument("--data-dir", default=None, help="data directory (sets FNAV_DATA_DIR)")
    ap.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = ap.parse_args(argv)

    if args.data_dir:
        os.environ["FNAV_DATA_DIR"] = os.path.abspath(args.data_dir)

    from fnav.core.system import RING

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger().addHandler(RING)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # не писать каждый запрос тайла

    import uvicorn

    from fnav.core.app import create_app
    from fnav.core.runtime import CoreOptions

    sockets = [_bind(args.host, args.port)]
    remote_port = None
    if args.remote_port:
        try:
            sockets.append(_bind("127.0.0.1", args.remote_port))
            remote_port = args.remote_port
        except OSError as e:  # без удалённого входа — но локально всё работает
            log.error("remote port 127.0.0.1:%d unavailable: %s", args.remote_port, e)

    app = create_app(CoreOptions(mock=args.mock, remote_port=remote_port))
    config = uvicorn.Config(
        app,
        log_level=args.log_level,
        proxy_headers=False,  # иначе X-Forwarded-For с 127.0.0.1 подменил бы адрес клиента
        access_log=args.log_level == "debug",
        ws_ping_interval=20,
        ws_ping_timeout=20,
        timeout_graceful_shutdown=3,
    )
    uvicorn.Server(config).run(sockets=sockets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
