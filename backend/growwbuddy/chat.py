"""Terminal chat for manual testing (task 3.13).

    python -m growwbuddy.chat            # uses the index in GROWWBUDDY_DB_URL and Groq
    python -m growwbuddy.chat --debug    # also prints the response type and reason

History is kept in memory for this session only. Type `/new` to start a fresh thread and
`/quit` (or Ctrl+D) to exit. Needs `make ingest` first, and GROQ_API_KEY for questions the
rules can't answer on their own.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from growwbuddy.config import Settings, settings
from growwbuddy.constants import DISCLAIMER
from growwbuddy.llm.client import GroqClient
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.orchestrator import Pipeline, should_store
from growwbuddy.pipeline.types import Response, Turn
from growwbuddy.retrieval.embed import make_embedder
from growwbuddy.retrieval.retriever import Retriever
from growwbuddy.retrieval.vector import VectorIndex
from growwbuddy.store.db import init_db, make_engine, make_session_factory


def build_pipeline(s: Settings = settings) -> Pipeline:
    engine = make_engine(s.db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    retriever = Retriever(
        VectorIndex(sf, s.embed_model),
        make_embedder(s),
        sf,
        min_score=s.retrieval_min_score,
        min_gap=s.retrieval_min_gap,
    )
    return Pipeline(
        GroqClient(s),
        retriever,
        history_turns=s.history_turns,
        fetched_at=retriever.fetched_at,
        stale_days=s.stale_days,
    )


def render(resp: Response, debug: bool = False) -> str:
    lines = [f"GrowwBuddy: {resp.text}"]
    if resp.citation:
        lines.append(f"  Source: {resp.citation.title} <{resp.citation.url}>")
    if resp.footer:
        lines.append(f"  {resp.footer}")
    if debug:
        lines.append(f"  [type={resp.type} reason={resp.reason} scheme={resp.scheme}]")
    return "\n".join(lines)


async def repl(pipeline: Pipeline, debug: bool) -> None:
    history: list[Turn] = []
    print(f"{refusals.WELCOME}\n{DISCLAIMER}\n(/new for a new thread, /quit to exit)\n")
    loop = asyncio.get_running_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, input, "You: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        cmd = text.strip().lower()
        if cmd in ("/quit", "/exit"):
            return
        if cmd == "/new":
            history.clear()
            print("(new thread)\n")
            continue
        resp = await pipeline.handle_message(text, history)
        print(render(resp, debug) + "\n")
        if should_store(resp):
            history.append(Turn(text, resp.type, resp.scheme, resp.text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m growwbuddy.chat")
    parser.add_argument("--debug", action="store_true", help="show type, reason and logs")
    args = parser.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(repl(build_pipeline(), args.debug))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
