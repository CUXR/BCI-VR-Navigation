"""Fan-out of backend events to every connected `/events` WebSocket."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

_QUEUE_SIZE = 1000


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, **payload) -> None:
        """Publish from the event loop thread."""
        event = {"type": event_type, "ts": time.time(), **payload}
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping event for slow subscriber: %s", event_type)

    def publish_threadsafe(self, event_type: str, **payload) -> None:
        """Publish from any thread (e.g. the board poll thread)."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: self.publish(event_type, **payload))
