"""Bluesky (AT Protocol) source — fully async via aiohttp.

Auth is optional. If credentials are absent, the source quietly returns
nothing. Relevance is decided by the season's keywords OR any roster name
appearing in the post, so it adapts to a new cast with no code change.

Session handling: createSession is heavily rate-limited by Bluesky
(30/5min, 300/day — a 2-minute poll would burn 720/day). So we create a
session ONCE, reuse the access token, refresh it via refreshSession when it
expires, and only fall back to a fresh createSession if the refresh fails.

GOTCHA: an expired access token comes back as HTTP **400** with an error body
of ExpiredToken — not 401. Access tokens last ~2 hours, so mishandling this
silently kills every feed two hours after startup.
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
        self._access: str | None = None
        self._refresh: str | None = None
        self._roster_res: list[re.Pattern] | None = None
        self._roster_key: tuple = ()

    async def fetch(self) -> list[Update]:
        if not (self.username and self.password):
            return []
        async with aiohttp.ClientSession() as session:
            if not await self._ensure_session(session):
                return []
            updates: list[Update] = []
            for handle in self.accounts:
                try:
                    updates.extend(await self._fetch_account(session, handle))
                except Exception as e:
                    log.error("Bluesky fetch failed for %s: %s", handle, e)
            return updates

    # --- session lifecycle ---------------------------------------------------
    async def _ensure_session(self, session: aiohttp.ClientSession) -> bool:
        if self._access:
            return True
        if self._refresh and await self._refresh_session(session):
            return True
        return await self._create_session(session)

    async def _create_session(self, session: aiohttp.ClientSession) -> bool:
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
                self._access = data.get("accessJwt")
                self._refresh = data.get("refreshJwt")
                log.info("Bluesky session created")
                return self._access is not None
        except Exception as e:
            log.error("Bluesky auth error: %s", e)
            return False

    async def _refresh_session(self, session: aiohttp.ClientSession) -> bool:
        try:
            async with session.post(
                f"{_BASE}/com.atproto.server.refreshSession",
                headers={"Authorization": f"Bearer {self._refresh}"},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    log.warning("Bluesky refresh failed: HTTP %s", resp.status)
                    self._refresh = None
                    return False
                data = await resp.json()
                self._access = data.get("accessJwt")
                self._refresh = data.get("refreshJwt") or self._refresh
                log.info("Bluesky session refreshed")
                return self._access is not None
        except Exception as e:
            log.warning("Bluesky refresh error: %s", e)
            self._refresh = None
            return False

    # --- fetching -------------------------------------------------------------
    async def _fetch_account(self, session: aiohttp.ClientSession, handle: str,
                             retried: bool = False) -> list[Update]:
        headers = {"Authorization": f"Bearer {self._access}"}
        async with session.get(
            f"{_BASE}/app.bsky.feed.getAuthorFeed",
            params={"actor": handle, "limit": 25, "filter": "posts_no_replies"},
            headers=headers, timeout=15,
        ) as resp:
            # AT Protocol signals an expired access token with HTTP 400 and an
            # error body of ExpiredToken/InvalidToken — NOT 401. Only handling
            # 401 meant every feed 400'd forever once the ~2h token lapsed, and
            # the refreshSession path below was never reached.
            expired = resp.status == 401
            if resp.status == 400:
                try:
                    err = (await resp.json()).get("error", "")
                except Exception:
                    err = ""
                expired = err in ("ExpiredToken", "InvalidToken", "AuthMissing")
                if not expired:
                    log.warning("Bluesky feed %s: HTTP 400 (%s)", handle, err or "?")
                    return []
            if expired and not retried:
                log.info("Bluesky token expired — refreshing session")
                self._access = None
                if await self._ensure_session(session):
                    return await self._fetch_account(session, handle, retried=True)
                return []
            if resp.status != 200:
                log.warning("Bluesky feed %s: HTTP %s", handle, resp.status)
                return []
            data = await resp.json()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        out: list[Update] = []
        for item in data.get("feed", []):
            if item.get("reason"):
                continue  # repost of someone else's content — skip
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

    # --- relevance --------------------------------------------------------------
    def _roster_patterns(self) -> list[re.Pattern]:
        """Word-boundary patterns for every canonical name AND every nickname,
        so a Bluesky post that only says "Rick", "Lala", or "Salina" still
        clears the relevance gate instead of being dropped before extraction.
        Cache key includes nicknames so runtime /addnickname recompiles it."""
        names = self.roster.names
        nicks = sorted(self.roster.nicknames.keys())
        key = (tuple(names), tuple(nicks))
        if self._roster_res is None or key != self._roster_key:
            self._roster_res = [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
                                for t in list(names) + nicks]
            self._roster_key = key
        return self._roster_res

    def _is_relevant(self, text: str) -> bool:
        low = text.lower()
        if sum(1 for s in _SPAM if s in low) >= 2:
            return False
        if any(k in low for k in self.keywords):
            return True
        return any(p.search(text) for p in self._roster_patterns())

    @staticmethod
    def _created(value: str | None) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
