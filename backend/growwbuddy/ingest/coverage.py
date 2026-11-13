"""Coverage report (task 1.9): which supported question types each page can answer.

Question types are the "supported factual questions" in docs/problemStatement.md §2, plus
a few extra facts the pages carry. Gaps become "not found" answers and Known Limitations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from growwbuddy.ingest.parse_groww import ParsedPage

Answerable = Literal["yes", "no", "n/a"]


@dataclass(frozen=True)
class QuestionType:
    key: str
    label: str
    field: str | None  # parsed field that answers it; None = no page field can
    required: bool  # listed in the problem statement


QUESTION_TYPES: tuple[QuestionType, ...] = (
    QuestionType("expense_ratio", "Expense ratio", "expense_ratio", True),
    QuestionType("exit_load", "Exit load", "exit_load", True),
    QuestionType("min_sip", "Minimum SIP", "min_sip", True),
    QuestionType("elss_lock_in", "ELSS lock-in", "lock_in", True),
    QuestionType("riskometer", "Riskometer", "riskometer", True),
    QuestionType("benchmark", "Benchmark", "benchmark", True),
    QuestionType("statements", "Statement / capital gains download", None, True),
    QuestionType("min_lumpsum", "Minimum lump sum", "min_lumpsum", False),
    QuestionType("fund_manager", "Fund manager", "fund_managers", False),
    QuestionType("tax", "Tax on redemption", "tax", False),
    QuestionType("objective", "Investment objective", "objective", False),
)


@dataclass(frozen=True)
class CoverageCell:
    status: Answerable
    note: str = ""


def assess(page: ParsedPage, q: QuestionType) -> CoverageCell:
    if q.key == "elss_lock_in":
        category = page.fields.get("category")
        if not category or "elss" not in category.value.lower():
            return CoverageCell("n/a", "not an ELSS scheme")
    if q.field is None:
        return CoverageCell("no", "not on the scheme page")
    if q.field in page.fields:
        return CoverageCell("yes")
    return CoverageCell("no", "not stated on the page")


def load_pages(parsed_dir: Path) -> list[ParsedPage]:
    return [
        ParsedPage.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(parsed_dir.glob("*.json"))
    ]


def build_report(pages: list[ParsedPage]) -> list[tuple[ParsedPage, dict[str, CoverageCell]]]:
    return [(p, {q.key: assess(p, q) for q in QUESTION_TYPES}) for p in pages]


def format_report(pages: list[ParsedPage]) -> str:
    if not pages:
        return "No parsed pages found. Run `python -m growwbuddy.ingest` first."
    rows = build_report(pages)
    keys = [p.scheme_key for p in pages]
    label_w = max(len(q.label) for q in QUESTION_TYPES) + 2
    col_w = max(len(k) for k in keys) + 2
    lines = ["Coverage report: can each scheme page answer the question type?", ""]
    lines.append("".ljust(label_w) + "".join(k.ljust(col_w) for k in keys))
    for q in QUESTION_TYPES:
        mark = "*" if q.required else " "
        cells = "".join(cells_[q.key].status.ljust(col_w) for _, cells_ in rows)
        lines.append(f"{mark}{q.label}".ljust(label_w) + cells)
    lines += ["", "* = supported question type in the problem statement", "", "Gaps:"]
    gaps = [
        f"  - {q.label} / {page.scheme_key}: {cell.note}"
        for page, cells_ in rows
        for q in QUESTION_TYPES
        if (cell := cells_[q.key]).status != "yes"
    ]
    lines += gaps or ["  (none)"]
    lines += ["", "Data dates:"]
    lines += [f"  - {p.scheme_key}: as of {p.as_of_date} ({p.as_of_origin})" for p in pages]
    return "\n".join(lines)
