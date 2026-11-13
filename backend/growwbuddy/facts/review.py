"""Human review of scheme facts (task 2.10, EV-2-12).

A reviewer compares each fact with the live Groww page and either confirms it or enters the
correct value (`extracted_by = manual`). Both record who reviewed it and when. A manual
value must still pass the sanity checks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from growwbuddy.ingest.extract_facts import FACT_KEYS
from growwbuddy.ingest.fact_checks import check_fact
from growwbuddy.ingest.parse_groww import normalise_riskometer
from growwbuddy.store.models import SchemeFact, SchemeFactHistory


class ReviewError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def list_facts(
    session: Session,
    *,
    scheme: str | None = None,
    pending_only: bool = False,
    unverified_only: bool = False,
) -> list[SchemeFact]:
    q = select(SchemeFact).order_by(SchemeFact.scheme, SchemeFact.fact_key)
    if scheme:
        q = q.where(SchemeFact.scheme == scheme)
    rows = list(session.scalars(q))
    if unverified_only:
        rows = [r for r in rows if not r.verified]
    elif pending_only:
        rows = [r for r in rows if not r.verified or r.reviewed_by is None]
    order = {k: i for i, k in enumerate(FACT_KEYS)}
    return sorted(rows, key=lambda r: (r.scheme, order.get(r.fact_key, 99)))


def _get(session: Session, scheme: str, fact_key: str) -> SchemeFact:
    row = session.get(SchemeFact, (scheme, fact_key))
    if row is None:
        raise ReviewError(f"no fact {scheme}/{fact_key}")
    return row


def _reviewer(name: str) -> str:
    name = name.strip()
    if not name:
        raise ReviewError("a reviewer name is required")
    return name[:64]


def verify_fact(
    session: Session, scheme: str, fact_key: str, reviewer: str, now: datetime | None = None
) -> SchemeFact:
    """Confirm the stored value matches the live page."""
    row = _get(session, scheme, fact_key)
    if row.value_text is None:
        raise ReviewError(f"{scheme}/{fact_key} has no value; enter one with `set`")
    row.verified = True
    row.check_error = None
    row.reviewed_by = _reviewer(reviewer)
    row.reviewed_at = now or _now()
    return row


def set_manual(
    session: Session,
    scheme: str,
    fact_key: str,
    value: str,
    reviewer: str,
    now: datetime | None = None,
) -> SchemeFact:
    """Replace the value with one a person read off the page."""
    row = _get(session, scheme, fact_key)
    value = value.strip()
    if fact_key == "riskometer":
        value = normalise_riskometer(value)
    if err := check_fact(fact_key, value):
        raise ReviewError(f"value rejected: {err}")
    ts = now or _now()
    session.add(
        SchemeFactHistory(
            scheme=scheme,
            fact_key=fact_key,
            old_value=row.value_text,
            new_value=value,
            old_verified=row.verified,
            new_verified=True,
            old_extracted_by=row.extracted_by,
            source_id=row.source_id,
            changed_at=ts,
        )
    )
    row.value_text = value
    row.extracted_by = "manual"
    row.verified = True
    row.check_error = None
    row.reviewed_by = _reviewer(reviewer)
    row.reviewed_at = ts
    return row


def describe(row: SchemeFact) -> str:
    status = "verified" if row.verified else "UNVERIFIED"
    reviewed = (
        f"reviewed by {row.reviewed_by} on {row.reviewed_at:%d %b %Y}"
        if row.reviewed_by and row.reviewed_at
        else "not reviewed by a person"
    )
    lines = [
        f"{row.scheme} / {row.fact_key}",
        f"  value:     {row.value_text if row.value_text is not None else '(none)'}",
        f"  status:    {status} ({row.extracted_by}); {reviewed}",
        f"  as of:     {row.as_of_date:%d %b %Y}",
        f"  source:    {row.source_url}",
    ]
    if row.extracted_by == "manual" and row.extracted_value != row.value_text:
        lines.append(f"  extracted: {row.extracted_value}")
    if row.check_error:
        lines.append(f"  problem:   {row.check_error}")
    if row.evidence:
        lines.extend(f"  | {line}" for line in row.evidence.splitlines())
    return "\n".join(lines)


def review_loop(
    session: Session,
    rows: Sequence[SchemeFact],
    reviewer: str,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> tuple[int, int]:
    """Walk through `rows`. Returns (confirmed, corrected). Commits after each change."""
    reviewer = _reviewer(reviewer)
    confirmed = corrected = 0
    for i, row in enumerate(rows, 1):
        say(f"\n[{i}/{len(rows)}] {describe(row)}")
        while True:
            choice = ask("  [v]erify  [m]anual value  [s]kip  [q]uit > ").strip().lower()
            try:
                if choice == "v":
                    verify_fact(session, row.scheme, row.fact_key, reviewer)
                    session.commit()
                    confirmed += 1
                elif choice == "m":
                    value = ask("  correct value as shown on the page > ")
                    set_manual(session, row.scheme, row.fact_key, value, reviewer)
                    session.commit()
                    corrected += 1
                elif choice == "q":
                    return confirmed, corrected
                elif choice != "s":
                    continue
            except ReviewError as e:
                session.rollback()
                say(f"  {e}")
                continue
            break
    return confirmed, corrected
