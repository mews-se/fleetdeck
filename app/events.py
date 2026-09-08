"""In-process fan-out of events to SSE subscribers."""

import asyncio
import json


class EventBus:
    def __init__(self):
        self.queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.queues.discard(q)

    def publish(self, event: str, data: dict):
        message = (event, json.dumps(data, separators=(",", ":")))
        for q in list(self.queues):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                self.queues.discard(q)
