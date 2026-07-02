"""RSS source (Jokers Updates live-feed RSS)."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

import aiohttp
import feedparser

from ..models import Update
from .dedup import content_hash, hash_from_uid

log = logging.getLogger("bb.ingest.rss")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean_html(text: str) -> str:
    """Strip tags and unescape entities so downstream text is plain."""
    if not text:
        return ""
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return _WS.sub(" ", text).strip()


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
                title = _clean_html(entry.get("title", ""))
                body = _clean_html(entry.get("description", ""))
                link = entry.get("link", "")
                published = self._published(entry)
                if not title and not body:
                    continue
                # Prefer the feed's stable per-item ID: distinct events with
                # similar wording (constant on live feeds) must not collide.
                uid = entry.get("id") or entry.get("guid") or link
                h = hash_from_uid(uid) if uid else content_hash(
                    title, f"{body}|{published.isoformat()}")
                updates.append(Update(
                    content_hash=h,
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
