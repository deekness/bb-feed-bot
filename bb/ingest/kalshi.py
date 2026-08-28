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
        params = {"event_ticker": self.event_ticker, "status": "open", "limit": "200"}
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as sess:
                async with sess.get(_BASE, params=params, timeout=self.timeout) as resp:
                    if resp.status != 200:
                        self.consecutive_failures += 1
                        log.warning("kalshi HTTP %s", resp.status)
                        return None
                    data = await resp.json()
        except Exception as e:
            self.consecutive_failures += 1
            log.warning("kalshi fetch failed: %s", e)
            return None
        self.consecutive_failures = 0

        markets = data.get("markets")
        if not isinstance(markets, list):
            log.warning("kalshi: unexpected payload shape")
            return None

        out: dict[str, dict] = {}
        for m in markets:
            if not isinstance(m, dict):
                continue
            who = self._houseguest(m)
            if not who:
                continue
            price = m.get("last_price")
            if price is None:
                price = m.get("yes_bid")
            try:
                price = int(price)
            except (TypeError, ValueError):
                continue
            out[who] = {
                "ticker": str(m.get("ticker", "")),
                "price": price,
                "volume": int(m.get("volume") or 0),
                "volume_24h": int(m.get("volume_24h") or 0),
            }
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
