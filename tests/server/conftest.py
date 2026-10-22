"""Фикстуры тестов fnav-relay: база во временном каталоге, сервер на свободном порту."""

from __future__ import annotations

import threading

import pytest
from relay_helpers import DEVICE_TOKEN, Config, Relay, RelayServer, Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "track.db"), radius_m=50.0, stop_min_s=300.0)
    yield s
    s.close()


@pytest.fixture
def relay(tmp_path):
    cfg = Config(listen_host="127.0.0.1", listen_port=0, db_path=str(tmp_path / "relay.db"),
                 device_token=DEVICE_TOKEN)
    st = Store(cfg.db_path, cfg.radius_m, cfg.stop_min_s)
    srv = RelayServer(cfg, st)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    yield Relay(f"http://127.0.0.1:{srv.server_address[1]}", st, cfg)
    srv.shutdown()
    srv.server_close()
    th.join(5)
    st.close()
