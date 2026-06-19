"""Ingestion pipeline: poll all sources -> store -> return only NEW updates.

De-dup is delegated to the DB (atomic ON CONFLICT). Whatever comes back is
genuinely new and ready for extraction / summarization.
"""
from __future__ import annotations

import logging

from ..db import Database
from ..models import Update

log = logging.getLogger("bb.ingest.pipeline")


class IngestPipeline:
    def __init__(self, db: Database, sources: list):
        self.db = db
        self.sources = sources

    async def run(self) -> list[Update]:
        collected: list[Update] = []
        for src in self.sources:
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
