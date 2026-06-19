"""RSS source (Jokers Updates live-feed RSS)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp
import feedparser

from ..models import Update
from .dedup import content_hash

log = logging.getLogger("bb.ingest.rss")


class RSSSource:
    name = "rss"

    def __init__(self, url: str, timeout: int = 20):
        self.url = url
        self.timeout = timeout

    async def fetch(self) -> list[Update]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url, timeout=self.timeout) as resp:
                    raw = await resp.read()
        except Exception as e:
            log.error("RSS fetch failed: %s", e)
            return []

        feed = feedparser.parse(raw)
        updates: list[Update] = []
        for entry in feed.entries:
            try:
                title = entry.get("title", "").strip()
                body = entry.get("description", "").strip()
                link = entry.get("link", "")
                published = self._published(entry)
                if not title and not body:
                    continue
                updates.append(Update(
                    content_hash=content_hash(title, body),
                    source=self.name,
                    author=entry.get("author", ""),
                    title=title, body=body, link=link, published_at=published,
                ))
            except Exception as e:
                log.error("RSS entry parse error: %s", e)
        return updates

    @staticmethod
    def _published(entry) -> datetime:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        return datetime.now(timezone.utc)
