"""Фикстуры тестов fnav-core."""

from __future__ import annotations

import pytest
from core_helpers import LiveServer, make_opts


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("FNAV_DATA_DIR", str(d))
    for k in ("FNAV_SECRETS", "FNAV_PASSWORD_HASH", "FNAV_SESSION_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return d


@pytest.fixture
def live_server(data_dir):
    servers: list[LiveServer] = []

    def start(**kw) -> LiveServer:
        kw.setdefault("background", True)
        srv = LiveServer(make_opts(**kw)).__enter__()
        servers.append(srv)
        return srv

    yield start
    for s in servers:
        s.__exit__(None, None, None)
