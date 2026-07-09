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

    @property
    def available(self) -> bool:
        return self._client is not None

    async def text(self, system: str, user: str, *, max_tokens: int = 1500,
                   temperature: float | None = None, heavy: bool = False) -> str | None:
        # NOTE: `temperature` is accepted for call-site compatibility but no
        # longer sent — Sonnet 5 / Opus 4.8 reject non-default sampling params
        # (HTTP 400 'temperature is deprecated for this model'). Models use
        # their own default sampling.
        """heavy=True routes to the recap model (daily/weekly recaps); every
        other call uses the workhorse model."""
        if not self._client:
            return None
        model = self.recap_model if heavy else self.model
        try:
            await self.limiter.acquire()
            msg = await self._client.messages.create(
                model=model, max_tokens=max_tokens,
                system=system, messages=[{"role": "user", "content": user}],
            )
            self.consecutive_failures = 0
            return "".join(b.text for b in msg.content if b.type == "text").strip()
        except Exception as e:
            self.consecutive_failures += 1
            log.error("LLM text call failed (%d in a row): %s",
                      self.consecutive_failures, e)
            return None

    async def structured(self, system: str, user: str, *, tool_name: str,
                         tool_description: str, schema: dict,
                         max_tokens: int = 2000) -> dict | None:
        """Force a single tool call and return its input as a dict."""
        if not self._client:
            return None
        try:
            await self.limiter.acquire()
            msg = await self._client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                tools=[{"name": tool_name, "description": tool_description,
                        "input_schema": schema}],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}],
            )
            self.consecutive_failures = 0
            for block in msg.content:
                if block.type == "tool_use":
                    return dict(block.input)
            return None
        except Exception as e:
            self.consecutive_failures += 1
            log.error("LLM structured call failed (%d in a row): %s",
                      self.consecutive_failures, e)
            return None
