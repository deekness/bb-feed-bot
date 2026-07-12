"""RSS source (Jokers Updates live-feed RSS)."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

import aiohttp
from urllib.parse import quote
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
    """Jokers RSS, with a proxy escape hatch.

    Jokers' host (169.61.62.206) refuses TCP connections from datacenter IPs —
    the feed serves fine from a home connection but Railway can't reach it at
    all. Crucially, rss./forums./www.jokersupdates.com ALL resolve to that one
    IP, so alternate hostnames buy nothing: they're three doors to the same
    locked building.

    The only fix is to make the request from a different IP. `proxy_templates`
    are URL patterns containing {url} (the URL-encoded feed address); a proxy
    service fetches Jokers from its own address and hands back the raw XML.

    Direct URLs are always tried FIRST, so if Jokers ever unblocks cloud IPs
    the bot silently goes back to the source and stops depending on a
    third party.
    """

    name = "rss"

    def __init__(self, url: str, timeout: int = 20,
                 fallback_urls: list[str] | None = None,
                 proxy_templates: list[str] | None = None):
        self.urls = [url] + list(fallback_urls or [])
        self.proxy_templates = list(proxy_templates or [])
        self.timeout = timeout
        self.consecutive_failures = 0   # read by the bot for source-health alerts
        self.using_proxy = False        # surfaced in /status
        self._active_url = url

    def _candidates(self) -> list[tuple[str, str, bool]]:
        """(label, url, is_proxy) — direct first, proxied only as a fallback."""
        direct = ([self._active_url] +
                  [u for u in self.urls if u != self._active_url])
        out = [(u, u, False) for u in direct]
        primary = self.urls[0]
        for tpl in self.proxy_templates:
            try:
                proxied = tpl.replace("{url}", quote(primary, safe=""))
            except Exception:
                continue
            out.append((f"proxy:{tpl.split('/')[2]}", proxied, True))
        return out

    async def fetch(self) -> list[Update]:
        raw = None
        errors: list[str] = []
        async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as session:
            for label, url, is_proxy in self._candidates():
                try:
                    async with session.get(url, timeout=self.timeout) as resp:
                        if resp.status != 200:
                            errors.append(f"{label} -> HTTP {resp.status}")
                            continue
                        body = await resp.read()
                    # A proxy can return 200 with an error page instead of the
                    # feed, so require it to actually look like RSS/XML.
                    if b"<rss" not in body[:600] and b"<?xml" not in body[:600]:
                        errors.append(f"{label} -> 200 but not XML")
                        continue
                    raw = body
                    if is_proxy and not self.using_proxy:
                        log.warning("RSS direct fetch blocked — falling back to %s",
                                    label)
                    elif not is_proxy and self.using_proxy:
                        log.info("RSS direct fetch working again — proxy dropped")
                    elif not is_proxy and self._active_url != url:
                        log.info("RSS switched to working URL: %s", url)
                    self.using_proxy = is_proxy
                    if not is_proxy:
                        self._active_url = url
                    break
                except Exception as e:
                    errors.append(f"{label} -> {e}")

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
