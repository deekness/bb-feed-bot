"""Watch a Bluesky account for the Block Buster competition preview.

Big Brother releases a preview clip for the eviction-night Block Buster comp,
and one account reliably posts it the moment it drops with the same wording
every time ("tonights BBBB! #BB28"). That is worth relaying immediately — it is
the only look anyone gets at the comp before it is played.

Deliberately narrow: one account, one text pattern, link only. Nothing here
touches game state or the summaries.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import aiohttp

log = logging.getLogger("bb.preview")

_PUBLIC_FEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"

# "tonights BBBB", "tonight's bbbb", "Tonights  BBBB!" — the apostrophe and
# spacing vary, the shape does not.
_PREVIEW = re.compile(r"tonight'?s?\s*bbbb", re.IGNORECASE)


def is_preview(text: str) -> bool:
    return bool(_PREVIEW.search(text or ""))


class PreviewWatcher:
    """Newest matching post from one account, or None."""

    def __init__(self, handle: str, timeout: int = 15):
        self.handle = handle.lstrip("@")
        self.timeout = timeout
        self.consecutive_failures = 0

    async def latest(self) -> dict | None:
        params = {"actor": self.handle, "limit": "20"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_PUBLIC_FEED, params=params,
                                       timeout=self.timeout) as resp:
                    if resp.status != 200:
                        self.consecutive_failures += 1
                        log.warning("preview feed HTTP %s", resp.status)
                        return None
                    data = await resp.json()
        except Exception as e:
            self.consecutive_failures += 1
            log.warning("preview feed error: %s", e)
            return None
        self.consecutive_failures = 0

        for item in data.get("feed", []):
            if item.get("reason"):
                continue                      # repost, not the original drop
            post = item.get("post", {})
            record = post.get("record", {})
            text = (record.get("text") or "").strip()
            if not is_preview(text):
                continue
            uri = post.get("uri", "")
            rkey = uri.split("/")[-1] if uri else ""
            if not rkey:
                continue
            created = record.get("createdAt")
            try:
                when = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if created else datetime.now(timezone.utc))
            except (ValueError, AttributeError):
                when = datetime.now(timezone.utc)
            return {
                "uri": uri,
                "text": text,
                "created_at": when,
                "url": f"https://bsky.app/profile/{self.handle}/post/{rkey}",
            }
        return None
