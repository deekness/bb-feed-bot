"""Prompt-caching checks for the LLM wrapper (no network, no API key)."""
import asyncio
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from bb.llm import LLM

FAILS = []


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}: got {got!r} want {want!r}")
        print(f"FAIL {label}: got {got!r} want {want!r}")
    else:
        print(f"ok   {label}")


class FakeMessages:
    """Records every request and replays scripted responses."""

    def __init__(self):
        self.calls = []
        self.raise_cache_error_once = False
        self.usage = SimpleNamespace(cache_read_input_tokens=0,
                                     cache_creation_input_tokens=0,
                                     input_tokens=100, output_tokens=10)

    async def create(self, **kw):
        self.calls.append(kw)
        if self.raise_cache_error_once:
            self.raise_cache_error_once = False
            raise Exception("400 invalid_request_error: cache_control is not "
                            "supported for this model")
        blocks = [SimpleNamespace(type="tool_use", input={"ok": True})] \
            if kw.get("tools") else [SimpleNamespace(type="text", text="hi")]
        return SimpleNamespace(content=blocks, usage=self.usage)


def make_llm():
    llm = LLM("sk-test", "claude-opus-5", 100, 1000)
    fake = FakeMessages()
    llm._client = SimpleNamespace(messages=fake)
    return llm, fake


SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


async def main():
    # --- 1. caching off by default: request is unchanged from before ---------
    llm, fake = make_llm()
    await llm.structured("SYS", "USER", tool_name="t", tool_description="d",
                         schema=SCHEMA)
    check("default: system stays a plain string",
          type(fake.calls[0]["system"]).__name__, "str")
    check("default: no cache_control anywhere",
          "cache_control" in json.dumps(fake.calls[0], default=str), False)

    await llm.text("SYS", "USER")
    check("text default: system stays a plain string",
          type(fake.calls[1]["system"]).__name__, "str")

    # --- 2. opt-in marks the system block, NOT the user message -------------
    llm, fake = make_llm()
    await llm.structured("SYS", "USER", tool_name="t", tool_description="d",
                         schema=SCHEMA, cache_system=True)
    sysarg = fake.calls[0]["system"]
    check("cache_system: system becomes a block list", isinstance(sysarg, list), True)
    check("cache_system: breakpoint present",
          sysarg[0].get("cache_control"), {"type": "ephemeral"})
    check("cache_system: prompt text preserved exactly", sysarg[0]["text"], "SYS")
    check("cache_system: tools still sent (they cache WITH the system)",
          [t["name"] for t in fake.calls[0]["tools"]], ["t"])
    check("cache_system: user message left outside the cache",
          "cache_control" in json.dumps(fake.calls[0]["messages"]), False)

    # --- 3. the prefix must be byte-identical across calls ------------------
    llm, fake = make_llm()
    for _ in range(3):
        await llm.structured("SYS", "changing user text " + str(_),
                             tool_name="t", tool_description="d",
                             schema=SCHEMA, cache_system=True)
    prefixes = {json.dumps([c["tools"], c["system"]], sort_keys=True)
                for c in fake.calls}
    check("prefix identical across 3 calls (a cache hit needs this)",
          len(prefixes), 1)

    # --- 4. a cache rejection disables caching and retries plain ------------
    llm, fake = make_llm()
    fake.raise_cache_error_once = True
    got = await llm.structured("SYS", "USER", tool_name="t",
                               tool_description="d", schema=SCHEMA,
                               cache_system=True)
    check("rejection: call still succeeds", got, {"ok": True})
    check("rejection: it retried", len(fake.calls), 2)
    check("rejection: retry sent a plain string",
          type(fake.calls[1]["system"]).__name__, "str")
    check("rejection: caching now off process-wide", llm.cache_enabled, False)
    check("rejection: extraction loop not marked failing",
          llm.consecutive_failures, 0)

    # a non-cache error must NOT be swallowed as a cache problem
    llm, fake = make_llm()

    async def boom(**kw):
        fake.calls.append(kw)
        raise Exception("529 overloaded_error")

    fake.create = boom
    check("unrelated error returns None",
          await llm.structured("S", "U", tool_name="t", tool_description="d",
                               schema=SCHEMA, cache_system=True), None)
    check("unrelated error does not disable caching", llm.cache_enabled, True)
    check("unrelated error counts as a failure", llm.consecutive_failures, 1)
    check("unrelated error is not retried", len(fake.calls), 1)

    # --- 5. hit/write accounting ------------------------------------------
    llm, fake = make_llm()
    fake.usage = SimpleNamespace(cache_read_input_tokens=0,
                                 cache_creation_input_tokens=2600,
                                 input_tokens=900, output_tokens=10)
    await llm.structured("S", "U", tool_name="t", tool_description="d",
                         schema=SCHEMA, cache_system=True)
    check("first call counted as a write", (llm.cache_writes, llm.cache_reads),
          (1, 0))
    fake.usage = SimpleNamespace(cache_read_input_tokens=2600,
                                 cache_creation_input_tokens=0,
                                 input_tokens=900, output_tokens=10)
    for _ in range(4):
        await llm.structured("S", "U", tool_name="t", tool_description="d",
                             schema=SCHEMA, cache_system=True)
    check("subsequent calls counted as hits",
          (llm.cache_writes, llm.cache_reads), (1, 4))
    check("cached tokens tallied", llm.cache_tokens_read, 10400)
    check("written tokens tallied", llm.cache_tokens_written, 2600)

    # --- 6. an uncached model response must not crash the counters --------
    llm, fake = make_llm()
    fake.usage = None
    check("missing usage is survivable",
          await llm.text("S", "U", cache_system=True), "hi")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S)")
        sys.exit(1)
    print("all prompt-cache checks passed")


asyncio.run(main())
