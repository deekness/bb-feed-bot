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

import json
import logging
import time
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
_HASHTAG = re.compile(r"(?:(?<=\s)|^)#\w+")


def strip_hashtags(text: str) -> str:
    """The upstream post verbatim, minus its hashtags (#BB28 etc.)."""
    out = _HASHTAG.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(ln.rstrip() for ln in out.splitlines()).strip()

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


_HOURS = re.compile(r"(\d+)\s*(?:hours?|hrs?|h)\b", re.IGNORECASE)
_MINS = re.compile(r"(\d+)\s*(?:minutes?|mins?|m)\b", re.IGNORECASE)


def duration_minutes(text: str) -> int | None:
    """The outage length in whole minutes, or None if the post doesn't say.

    Handles the shapes the upstream account actually posts: "7 mins",
    "1 hour 8 mins", "2 hours", "45 minutes".
    """
    raw = duration_in(text)
    if not raw:
        return None
    hours = sum(int(h) for h in _HOURS.findall(raw))
    mins = sum(int(m) for m in _MINS.findall(raw))
    total = hours * 60 + mins
    return total if total > 0 else None


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class FeedStateApi:
    """FeedBot's own status file — the fastest signal available.

    The Bluesky account is a *notification* the operator's checker emits after
    it detects a change; this JSON is what the checker itself writes, so it
    flips first (observed: site showing feeds back while the post had not
    appeared). Tiny and ETag'd, so most polls cost a 304.

        {"current": 1785443423, "status": "up", "since": 1785443063, ...}

    `since` is the moment the CURRENT state began, so an outage's length is
    exact arithmetic rather than text parsed out of a post.
    """

    def __init__(self, url: str):
        self.url = url
        self._etag: str | None = None
        self.consecutive_failures = 0
        self.unchanged_polls = 0        # watchdog: a stale cache looks like this

    async def fetch(self) -> dict | None:
        """{'state', 'since' (aware UTC), 'raw'} or None if unchanged/failed."""
        # It is a static asset on a CDN. Conditional GETs and edge caching kept
        # handing back a stale "down" for an entire 3h46m outage — the bot never
        # saw the feeds return. Ask for a fresh copy every time: the file is
        # ~300 bytes, so freshness is worth far more than the saved bytes.
        headers = {"User-Agent": _UA, "Cache-Control": "no-cache",
                   "Pragma": "no-cache"}
        url = f"{self.url}{'&' if '?' in self.url else '?'}t={int(time.time())}"
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status == 304:
                        self.consecutive_failures = 0
                        self.unchanged_polls += 1
                        return None                 # unchanged since last poll
                    if resp.status != 200:
                        self.consecutive_failures += 1
                        log.warning("feed-state API HTTP %s", resp.status)
                        return None
                    self._etag = resp.headers.get("ETag")
                    data = json.loads(await resp.text())
        except Exception as e:
            self.consecutive_failures += 1
            log.warning("feed-state API failed: %s", e)
            return None
        self.consecutive_failures = 0
        status = str(data.get("status", "")).strip().lower()
        since = data.get("since")
        if not status or not isinstance(since, (int, float)):
            return None
        self.unchanged_polls = 0
        return {
            # Anything that isn't "up" means the feeds are not watchable. The
            # file doesn't distinguish anipals from WBRB; the Bluesky post does,
            # and it arrives shortly after.
            "state": STATE_LIVE if status == "up" else STATE_WBRB,
            "since": datetime.fromtimestamp(float(since), tz=timezone.utc),
            "raw": status,
        }


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
