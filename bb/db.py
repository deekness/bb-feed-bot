"""Async PostgreSQL layer (asyncpg).

Design choices that fix old-bot pain:
  * asyncpg pool: every query is awaited and never blocks the event loop.
  * Rows are Records (dict-like by column name) everywhere — no tuple/dict
    branching, which is where most of the old PG bugs lived.
  * The `updates` table is the source of truth for summaries, so there is no
    separate in-memory queue to persist/restore.
  * De-dup is atomic via INSERT ... ON CONFLICT DO NOTHING RETURNING.
"""
from __future__ import annotations

import logging
from datetime import datetime

import asyncpg

from .models import Update

log = logging.getLogger("bb.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS updates (
    id            BIGSERIAL PRIMARY KEY,
    content_hash  TEXT UNIQUE NOT NULL,
    source        TEXT NOT NULL,
    author        TEXT DEFAULT '',
    title         TEXT NOT NULL,
    body          TEXT DEFAULT '',
    link          TEXT DEFAULT '',
    published_at  TIMESTAMPTZ NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_updates_published ON updates(published_at);

CREATE TABLE IF NOT EXISTS alliances (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT,
    status      TEXT NOT NULL DEFAULT 'forming',
    confidence  REAL NOT NULL DEFAULT 0,
    locked      BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alliance_members (
    alliance_id BIGINT NOT NULL REFERENCES alliances(id) ON DELETE CASCADE,
    houseguest  TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (alliance_id, houseguest)
);

CREATE TABLE IF NOT EXISTS alliance_evidence (
    id          BIGSERIAL PRIMARY KEY,
    alliance_id BIGINT NOT NULL REFERENCES alliances(id) ON DELETE CASCADE,
    quote       TEXT,
    confidence  REAL,
    source_hash TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS relationships (
    hg_a       TEXT NOT NULL,
    hg_b       TEXT NOT NULL,
    affinity   REAL NOT NULL DEFAULT 0,
    label      TEXT,
    last_event TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hg_a, hg_b)
);

CREATE TABLE IF NOT EXISTS game_state (
    week        INT NOT NULL,
    role        TEXT NOT NULL,
    houseguest  TEXT NOT NULL,
    confidence  REAL DEFAULT 0,
    source_hash TEXT,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (week, role, houseguest)
);

CREATE TABLE IF NOT EXISTS bot_kv (
    key        TEXT PRIMARY KEY,
    value      JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self, min_size: int = 2, max_size: int = 10) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=min_size, max_size=max_size)
        await self.init_schema()
        log.info("PostgreSQL pool ready (%s-%s connections)", min_size, max_size)

    async def init_schema(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # --- thin query helpers (Records are dict-like by column name) ----------
    async def fetch(self, sql: str, *args):
        return await self.pool.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self.pool.fetchval(sql, *args)

    async def execute(self, sql: str, *args) -> str:
        return await self.pool.execute(sql, *args)

    # --- updates ------------------------------------------------------------
    async def add_update(self, u: Update) -> bool:
        """Insert an update. Returns True if newly inserted, False if duplicate."""
        row = await self.fetchrow(
            """
            INSERT INTO updates (content_hash, source, author, title, body, link, published_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING id
            """,
            u.content_hash, u.source, u.author, u.title, u.body, u.link, u.published_at,
        )
        return row is not None

    async def updates_between(self, start: datetime, end: datetime) -> list[Update]:
        rows = await self.fetch(
            """
            SELECT content_hash, source, author, title, body, link, published_at
            FROM updates WHERE published_at >= $1 AND published_at < $2
            ORDER BY published_at ASC
            """,
            start, end,
        )
        return [self._to_update(r) for r in rows]

    async def recent_updates(self, hours: int) -> list[Update]:
        rows = await self.fetch(
            """
            SELECT content_hash, source, author, title, body, link, published_at
            FROM updates WHERE published_at > now() - make_interval(hours => $1)
            ORDER BY published_at DESC
            """,
            hours,
        )
        return [self._to_update(r) for r in rows]

    @staticmethod
    def _to_update(r) -> Update:
        return Update(
            content_hash=r["content_hash"], source=r["source"], author=r["author"],
            title=r["title"], body=r["body"], link=r["link"], published_at=r["published_at"],
        )

    # --- key/value (channel id, last-run markers, etc.) ---------------------
    async def kv_get(self, key: str):
        val = await self.fetchval("SELECT value FROM bot_kv WHERE key = $1", key)
        return val

    async def kv_set(self, key: str, value) -> None:
        import json
        await self.execute(
            """
            INSERT INTO bot_kv (key, value, updated_at) VALUES ($1, $2::jsonb, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            key, json.dumps(value),
        )
