"""Ingest run: fetch -> extract -> hash check -> parse -> write (task 1.10).

Sources are processed one at a time with a pause between them (EC-ING-08). A failure on
one source never touches the previously parsed version of it, and the other sources
still update (EC-ING-23).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.ingest.fetch import Fetcher, FetchError
from growwbuddy.ingest.manifest import Manifest, SourceEntry
from growwbuddy.ingest.parse_groww import (
    ParseError,
    build_parsed_page,
    content_hash,
    extract_fund_data,
)
from growwbuddy.store.models import SourceState

Status = Literal["parsed", "unchanged", "failed"]


@dataclass(frozen=True)
class SourceOutcome:
    source_id: str
    status: Status
    detail: str = ""


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_raw(raw_dir: Path, source_id: str, html: str, day: str, keep: int) -> Path:
    """Store raw HTML at raw/<source_id>/<yyyy-mm-dd>.html; keep the newest `keep` copies."""
    folder = raw_dir / source_id
    path = folder / f"{day}.html"
    _write_atomic(path, html)
    copies = sorted(folder.glob("*.html"))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)
    return path


def parsed_path(parsed_dir: Path, source_id: str) -> Path:
    return parsed_dir / f"{source_id}.json"


def _ingest_one(
    source: SourceEntry,
    state: SourceState,
    *,
    fetcher: Fetcher,
    raw_dir: Path,
    parsed_dir: Path,
    raw_keep: int,
    force: bool,
) -> SourceOutcome:
    try:
        result = fetcher.fetch(source)
    except FetchError as e:
        state.status, state.last_error = "failed", str(e)
        return SourceOutcome(source.id, "failed", str(e))

    save_raw(raw_dir, source.id, result.html, result.fetched_at.date().isoformat(), raw_keep)

    try:
        fund = extract_fund_data(result.html, source)
    except ParseError as e:
        state.status, state.last_error = "failed", f"parse: {e}"
        return SourceOutcome(source.id, "failed", f"parse: {e}")

    digest = content_hash(fund)
    out = parsed_path(parsed_dir, source.id)
    state.fetched_at = result.fetched_at
    state.http_last_modified = result.last_modified
    state.last_error = None

    if not force and state.sha256 == digest and out.exists():
        state.status = "unchanged"
        return SourceOutcome(source.id, "unchanged", "content hash unchanged; parse skipped")

    try:
        page = build_parsed_page(fund, source, result.fetched_at)
    except ParseError as e:
        state.status, state.last_error = "failed", f"parse: {e}"
        return SourceOutcome(source.id, "failed", f"parse: {e}")

    _write_atomic(out, page.model_dump_json(indent=2) + "\n")
    state.sha256 = digest
    state.status = "ok"
    detail = f"as_of {page.as_of_date.isoformat()}"
    if page.missing:
        detail += f"; missing: {', '.join(page.missing)}"
    return SourceOutcome(source.id, "parsed", detail)


def run_ingest(
    manifest: Manifest,
    *,
    fetcher: Fetcher,
    session_factory: sessionmaker[Session],
    raw_dir: Path,
    parsed_dir: Path,
    only: Sequence[str] | None = None,
    force: bool = False,
    raw_keep: int = 5,
    pause_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SourceOutcome]:
    sources = [manifest.get(s) for s in only] if only else list(manifest.sources)
    outcomes: list[SourceOutcome] = []
    for i, source in enumerate(sources):
        if i:
            sleep(pause_s)
        with session_factory() as session, session.begin():
            state = session.get(SourceState, source.id) or SourceState(source_id=source.id)
            session.add(state)
            outcome = _ingest_one(
                source,
                state,
                fetcher=fetcher,
                raw_dir=raw_dir,
                parsed_dir=parsed_dir,
                raw_keep=raw_keep,
                force=force,
            )
            state.last_checked_at = fetcher.now()
        outcomes.append(outcome)
    return outcomes
