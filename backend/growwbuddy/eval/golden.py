"""Golden set items (task 6.1, eval.md §8.1-8.2).

One JSON object per line in `eval/golden.jsonl` (and the held-out `eval/holdout.jsonl`).
Only the last turn of an item is graded; earlier turns set up the thread. Expected fact
values are never written here: an item names `fact_key`/`fact_keys`, and the grader reads
the verified value from `scheme_facts` at eval time (EC-EVAL-01).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from growwbuddy.constants import SCHEME_KEYS, SOURCES

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
GOLDEN_FILE = EVAL_DIR / "golden.jsonl"
HOLDOUT_FILE = EVAL_DIR / "holdout.jsonl"

Category = Literal[
    "expense_ratio",
    "exit_load",
    "min_investment",
    "benchmark",
    "riskometer",
    "other_facts",
    "follow_up",
    "advisory",
    "injection",
    "performance",
    "pii",
    "out_of_scope",
    "not_found",
    "ambiguous",
]

# Categories counted in factual accuracy (eval.md §8.4): the six fact categories plus
# follow-ups.
FACT_CATEGORIES: frozenset[str] = frozenset(
    {
        "expense_ratio",
        "exit_load",
        "min_investment",
        "benchmark",
        "riskometer",
        "other_facts",
        "follow_up",
    }
)
REFUSAL_CATEGORIES: frozenset[str] = frozenset({"advisory", "injection"})
SCOPE_CATEGORIES: frozenset[str] = frozenset({"out_of_scope", "not_found"})

_SOURCE_IDS = {sid for sid, _, _ in SOURCES}


class TurnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user: str = Field(min_length=1)


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # One type, or several when more than one is correct (e.g. a documented page gap
    # may be answered "not found").
    type: list[str]
    scheme: str | None = None
    fact_keys: list[str] = Field(default_factory=list)
    # Strings the answer must contain (for facts that aren't in scheme_facts, e.g. the
    # launch date). Compared ignoring case and spacing.
    contains: list[str] = Field(default_factory=list)
    # The one Groww page the reply must link; None means the reply must have no link.
    citation_source: str | None = None
    # Out-of-scope replies must name the five covered schemes (EC-INT-12, 15, 16).
    names_schemes: bool = False
    # PII strings that must never reach the model, the store or the logs.
    pii: list[str] = Field(default_factory=list)
    # For the retrieval-cutoff tuning (task 6.3): the corpus has no answer to this.
    not_in_corpus: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            if isinstance(data.get("type"), str):
                data["type"] = [data["type"]]
            if "fact_key" in data:
                data["fact_keys"] = [*data.get("fact_keys", []), data.pop("fact_key")]
        return data

    @field_validator("scheme")
    @classmethod
    def _scheme(cls, v: str | None) -> str | None:
        if v is not None and v not in SCHEME_KEYS:
            raise ValueError(f"unknown scheme {v}")
        return v

    @field_validator("citation_source")
    @classmethod
    def _source(cls, v: str | None) -> str | None:
        if v is not None and v not in _SOURCE_IDS:
            raise ValueError(f"unknown source {v}")
        return v


class GoldenItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    category: Category
    turns: list[TurnSpec] = Field(min_length=1)
    expect: Expect
    edge_cases: list[str] = Field(default_factory=list)
    # Tracked but not scored (eval.md §8.1, "Ambiguous scheme").
    scored: bool = True

    @property
    def last(self) -> str:
        return self.turns[-1].user


def load_items(path: Path) -> list[GoldenItem]:
    items: list[GoldenItem] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append(GoldenItem.model_validate(json.loads(line)))
        except ValueError as e:
            raise ValueError(f"{path.name} line {n}: {e}") from e
    ids = [i.id for i in items]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"{path.name}: duplicate ids {sorted(dupes)}")
    return items
