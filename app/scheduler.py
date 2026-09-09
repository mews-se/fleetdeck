"""One asyncio task per source. A failing source never blocks the others."""

import asyncio
import logging
import random
import time

from app import attention
from app.db import Database
from app.events import EventBus
from app.sources import Context, Skip, Source, SourceError

log = logging.getLogger("fleetdeck.scheduler")

ATTENTION_THROTTLE = 10
PRUNE_INTERVAL = 3600
VACUUM_INTERVAL = 86400


class Scheduler:
    def __init__(self, ctx: Context, sources: list[Source], events: EventBus):
        self.ctx = ctx
        self.db: Database = ctx.db
        self.sources = sources
        self.events = events
        self.tasks: list[asyncio.Task] = []
        self.status: dict[str, str] = {}
        self._attention_due = 0.0
        self._attention_lock = asyncio.Lock()

    def start(self):
        for s in self.sources:
            self.tasks.append(asyncio.create_task(self._loop(s), name=f"source:{s.name}"))
        self.tasks.append(asyncio.create_task(self._housekeeping(), name="housekeeping"))

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _loop(self, source: Source):
        await asyncio.sleep(random.uniform(0, min(5.0, source.interval / 4)))
        while True:
            started = time.monotonic()
            await self._tick(source)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, source.interval - elapsed))

    async def _tick(self, source: Source):
        started = time.monotonic()
        state, error = "ok", None
        try:
            async with asyncio.timeout(source.timeout):
                await source.collect()
        except Skip as e:
            state, error = "skipped", str(e)
        except asyncio.CancelledError:
            raise
        except SourceError as e:
            state, error = "error", str(e)
        except Exception as e:
            state, error = "error", f"{type(e).__name__}: {e}"
        duration = int((time.monotonic() - started) * 1000)
        self.db.record_run(
            source.name, state == "ok", duration, error, skipped=(state == "skipped")
        )
        if state == "error":
            log.warning("%s: %s", source.name, error)
        if self.status.get(source.name) != state:
            self.status[source.name] = state
            self.events.publish("source", {"source": source.name, "state": state, "error": error})
        await self.refresh_attention()

    async def refresh_attention(self, force: bool = False):
        if not force and time.monotonic() < self._attention_due:
            return
        async with self._attention_lock:
            self._attention_due = time.monotonic() + ATTENTION_THROTTLE
            try:
                active = attention.compute(self.db, self.ctx.config, self.ctx.now())
                opened, cleared = self.db.sync_attention(active)
            except Exception:
                log.exception("attention rules failed")
                return
            if opened or cleared:
                self.events.publish(
                    "attention",
                    {"opened": [f"{s}:{k}" for s, k in opened],
                     "cleared": [f"{s}:{k}" for s, k in cleared],
                     "open": len(self.db.unacked_attention())},
                )

    async def _housekeeping(self):
        last_vacuum = time.monotonic()
        while True:
            await asyncio.sleep(PRUNE_INTERVAL)
            try:
                self.db.prune()
                if time.monotonic() - last_vacuum >= VACUUM_INTERVAL:
                    self.db.vacuum()
                    last_vacuum = time.monotonic()
            except Exception:
                log.exception("housekeeping failed")
