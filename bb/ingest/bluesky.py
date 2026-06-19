"""Bluesky (AT Protocol) source — fully async via aiohttp.

Auth is optional. If credentials are absent, the source quietly returns
nothing. Relevance is decided by the season's keywords OR any roster name
appearing in the post, so it adapts to a new cast with no code change.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp

from ..models import Update
from ..roster import Roster
from .dedup import content_hash

log = logging.getLogger("bb.ingest.bluesky")

_BASE = "https://bsky.social/xrpc"
_WS = re.compile(r"\s+")
_SPAM = ("subscribe", "follow me", "link in bio", "patreon", "donate",
         "use code", "check out my", "buy my")


class BlueskySource:
    name = "bluesky"

    def __init__(self, accounts: list[str], roster: Roster, keywords: list[str],
                 username: str | None = None, password: str | None = None,
                 lookback_hours: int = 6):
        self.accounts = accounts
        self.roster = roster
        self.keywords = keywords
        self.username = username or os.getenv("BLUESKY_USERNAME")
        self.password = password or os.getenv("BLUESKY_PASSWORD")
        self.lookback_hours = lookback_hours
        self._token: str | None = None

    async def fetch(self) -> list[Update]:
        if not (self.username and self.password):
            return []
        async with aiohttp.ClientSession() as session:
            if not await self._auth(session):
                return []
            updates: list[Update] = []
            for handle in self.accounts:
                try:
                    updates.extend(await self._fetch_account(session, handle))
                except Exception as e:
                    log.error("Bluesky fetch failed for %s: %s", handle, e)
            return updates

    async def _auth(self, session: aiohttp.ClientSession) -> bool:
        try:
            async with session.post(
                f"{_BASE}/com.atproto.server.createSession",
                json={"identifier": self.username, "password": self.password},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    log.error("Bluesky auth failed: HTTP %s", resp.status)
                    return False
                data = await resp.json()
                self._token = data.get("accessJwt")
                return self._token is not None
        except Exception as e:
            log.error("Bluesky auth error: %s", e)
            return False

    async def _fetch_account(self, session: aiohttp.ClientSession, handle: str) -> list[Update]:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with session.get(
            f"{_BASE}/app.bsky.feed.getAuthorFeed",
            params={"actor": handle, "limit": 25, "filter": "posts_no_replies"},
            headers=headers, timeout=15,
        ) as resp:
            if resp.status != 200:
                log.warning("Bluesky feed %s: HTTP %s", handle, resp.status)
                return []
            data = await resp.json()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        out: list[Update] = []
        for item in data.get("feed", []):
            post = item.get("post", {})
            record = post.get("record", {})
            text = (record.get("text") or "").strip()
            if not text:
                continue
            created = self._created(record.get("createdAt"))
            if created < cutoff or not self._is_relevant(text):
                continue
            clean = _WS.sub(" ", text).strip()
            uri = post.get("uri", "")
            link = f"https://bsky.app/profile/{handle}/post/{uri.split('/')[-1]}" if uri else ""
            out.append(Update(
                content_hash=content_hash(clean, ""),
                source=self.name,
                author=f"@{handle.split('.')[0]}",
                title=clean, body="", link=link, published_at=created,
            ))
        return out

    def _is_relevant(self, text: str) -> bool:
        low = text.lower()
        if sum(1 for s in _SPAM if s in low) >= 2:
            return False
        if any(k in low for k in self.keywords):
            return True
        return any(self.roster.contains(name) for name in self.roster.names if name.lower() in low)

    @staticmethod
    def _created(value: str | None) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
