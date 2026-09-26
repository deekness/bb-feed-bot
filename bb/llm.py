"""Async Anthropic wrapper with rate limiting.

Two entry points:
  text(...)       -> free-form text (used by the summarizer)
  structured(...) -> forced tool-use; returns the tool input dict, so we get
                     reliable structured JSON without parsing model prose.

If no API key is configured, `available` is False and callers fall back to
deterministic pattern logic.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import anthropic

log = logging.getLogger("bb.llm")


class RateLimiter:
    def __init__(self, per_minute: int, per_hour: int):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._minute: deque[float] = deque()
        self._hour: deque[float] = deque()

    async def acquire(self) -> None:
        now = time.time()
        self._prune(now)
        if len(self._minute) >= self.per_minute:
            wait = 60 - (now - self._minute[0])
            if wait > 0:
                log.warning("LLM rate limit (minute) — waiting %.1fs", wait)
                await asyncio.sleep(wait)
        if len(self._hour) >= self.per_hour:
            wait = 3600 - (now - self._hour[0])
            if wait > 0:
                log.warning("LLM rate limit (hour) — waiting %.1fs", wait)
                await asyncio.sleep(wait)
        t = time.time()
        self._minute.append(t)
        self._hour.append(t)

    def _prune(self, now: float) -> None:
        while self._minute and self._minute[0] < now - 60:
            self._minute.popleft()
        while self._hour and self._hour[0] < now - 3600:
            self._hour.popleft()


class LLM:
    def __init__(self, api_key: str, model: str, rpm: int, rph: int,
                 recap_model: str = ""):
        self.model = model
        # Recaps (daily/weekly) are low-volume, prose-quality-critical calls, so
        # they can run on a stronger/pricier model than the high-volume
        # extraction + hourly grind. Empty LLM_MODEL_RECAP => same model as
        # everything else, i.e. the split is a no-op until you opt in.
        self.recap_model = recap_model.strip() or model
        self.limiter = RateLimiter(rpm, rph)
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        if not self._client:
            log.warning("LLM DISABLED — no ANTHROPIC_API_KEY. Summaries fall "
                        "back to raw update lists.")
        elif self.recap_model != self.model:
            log.info("LLM ready — workhorse=%s, recap=%s", self.model, self.recap_model)
        else:
            log.info("LLM ready — %s (recaps use the same model; set "
                     "LLM_MODEL_RECAP to split)", self.model)
        # Consecutive-failure counter: lets the bot notice a dead key/quota
        # and DM the admin instead of silently degrading to raw lists.
        self.consecutive_failures = 0
        # --- prompt caching ---------------------------------------------------
        # The extraction call re-sends the same ~2,600 static tokens (tool
        # schema + system prompt) on every ingest cycle. Marking that prefix
        # cacheable makes each repeat cost 10% of input price instead of 100%.
        # Cadence is what makes it pay: ingest ticks every ~2 minutes against a
        # 5-minute cache TTL, so all but the first call in a window is a hit.
        # Writes cost 1.25x, so break-even is ~1.28 calls per window — cleared
        # whenever the feeds are active. During a quiet stretch calls can fall
        # more than 5 minutes apart and each one pays the 25% write premium;
        # that is the accepted trade against 90% off every hit.
        self.cache_enabled = True
        self.cache_reads = 0
        self.cache_writes = 0
        self.cache_tokens_read = 0
        self.cache_tokens_written = 0
        self._cache_log_every = 20

    @property
    def available(self) -> bool:
        return self._client is not None

    # --- prompt caching helpers ----------------------------------------------
    def _system_arg(self, system: str, cache: bool):
        """A plain string when caching is off, so uncached calls are byte-for-byte
        what they were before. The breakpoint goes on the system block because
        the cache prefix runs tools -> system -> messages: marking system
        therefore caches the tool schema WITH it, and the volatile user message
        stays outside."""
        if not cache:
            return system
        return [{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}]

    def _cache_rejected(self, exc: Exception) -> bool:
        """Did this call fail *because* of cache_control?

        This codebase has been bitten once already by a parameter a model
        silently stopped accepting (`temperature`), and extraction is the core
        loop — it must not go dark over an optimisation. So a cache-specific
        rejection turns caching off for the process and the call is retried
        plain, rather than failing the cycle.
        """
        text = f"{exc}".lower()
        if "cache" not in text:
            return False
        log.warning("prompt caching rejected by the API — disabling it for this "
                    "process and retrying without it: %s", exc)
        self.cache_enabled = False
        return True

    def _note_usage(self, usage) -> None:
        """Record cache effectiveness so it can be CONFIRMED from the logs
        rather than assumed from arithmetic."""
        if usage is None:
            return
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        wrote = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if not (read or wrote):
            return
        if read:
            self.cache_reads += 1
            self.cache_tokens_read += read
        else:
            self.cache_writes += 1
            self.cache_tokens_written += wrote
        log.debug("prompt cache: read=%d written=%d", read, wrote)
        calls = self.cache_reads + self.cache_writes
        # Rolled up rather than logged per call: extraction runs every couple of
        # minutes and a line each time would bury the feed logs.
        if calls % self._cache_log_every == 0:
            log.info("prompt cache: %d/%d calls hit (%.0f%%), %s tokens served "
                     "from cache, %s written",
                     self.cache_reads, calls,
                     100.0 * self.cache_reads / calls,
                     f"{self.cache_tokens_read:,}",
                     f"{self.cache_tokens_written:,}")

    async def text(self, system: str, user: str, *, max_tokens: int = 1500,
                   temperature: float | None = None, heavy: bool = False,
                   cache_system: bool = False) -> str | None:
        # NOTE: `temperature` is accepted for call-site compatibility but no
        # longer sent — Sonnet 5 / Opus 4.8 reject non-default sampling params
        # (HTTP 400 'temperature is deprecated for this model'). Models use
        # their own default sampling.
        """heavy=True routes to the recap model (daily/weekly recaps); every
        other call uses the workhorse model."""
        if not self._client:
            return None
        model = self.recap_model if heavy else self.model
        for attempt in (1, 2):
            cache = cache_system and self.cache_enabled
            try:
                await self.limiter.acquire()
                msg = await self._client.messages.create(
                    model=model, max_tokens=max_tokens,
                    system=self._system_arg(system, cache),
                    messages=[{"role": "user", "content": user}],
                )
            except Exception as e:
                if cache and attempt == 1 and self._cache_rejected(e):
                    continue
                self.consecutive_failures += 1
                log.error("LLM text call failed (%d in a row): %s",
                          self.consecutive_failures, e)
                return None
            self._note_usage(getattr(msg, "usage", None))
            self.consecutive_failures = 0
            return "".join(b.text for b in msg.content if b.type == "text").strip()
        return None

    async def structured(self, system: str, user: str, *, tool_name: str,
                         tool_description: str, schema: dict,
                         max_tokens: int = 2000,
                         cache_system: bool = False) -> dict | None:
        """Force a single tool call and return its input as a dict.

        `cache_system` marks the tool schema + system prompt as a cacheable
        prefix. Worth it only where the same prefix recurs inside the cache TTL
        — i.e. extraction, not the once-an-hour summaries.
        """
        if not self._client:
            return None
        for attempt in (1, 2):
            cache = cache_system and self.cache_enabled
            try:
                await self.limiter.acquire()
                msg = await self._client.messages.create(
                    model=self.model, max_tokens=max_tokens,
                    system=self._system_arg(system, cache),
                    tools=[{"name": tool_name, "description": tool_description,
                            "input_schema": schema}],
                    tool_choice={"type": "tool", "name": tool_name},
                    messages=[{"role": "user", "content": user}],
                )
            except Exception as e:
                if cache and attempt == 1 and self._cache_rejected(e):
                    continue
                self.consecutive_failures += 1
                log.error("LLM structured call failed (%d in a row): %s",
                          self.consecutive_failures, e)
                return None
            self._note_usage(getattr(msg, "usage", None))
            self.consecutive_failures = 0
            for block in msg.content:
                if block.type == "tool_use":
                    return dict(block.input)
            return None
        return None
