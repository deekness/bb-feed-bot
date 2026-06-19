# Big Brother Live-Feed Bot

A Discord bot that watches Big Brother live-feed updates (Jokers Updates RSS +
Bluesky), summarizes them neutrally, and tracks alliances, relationships, and
game state. Built to be **season-agnostic**: a new season is a one-file edit.

---

## Core principles

**Neutrality.** The bot treats every houseguest identically. There is no
per-houseguest logic anywhere: importance is scored purely by *event type*
(an eviction matters; who's involved doesn't change that), and every LLM prompt
explicitly forbids favoritism or "who deserves to win" commentary. This was a
deliberate fix to remove the old bot's built-in bias.

**Roster-grounded extraction.** The old tracker guessed alliances/members with
regex and then fought false positives with a blacklist — which is why random
words became "alliances." Here, the cast list in `season.yaml` is a hard gate:
the LLM proposes dynamics, but any name that doesn't resolve to a real
houseguest is discarded. No roster entry → it cannot appear.

**The database is the queue.** Summaries are built by querying the `updates`
table for a time window. There's no in-memory highlight queue to persist, so a
restart never loses or double-posts anything.

---

## Updating for a new season

Edit **one file**: copy `season.example.yaml` to `season.yaml` and fill in:

- `season_number`, `season_name`, `season_start_date` (premiere date — drives "Day N" and week math)
- `roster` — the full first-name list (leave empty until the cast is announced; extraction stays off until then)
- `nicknames` — optional map of nicknames/typos → canonical name
- `bluesky_accounts`, `bb_keywords` — usually unchanged between seasons

No code changes required.

---

## Architecture

```
main.py                  entry point
bb/
  config.py              Settings (env) + Season (yaml)
  roster.py              the neutral name-resolution gate
  db.py                  asyncpg pool, schema, queries (DB = source of truth)
  llm.py                 async Anthropic wrapper: text() + structured() (forced tool-use)
  models.py              dataclasses (Update, AllianceProposal, ...)
  logging_setup.py       console + logs/bot.log
  ingest/
    rss.py               Jokers Updates RSS
    bluesky.py           AT Protocol feeds (relevance = keywords OR roster name)
    dedup.py             content hashing (normalizes times/dates/urls)
    pipeline.py          poll sources -> store -> return only NEW updates
  analysis/
    extract.py           LLM structured extraction + roster validation
    summarize.py         neutral hourly/daily summaries (+ pattern fallback)
  trackers/
    alliances.py         evidence accumulation, confidence decay, confirm/reject
    relationships.py     pairwise affinity graph
    game_state.py        HOH / noms / veto / eviction per week
  discord_bot/
    client.py            bot + ingest/hourly/daily loops
    commands.py          slash commands
```

### How alliance tracking works
1. Each ingest cycle, new updates go to the LLM extractor, which returns
   proposed alliances **with a supporting quote and confidence** via a forced
   tool call (reliable JSON).
2. Every member name is validated against the roster; proposals with fewer than
   two real houseguests are dropped.
3. The tracker **merges** a proposal into an existing alliance if they share 2+
   members, otherwise creates a new "forming" one.
4. Confidence **rises** with corroboration and **decays** daily without fresh
   mentions; a forming alliance is promoted to "active" past a threshold and
   auto-dissolved if confidence falls too low.
5. `/confirmalliance` and `/rejectalliance` **lock** an alliance so your manual
   judgment is never overwritten by the automatic pipeline.

---

## Commands

Public: `/wtf`, `/summary [hours]`, `/alliances`, `/relationship <houseguest>`, `/gamestate`
Admin: `/confirmalliance <id>`, `/rejectalliance <id>`, `/setchannel <channel>`, `/status`
Owner: `/sync`

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in DISCORD_TOKEN, DATABASE_URL, ANTHROPIC_API_KEY
cp season.example.yaml season.yaml
python main.py
```

Set the posting channel at runtime with `/setchannel`, or via `UPDATE_CHANNEL_ID`.

### Environment variables
Required: `DISCORD_TOKEN`, `DATABASE_URL` (Postgres).
Recommended: `ANTHROPIC_API_KEY`, `LLM_MODEL` (set to the current model string you want).
Optional: `UPDATE_CHANNEL_ID`, `OWNER_ID`, `TIMEZONE`, `SEASON_CONFIG`, `LLM_RPM`, `LLM_RPH`,
`BLUESKY_USERNAME`, `BLUESKY_PASSWORD` (Bluesky ingestion is skipped without these).

Without an Anthropic key the bot still runs: summaries fall back to deterministic
pattern logic and extraction is disabled.

### Deploy (Railway / Docker)
Provision a Postgres instance, set the env vars, and run `python main.py` as the
start command. The schema is created automatically on first connect.

---

## Extension points
- **Predictions / polls, Zings, etc.**: add a new cog under `bb/discord_bot/` and
  load it in `setup_hook` — the command surface is intentionally small.
- **Validation harness**: drop a set of real updates with hand-labeled expected
  extraction into a fixtures file and run `extractor.extract()` over them to
  measure precision/recall before changing the prompt. (Same idea as testing
  entry variants on a replay before trusting them.)
- **Live per-N highlights**: the urgent-alert path in `ingest_loop` is the hook
  if you want more than hourly/daily cadence.
