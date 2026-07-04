"""Live-feed state monitor — are the feeds LIVE, on Anipals, or WBRB?

We do not watch the Paramount+ stream ourselves (auth-heavy, fragile, and
changes every season). Instead we consume @feed-bot.bsky.social — a public
tracker that watches feed elements server-side and posts state transitions
("🚨 Feeds are back. (Duration: 8 mins) 🚨"). We classify its posts, keep the
current state in bot_kv, and announce transitions in the update channel with
attribution. If that account goes quiet (its own site warns each season start
is a scramble), we simply have no signal — the bot never guesses.

Uses the PUBLIC AppView endpoint, unauthenticated: no session cost, works even
without Bluesky credentials, and one small request per minute is well within
public limits.

State classification is keyword-based and intentionally editable at the top of
this file — if the upstream wording changes for BB28, tune the tuples and
redeploy.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("bb.ingest.feedstate")

_PUBLIC_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

# Order matters: first match wins. "back" is checked before the down-states so
# a post like "feeds are back after WBRB" classifies as live.
_LIVE_MARKERS = ("feeds are back", "feeds are live", "back to live", "feeds returned")
_ANIPALS_MARKERS = ("anipal",)  # covers Anipals / AniPals / anipal cam
_WBRB_MARKERS = ("wbrb", "we'll be right back", "we will be right back",
                 "hush hush", "feeds are down", "feeds went down",
                 "feeds cut", "feeds down")

_DURATION = re.compile(r"duration:\s*([^)\n]+)", re.IGNORECASE)

STATE_LIVE = "live"
STATE_ANIPALS = "anipals"
STATE_WBRB = "wbrb"


def classify(text: str) -> str | None:
    """Map an upstream post to a feed state, or None for non-state posts
    (season stats, announcements, 'testing things out...')."""
    low = text.lower()
    if any(m in low for m in _LIVE_MARKERS):
        return STATE_LIVE
    if any(m in low for m in _ANIPALS_MARKERS):
        return STATE_ANIPALS
    if any(m in low for m in _WBRB_MARKERS):
        return STATE_WBRB
    return None


def duration_in(text: str) -> str | None:
    """Pull the human-readable duration out of a 'Feeds are back' post."""
    m = _DURATION.search(text)
    return m.group(1).strip() if m else None


class FeedStateMonitor:
    def __init__(self, handle: str):
        self.handle = handle

    async def fetch_signal(self) -> dict | None:
        """Newest classifiable state post from the upstream account, or None.

        Returns {state, text, created_at (aware UTC), post_url}.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _PUBLIC_FEED,
                    params={"actor": self.handle, "limit": 15,
                            "filter": "posts_no_replies"},
                    timeout=15,
                ) as resp:
                    if resp.status != 200:
                        log.warning("feedstate fetch: HTTP %s", resp.status)
                        return None
                    data = await resp.json()
        except Exception as e:
            log.warning("feedstate fetch error: %s", e)
            return None

        for item in data.get("feed", []):
            if item.get("reason"):
                continue  # pinned post or repost — not a fresh signal
            post = item.get("post", {})
            record = post.get("record", {})
            text = (record.get("text") or "").strip()
            state = classify(text)
            if not state:
                continue
            uri = post.get("uri", "")
            rkey = uri.split("/")[-1] if uri else ""
            return {
                "state": state,
                "text": text,
                "created_at": self._created(record.get("createdAt")),
                "post_url": (f"https://bsky.app/profile/{self.handle}/post/{rkey}"
                             if rkey else ""),
            }
        return None

    @staticmethod
    def _created(value: str | None) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
