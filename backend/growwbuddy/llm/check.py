"""Live Groq check (task 3.4 "live schema check" and task 3.14 prompt caching).

    python -m growwbuddy.llm.check

Sends each strict schema (router, answer, exit-load extraction) to Groq once and reports
whether it was accepted, then repeats the answer call to see whether the static system
prompt is cached (`usage.prompt_tokens_details.cached_tokens > 0` on the second call).
A cached count of 0 is harmless (EC-GEN-15). Uses a few thousand tokens; never run in CI.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from growwbuddy.config import settings
from growwbuddy.ingest.extract_facts import (
    EXIT_LOAD_SCHEMA,
    EXIT_LOAD_SCHEMA_NAME,
    EXIT_LOAD_SYSTEM,
)
from growwbuddy.llm.client import GroqClient, JsonResult
from growwbuddy.llm.prompts import load_prompt
from growwbuddy.llm.schemas import (
    ANSWER_SCHEMA,
    ANSWER_SCHEMA_NAME,
    ROUTER_SCHEMA,
    ROUTER_SCHEMA_NAME,
)

_ANSWER_USER = (
    "Context:\n[groww-hdfc-mid-cap#exit_load] Exit load of HDFC Mid Cap Fund Direct Growth: "
    "Exit load of 1% if redeemed within 1 year.\n\n"
    "Question: What is the exit load of HDFC Mid Cap Fund?"
)


def _line(name: str, r: JsonResult) -> str:
    status = "accepted" if r.ok else f"FAILED ({r.error})"
    return (
        f"{name:<14} {status:<40} cached_tokens={r.usage.get('cached_tokens', 0)} usage={r.usage}"
    )


async def run() -> int:
    client = GroqClient(settings)
    calls: list[tuple[str, dict[str, Any]]] = [
        (
            "router",
            dict(
                system=load_prompt("router_system"),
                user="Conversation so far:\n(no earlier turns)\n\nLatest message:\n"
                "exit load of mid cap?",
                schema_name=ROUTER_SCHEMA_NAME,
                schema=ROUTER_SCHEMA,
                max_tokens=512,
                kind="router",
            ),
        ),
        (
            "answer",
            dict(
                system=load_prompt("answer_system"),
                user=_ANSWER_USER,
                schema_name=ANSWER_SCHEMA_NAME,
                schema=ANSWER_SCHEMA,
                max_tokens=1024,
                kind="answer",
            ),
        ),
        (
            "exit_load",
            dict(
                system=EXIT_LOAD_SYSTEM,
                user="Exit load of 1% if redeemed within 1 year",
                schema_name=EXIT_LOAD_SCHEMA_NAME,
                schema=EXIT_LOAD_SCHEMA,
                max_tokens=512,
                kind="extract",
            ),
        ),
    ]
    failed = 0
    answer_kwargs: dict[str, Any] = {}
    for name, kwargs in calls:
        r = await client.complete_json(**kwargs)
        failed += not r.ok
        print(_line(name, r))
        if name == "answer":
            answer_kwargs = kwargs
    # Task 3.14: the same answer call again; the static system prompt should now be cached.
    r = await client.complete_json(**answer_kwargs)
    print(_line("answer (again)", r))
    cached = r.usage.get("cached_tokens", 0)
    print(
        "prompt caching: "
        + ("working" if cached > 0 else "no cached tokens reported (harmless, EC-GEN-15)")
    )
    return 1 if failed else 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
