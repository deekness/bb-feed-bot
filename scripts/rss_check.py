"""One-shot Jokers RSS sanity check (audit item #9).

The bot's dedup prefers the feed's per-item ID (<id>/<guid>/<link>). If that
ID turned out to be per-THREAD instead of per-POST, dedup would collapse every
item after the first and the bot would silently drop almost the whole feed.
This verifies the assumption against live feed data.

Run from the repo root on day 1 of feeds (or any time):
    python scripts/rss_check.py

Healthy output:  unique uids == entries  and  unique hashes == entries.
Broken output:   duplicate UIDs listed, with the one-line fix to apply.
"""
from __future__ import annotations

import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import feedparser  # noqa: E402

from bb.ingest.dedup import content_hash, hash_from_uid  # noqa: E402

URL = "https://rss.jokersupdates.com/ubbthreads/rss/bbusaupdates/rss.php"


def main() -> None:
    raw = urllib.request.urlopen(URL, timeout=20).read()
    feed = feedparser.parse(raw)
    entries = feed.entries
    print(f"entries fetched:      {len(entries)}")
    if not entries:
        print("Feed returned no entries — nothing to check (off-season?).")
        return

    uids, hashes = [], []
    for e in entries:
        uid = e.get("id") or e.get("guid") or e.get("link", "")
        uids.append(uid)
        hashes.append(hash_from_uid(uid) if uid else
                      content_hash(e.get("title", ""), e.get("description", "")))

    print(f"unique uids:          {len(set(uids))}")
    print(f"unique content_hash:  {len(set(hashes))}")

    dupes = [u for u, c in Counter(uids).items() if c > 1]
    if dupes:
        print("\nDUPLICATE UIDs — dedup would silently drop items. Samples:")
        for d in dupes[:5]:
            print("   ", d)
        print("\nFix: in bb/ingest/rss.py, stop preferring hash_from_uid(uid) and "
              "always use content_hash(title, f\"{body}|{published.isoformat()}\").")
    else:
        print("\nOK: per-item UIDs are unique — dedup is safe as written.")

    print(f"\nsample uid:   {uids[0]}")
    print(f"sample title: {entries[0].get('title', '')[:110]}")


if __name__ == "__main__":
    main()
