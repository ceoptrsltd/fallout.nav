"""Запуск: python3 -m fnav_relay (настройки — из окружения, см. config.py)."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from .api import RelayServer
from .config import Config
from .store import Store

log = logging.getLogger("fnav_relay")


def main() -> None:
    logging.basicConfig(level=(os.environ.get("FNAV_RELAY_LOG_LEVEL") or "INFO").upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    os.umask(0o077)  # в базе лежат хэш пароля и секрет сессий
    cfg = Config.from_env()
    if not cfg.device_token:
        log.warning("FNAV_RELAY_DEVICE_TOKEN is empty: the device cannot send positions or sync its password")
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path, cfg.radius_m, cfg.stop_min_s)
    srv = RelayServer(cfg, store)

    def stop(*_: object) -> None:  # shutdown() нельзя звать из потока serve_forever
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("listening on %s:%d, db %s, radius %.0f m, stop >= %.0f s",
             cfg.listen_host, srv.server_address[1], cfg.db_path, cfg.radius_m, cfg.stop_min_s)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        store.close()
        log.info("stopped")


if __name__ == "__main__":
    main()
