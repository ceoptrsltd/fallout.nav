"""WebSocket /ws: рассылка телеметрии, настроек, событий и NMEA.

Гость (только просмотр, fnav/core/guests.py) получает то же, но сообщения проходят через `guest_filter` (настройки —
без секретов), а из его команд выполняются только команды чтения (READ_COMMANDS); остальные — ответ
`{"type":"error","status":403,"detail":"Read-only guest access"}`. Новая команда для гостя закрыта, пока её не
добавят в READ_COMMANDS. Гостевое соединение закрывается, когда истекает его ссылка (проверка с каждым кадром
телеметрии) или её отзывают (drop_guests)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fnav.core.util import cancel_tasks

log = logging.getLogger("fnav.core.ws")

QUEUE_MAX = 50  # 5 с телеметрии; клиент, который не успевает, отключаем
READ_COMMANDS = frozenset({"subscribe", "unsubscribe", "ping"})  # команды, которые можно гостю
GUEST_DENIED = "Read-only guest access"


class Client:
    def __init__(self, ws: WebSocket, telemetry_every: int = 1, guest: str | None = None,
                 guest_until: float | None = None):
        self.ws = ws
        self.guest = guest is not None  # гостевая сессия: только просмотр
        self.guest_id = guest  # id гостевой ссылки
        self.guest_until = guest_until  # срок ссылки (unix): после него соединение закрывается
        self.telemetry_every = max(1, telemetry_every)  # 1 — каждый кадр телеметрии, 5 — каждый пятый
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

    def close(self) -> None:
        """Закрыть со стороны сервера: отправитель доходит до None и закрывает сокет."""
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(None)


class WsManager:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        # сообщение для гостя (без секретов); None — как всем (fnav/core/guests.py redact_message)
        self.guest_filter: Callable[[dict], dict] | None = None

    def __len__(self) -> int:
        return len(self.clients)

    def broadcast_text(self, text: str, topic: str | None = None) -> None:
        for c in list(self.clients):
            if topic is None or topic in c.topics:
                c.send(text)

    def broadcast_telemetry(self, text: str, tick: int) -> None:
        now = time.time()
        for c in list(self.clients):
            if c.guest_until is not None and now >= c.guest_until:
                c.close()  # гостевая ссылка истекла
            elif tick % c.telemetry_every == 0:
                c.send(text)

    def drop_guests(self, active: Callable[[str], bool]) -> None:
        """Закрыть гостевые соединения, чьи ссылки больше не действуют (отозваны)."""
        for c in list(self.clients):
            if c.guest and not active(c.guest_id or ""):
                c.close()

    def broadcast(self, msg: dict[str, Any], topic: str | None = None) -> None:
        if not self.clients:
            return
        text = json.dumps(msg, ensure_ascii=False)
        guest_text: str | None = None
        for c in list(self.clients):
            if topic is not None and topic not in c.topics:
                continue
            if c.guest and self.guest_filter is not None:
                if guest_text is None:
                    gm = self.guest_filter(msg)
                    guest_text = text if gm is msg else json.dumps(gm, ensure_ascii=False)
                c.send(guest_text)
            else:
                c.send(text)

    def event(self, event: str, **fields: Any) -> None:
        self.broadcast({"type": "event", "event": event, **fields})

    def toast(self, text: str, level: str = "info") -> None:
        self.event("toast", level=level, text=text)

    def subscribers(self, topic: str) -> list[Client]:
        return [c for c in self.clients if topic in c.topics]

    async def serve(self, ws: WebSocket, hello: list[str], telemetry_every: int = 1, guest: str | None = None,
                    guest_until: float | None = None) -> None:
        """hello — уже готовые для этого клиента сообщения (для гостя — без секретов); guest — id гостевой ссылки."""
        await ws.accept()
        client = Client(ws, telemetry_every, guest, guest_until)
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
        if client.guest and t not in READ_COMMANDS:  # гость: только чтение, любая другая команда — отказ
            client.send(json.dumps({"type": "error", "status": 403, "detail": GUEST_DENIED,
                                    "command": str(t)[:40]}))
            return
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
