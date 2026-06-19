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


def content_hash(title: str, body: str) -> str:
    raw = f"{title or ''} {body or ''}".lower()
    raw = _TIME.sub("", raw)
    raw = _DATE.sub("", raw)
    raw = _URL.sub("", raw)
    raw = _HANDLE.sub("", raw)
    raw = _WS.sub(" ", raw).strip()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
