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


# Some hosts refuse connections from datacenter IPs or from clients with no
# User-Agent. Present as a normal browser.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class RSSSource:
    name = "rss"

    def __init__(self, url: str, timeout: int = 20,
                 fallback_urls: list[str] | None = None):
        # Candidate feed URLs, tried in order. The primary host has been seen to
        # refuse TCP connections from Railway while the site itself is up, so a
        # mirror/alternate path keeps the richest source alive.
        self.urls = [url] + list(fallback_urls or [])
        self.timeout = timeout
        self.consecutive_failures = 0   # read by the bot for source-health alerts
        self._active_url = url

    async def fetch(self) -> list[Update]:
        raw = None
        errors: list[str] = []
        async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as session:
            # Try the URL that worked last time first, then the rest.
            ordered = ([self._active_url] +
                       [u for u in self.urls if u != self._active_url])
            for url in ordered:
                try:
                    async with session.get(url, timeout=self.timeout) as resp:
                        if resp.status != 200:
                            errors.append(f"{url} -> HTTP {resp.status}")
                            continue
                        raw = await resp.read()
                    if self._active_url != url:
                        log.info("RSS switched to working URL: %s", url)
                        self._active_url = url
                    break
                except Exception as e:
                    errors.append(f"{url} -> {e}")

        if raw is None:
            self.consecutive_failures += 1
            log.error("RSS fetch failed (%d in a row): %s",
                      self.consecutive_failures, "; ".join(errors))
            return []
        self.consecutive_failures = 0

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
