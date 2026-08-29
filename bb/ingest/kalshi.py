"""Kalshi winner-market watcher — a leak detector for blackout weeks.

When the feeds go dark for days, the house still knows what happened and so do
the people who work on the show. If a result leaks, it tends to show up in the
betting market before it shows up anywhere a fan can read: the evicted player's
"will win the season" price falls, and it falls on volume.

This polls the public market-data endpoint (no auth needed) and reports a drop
that is BIG and BOUGHT — a price move on two contracts is one person with an
opinion, not information.

Deliberately conservative. Reality-TV markets are thin, so the default
thresholds are high; a false "someone leaked" alert is worse than a missed one,
because it spoils a result that may not even be real.
"""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("bb.kalshi")

_BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"
_UA = "ChenBot/1.0 (Big Brother Discord bot)"



def _cents(value) -> int | None:
    """'0.1900' -> 19. Kalshi quotes dollars as strings; a price in cents is
    the implied probability, which is what the alerts reason about."""
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class KalshiWatcher:
    def __init__(self, event_ticker: str, roster=None, timeout: int = 20):
        self.event_ticker = event_ticker
        self.roster = roster
        self.timeout = timeout
        self.consecutive_failures = 0

    async def fetch(self) -> dict[str, dict] | None:
        """{houseguest: {'price', 'volume', 'volume_24h', 'ticker'}} or None.

        Prices are cents and map directly to implied probability, so a move
        from 24 to 11 is a 13-point drop in their chance of winning.
        """
        # Try with the status filter first, then without: if the event's markets
        # aren't in the state we asked for, the API happily returns an empty
        # list with a 200, which used to look exactly like "working, nothing to
        # report".
        attempts = [
            {"event_ticker": self.event_ticker, "status": "open", "limit": "200"},
            {"event_ticker": self.event_ticker, "limit": "200"},
        ]
        markets = None
        for params in attempts:
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as sess:
                    async with sess.get(_BASE, params=params,
                                        timeout=self.timeout) as resp:
                        if resp.status != 200:
                            self.consecutive_failures += 1
                            log.warning("kalshi HTTP %s (params=%s)",
                                        resp.status, params)
                            return None
                        data = await resp.json()
            except Exception as e:
                self.consecutive_failures += 1
                log.warning("kalshi fetch failed: %s", e)
                return None
            got = data.get("markets")
            if not isinstance(got, list):
                log.warning("kalshi: unexpected payload shape — keys=%s",
                            list(data)[:8])
                return None
            if got:
                markets = got
                break
        self.consecutive_failures = 0

        if not markets:
            # Silence here is what wasted a deploy cycle. Say so.
            log.warning("kalshi: 0 markets returned for event %r — the event "
                        "ticker is probably wrong", self.event_ticker)
            return None

        out: dict[str, dict] = {}
        for m in markets:
            if not isinstance(m, dict):
                continue
            who = self._houseguest(m)
            if not who:
                continue
            # Kalshi returns DOLLAR STRINGS ("0.1900") in *_dollars fields and
            # volumes in *_fp fields. The older integer-cent names are kept as
            # a fallback in case the shape changes back.
            price = _cents(m.get("last_price_dollars"))
            if price is None:
                price = _cents(m.get("yes_bid_dollars"))
            if price is None:
                price = _int(m.get("last_price"))
            if price is None:
                continue
            out[who] = {
                "ticker": str(m.get("ticker", "")),
                "price": price,
                "volume": _int(m.get("volume_fp")) or _int(m.get("volume")) or 0,
                "volume_24h": (_int(m.get("volume_24h_fp"))
                               or _int(m.get("volume_24h")) or 0),
            }
        if not out:
            sample = [
                {k: m.get(k) for k in ("ticker", "yes_sub_title", "title")}
                for m in markets[:3]
            ]
            log.warning("kalshi: %d markets but none matched the roster — "
                        "sample=%s", len(markets), sample)
            return None
        return out or None

    def _houseguest(self, market: dict) -> str | None:
        """Map a market to a houseguest, via the roster rather than the ticker.

        Ticker suffixes are abbreviations ("-YAS"); the roster is the source of
        truth for who is actually in this house, so anyone it doesn't recognise
        is skipped rather than guessed at.
        """
        if not self.roster:
            return None
        blob = " ".join(str(market.get(k, "")) for k in
                        ("yes_sub_title", "subtitle", "title"))
        for name in self.roster.names:
            if name.lower() in blob.lower():
                return name
        return None


def detect_drops(prev: dict, cur: dict, *, min_drop: int, min_volume: int,
                 min_price: int) -> list[dict]:
    """Houseguests whose winner odds fell hard, on real volume.

    Both conditions matter. A price fall alone is noise in a thin market; new
    volume alone is someone taking a position. Together they are what a leak
    looks like from the outside.
    """
    hits = []
    for name, now in cur.items():
        was = prev.get(name)
        if not was:
            continue
        drop = int(was.get("price", 0)) - int(now.get("price", 0))
        traded = int(now.get("volume", 0)) - int(was.get("volume", 0))
        # Ignore players already priced as no-hopers: a 3c market falling to 1c
        # is not news, it is rounding.
        if int(was.get("price", 0)) < min_price:
            continue
        if drop >= min_drop and traded >= min_volume:
            hits.append({
                "houseguest": name,
                "from": int(was["price"]),
                "to": int(now["price"]),
                "drop": drop,
                "traded": traded,
            })
    hits.sort(key=lambda h: h["drop"], reverse=True)
    return hits
