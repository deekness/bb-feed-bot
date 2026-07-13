"""Ingestion pipeline: poll all sources -> store -> return only NEW updates.

De-dup is delegated to the DB (atomic ON CONFLICT). Whatever comes back is
genuinely new and ready for extraction / summarization.

Sources set their OWN cadence. The loop ticks every 2 minutes for Bluesky (an
API built to be polled, and the fastest path for breaking news), but Jokers is
a small fan forum running aging forum software — hammering it 720x/day is both
rude and very likely what trips its datacenter-IP blocks. A source that isn't
due is simply skipped this tick.
"""
from __future__ import annotations

import logging
import time

from ..db import Database
from ..models import Update

log = logging.getLogger("bb.ingest.pipeline")


class IngestPipeline:
    def __init__(self, db: Database, sources: list):
        self.db = db
        self.sources = sources
        self._last_poll: dict[str, float] = {}   # source name -> monotonic ts

    def _due(self, src) -> bool:
        interval = getattr(src, "poll_interval_s", 0)
        if not interval:
            return True                      # no cadence set: poll every tick
        last = self._last_poll.get(src.name)
        if last is None:
            return True                      # first tick after boot
        return (time.monotonic() - last) >= interval

    async def run(self) -> list[Update]:
        collected: list[Update] = []
        for src in self.sources:
            if not self._due(src):
                continue
            self._last_poll[src.name] = time.monotonic()
            try:
                items = await src.fetch()
                collected.extend(items)
                log.debug("%s returned %d items", src.name, len(items))
            except Exception as e:
                log.error("source %s failed: %s", getattr(src, "name", "?"), e)

        new: list[Update] = []
        for u in collected:
            try:
                if await self.db.add_update(u):
                    new.append(u)
            except Exception as e:
                log.error("store failed for %s: %s", u.content_hash[:8], e)
        if new:
            log.info("ingested %d new updates (from %d fetched)", len(new), len(collected))
        return new
