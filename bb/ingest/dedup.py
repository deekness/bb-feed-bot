"""Content hashing for de-duplication.

We normalize away volatile bits (timestamps, dates, urls, @handles, #tags,
whitespace) so the same event reported twice — or once via RSS and once via
Bluesky — collapses to one hash. The DB's UNIQUE(content_hash) is the final
authority; this just produces the key.
"""
from __future__ import annotations

import hashlib
import re

_TIME = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.IGNORECASE)
_DATE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_URL = re.compile(r"https?://\S+")
_HANDLE = re.compile(r"[@#]\w+")
_WS = re.compile(r"\s+")


def hash_from_uid(uid: str) -> str:
    """Hash a source-provided unique ID (RSS GUID/link). Preferred when the
    source has stable per-item IDs: unlike text normalization, two distinct
    events with similar wording (common on live-feed updates) never collide."""
    return hashlib.md5(uid.strip().encode("utf-8")).hexdigest()


def content_hash(title: str, body: str) -> str:
    raw = f"{title or ''} {body or ''}".lower()
    raw = _TIME.sub("", raw)
    raw = _DATE.sub("", raw)
    raw = _URL.sub("", raw)
    raw = _HANDLE.sub("", raw)
    raw = _WS.sub(" ", raw).strip()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
