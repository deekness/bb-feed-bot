"""Roster resolver — the single source of truth for "is this a houseguest?"

Every name produced by extraction passes through here. If it does not resolve
to someone on the season roster (or a configured nickname), it is dropped.
This is what makes extraction trustworthy and is applied identically to every
houseguest — there is no per-person logic anywhere.

Mutable at runtime: /addhouseguest, /removehouseguest and /addnickname mutate
this object in place (persisted via bot_kv and re-applied on startup), so a
premiere-night twist addition needs no redeploy. In-place mutation matters:
BlueskySource keys its compiled-pattern cache on tuple(roster.names) and the
extractor reads roster.names per call, so every consumer sees changes
immediately.
"""
from __future__ import annotations

from .config import Season


class Roster:
    def __init__(self, names: list[str], nicknames: dict[str, str]):
        self.names: list[str] = []
        self._canonical: dict[str, str] = {}
        # nickname key (lower) -> canonical roster name
        self._nick: dict[str, str] = {}
        for n in names:
            self.add(n)
        for nick, target in nicknames.items():
            self.add_nickname(nick, target)

    @classmethod
    def from_season(cls, season: Season) -> "Roster":
        return cls(season.roster, season.nicknames)

    # --- runtime mutation -----------------------------------------------------
    def add(self, name: str) -> bool:
        """Add a houseguest. Returns False if blank or already present."""
        name = str(name).strip()
        if not name or name.lower() in self._canonical:
            return False
        self.names.append(name)
        self._canonical[name.lower()] = name
        return True

    def remove(self, name: str) -> bool:
        """Remove a houseguest (typo fixes only — evicted HGs stay on the
        roster, since feeds keep referencing them). Drops their nicknames."""
        canon = self.resolve(name)
        if not canon:
            return False
        self.names.remove(canon)
        del self._canonical[canon.lower()]
        self._nick = {k: v for k, v in self._nick.items() if v != canon}
        return True

    def add_nickname(self, nick: str, target: str) -> bool:
        """Map a nickname/typo to a roster name. Lenient on target for YAML
        compatibility (unresolved targets pass through as-is)."""
        nick = str(nick).strip().lower()
        if not nick:
            return False
        canon = self._canonical.get(str(target).strip().lower(), str(target).strip())
        self._nick[nick] = canon
        return True

    @property
    def nicknames(self) -> dict[str, str]:
        return dict(self._nick)

    # --- resolution -------------------------------------------------------------
    def resolve(self, raw: str | None) -> str | None:
        """Return the canonical roster name for `raw`, or None if not on the roster."""
        if not raw:
            return None
        key = raw.strip().lower()
        if key in self._canonical:
            return self._canonical[key]
        if key in self._nick:
            return self._nick[key]
        return None

    def resolve_all(self, raws: list[str]) -> list[str]:
        """Resolve a list, dropping unknowns and de-duplicating, order preserved."""
        out: list[str] = []
        for r in raws:
            name = self.resolve(r)
            if name and name not in out:
                out.append(name)
        return out

    def contains(self, raw: str) -> bool:
        return self.resolve(raw) is not None

    @property
    def is_empty(self) -> bool:
        return len(self.names) == 0
