"""Roster resolver — the single source of truth for "is this a houseguest?"

Every name produced by extraction passes through here. If it does not resolve
to someone on the season roster (or a configured nickname), it is dropped.
This is what makes extraction trustworthy and is applied identically to every
houseguest — there is no per-person logic anywhere.
"""
from __future__ import annotations

from .config import Season


class Roster:
    def __init__(self, names: list[str], nicknames: dict[str, str]):
        self.names = list(names)
        self._canonical = {n.lower(): n for n in names}
        # nickname key (lower) -> canonical roster name
        self._nick: dict[str, str] = {}
        for nick, target in nicknames.items():
            canon = self._canonical.get(str(target).lower(), str(target))
            self._nick[str(nick).lower()] = canon

    @classmethod
    def from_season(cls, season: Season) -> "Roster":
        return cls(season.roster, season.nicknames)

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
