"""Помощники тестов fnav-core (уникальное имя модуля — без конфликтов с другими tests/*)."""

from __future__ import annotations

import asyncio
import io
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Self

import httpx
import uvicorn

from fnav.core.app import create_app
from fnav.core.runtime import CoreOptions

REMOTE = ("10.0.0.5", 50000)


def tiny_jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 120, 30)).save(buf, "JPEG")
    return buf.getvalue()


class FakeUpstream:
    """httpx-транспорт: записывает запросы, отвечает заданной функцией (по умолчанию — «нет связи»)."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is None:
            raise httpx.ConnectError("refused", request=request)
        return self.handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def make_opts(**kw) -> CoreOptions:
    kw.setdefault("mock", True)
    kw.setdefault("background", False)
    kw.setdefault("local_transport", FakeUpstream().transport)
    kw.setdefault("tile_transport", FakeUpstream(lambda r: httpx.Response(
        200, content=tiny_jpeg(), headers={"content-type": "image/jpeg"})).transport)
    return CoreOptions(**kw)


@asynccontextmanager
async def running_app(opts: CoreOptions | None = None, client_addr=REMOTE):
    app = create_app(opts or make_opts())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=client_addr)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


async def login(c: httpx.AsyncClient, password: str = "fnav-dev") -> None:
    r = await c.post("/api/login", json={"password": password})
    assert r.status_code == 200, r.text


# ── настоящий uvicorn в отдельном потоке (для WebSocket и стриминга) ──


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, opts: CoreOptions):
        self.app = create_app(opts)
        self.port = free_port()
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                             timeout_graceful_shutdown=1, proxy_headers=False)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.run(self.server.serve())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def __enter__(self) -> Self:
        self.thread.start()
        t0 = time.monotonic()
        while not self.server.started:
            if time.monotonic() - t0 > 15:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
