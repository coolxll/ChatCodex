"""Authenticated WebChat event fan-out with bounded replay."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
import json
import time
from typing import Any, AsyncIterator, Optional


@dataclass(frozen=True)
class GatewayEvent:
    event_id: int
    event: str
    conversation_id: str
    data: dict[str, Any]
    created_at: float

    def as_sse(self) -> str:
        payload = {
            "eventId": self.event_id,
            "conversationId": self.conversation_id,
            **self.data,
        }
        return (
            f"id: {self.event_id}\n"
            f"event: {self.event}\n"
            f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
        )


class EventBroker:
    """One-way event stream for approvals and standalone operations."""

    def __init__(self, replay_limit: int = 256):
        self.replay_limit = max(16, replay_limit)
        self._next_id = 0
        self._history: dict[str, deque[GatewayEvent]] = defaultdict(
            lambda: deque(maxlen=self.replay_limit)
        )
        self._subscribers: dict[str, set[asyncio.Queue[GatewayEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(
            self, event: str, conversation_id: str, data: Optional[dict] = None
    ) -> GatewayEvent:
        conversation_id = str(conversation_id or "")
        async with self._lock:
            self._next_id += 1
            item = GatewayEvent(
                event_id=self._next_id,
                event=event,
                conversation_id=conversation_id,
                data=dict(data or {}),
                created_at=time.time(),
            )
            self._history[conversation_id].append(item)
            self._history["*"].append(item)
            queues = list(self._subscribers.get(conversation_id, ()))
            queues.extend(self._subscribers.get("*", ()))
        for queue in queues:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    pass
        return item

    async def subscribe(
            self, conversation_id: str, after_id: int = 0
    ) -> AsyncIterator[GatewayEvent]:
        conversation_id = str(conversation_id or "")
        queue: asyncio.Queue[GatewayEvent] = asyncio.Queue(maxsize=64)
        async with self._lock:
            replay = [
                item for item in self._history.get(conversation_id, ())
                if item.event_id > after_id
            ]
            self._subscribers[conversation_id].add(queue)
        try:
            for item in replay:
                yield item
            while True:
                yield await asyncio.wait_for(queue.get(), timeout=20.0)
        except asyncio.TimeoutError:
            # Let the endpoint send a heartbeat and create a fresh wait.
            return
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(conversation_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(conversation_id, None)
