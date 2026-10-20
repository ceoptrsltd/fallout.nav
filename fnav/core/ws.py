"""WebSocket /ws: рассылка телеметрии, настроек, событий и NMEA."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fnav.core.util import cancel_tasks

log = logging.getLogger("fnav.core.ws")

QUEUE_MAX = 50  # 5 с телеметрии; клиент, который не успевает, отключаем


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.topics: set[str] = set()
        self.nmea_last: str | None = None
        self.closed = False

    def send(self, text: str) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(text)
            return True
        except asyncio.QueueFull:
            self.closed = True
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(None)  # сигнал отправителю закрыться
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            return False


class WsManager:
    def __init__(self) -> None:
        self.clients: set[Client] = set()

    def __len__(self) -> int:
        return len(self.clients)

    def broadcast_text(self, text: str, topic: str | None = None) -> None:
        for c in list(self.clients):
            if topic is None or topic in c.topics:
                c.send(text)

    def broadcast(self, msg: dict[str, Any], topic: str | None = None) -> None:
        if self.clients:
            self.broadcast_text(json.dumps(msg, ensure_ascii=False), topic)

    def event(self, event: str, **fields: Any) -> None:
        self.broadcast({"type": "event", "event": event, **fields})

    def toast(self, text: str, level: str = "info") -> None:
        self.event("toast", level=level, text=text)

    def subscribers(self, topic: str) -> list[Client]:
        return [c for c in self.clients if topic in c.topics]

    async def serve(self, ws: WebSocket, hello: list[str]) -> None:
        await ws.accept()
        client = Client(ws)
        for h in hello:
            client.send(h)
        self.clients.add(client)
        sender = asyncio.create_task(self._sender(client))
        try:
            while True:
                raw = await ws.receive_text()
                self._handle(client, raw)
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws: %s", e)
        finally:
            self.clients.discard(client)
            client.closed = True
            await cancel_tasks(sender)

    async def _sender(self, client: Client) -> None:
        try:
            while True:
                text = await client.queue.get()
                if text is None:
                    break
                await client.ws.send_text(text)
        except Exception as e:  # noqa: BLE001 — разрыв соединения
            log.debug("ws send: %s", e)
        finally:
            self.clients.discard(client)
            with contextlib.suppress(Exception):
                await client.ws.close()

    def _handle(self, client: Client, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("type")
        topics = msg.get("topics") or []
        if not isinstance(topics, list):
            return
        topics = {x for x in topics if isinstance(x, str)}
        if t == "subscribe":
            client.topics |= topics
            if "nmea" in topics:
                client.nmea_last = None
        elif t == "unsubscribe":
            client.topics -= topics
        elif t == "ping":
            client.send(json.dumps({"type": "pong"}))


def new_nmea_lines(tail: list[str], last: str | None) -> list[str]:
    """Строки из tail, появившиеся после `last`. Нет last или не найден → последние 20."""
    if last is None:
        return tail[-20:]
    for i in range(len(tail) - 1, -1, -1):
        if tail[i] == last:
            return tail[i + 1:]
    return tail[-50:]
