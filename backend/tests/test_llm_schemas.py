"""Router and answer schemas, system prompts (tasks 3.5, 3.6)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from growwbuddy.ingest.extract_facts import EXIT_LOAD_SCHEMA
from growwbuddy.llm.prompts import load_prompt
from growwbuddy.llm.schemas import ANSWER_SCHEMA, ROUTER_SCHEMA, Answer, RouterDecision


def _walk(node: Any) -> list[dict[str, Any]]:
    out = []
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            out.extend(_walk(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v))
    return out


@pytest.mark.parametrize("schema", [ROUTER_SCHEMA, ANSWER_SCHEMA, EXIT_LOAD_SCHEMA])
def test_schemas_fit_groq_strict_mode(schema: dict[str, Any]) -> None:
    text = json.dumps(schema)
    assert "minItems" not in text and "maxItems" not in text
    assert '"null"' not in text and "null" not in text
    for node in _walk(schema):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        assert not isinstance(node.get("type"), list)  # no unions


def test_router_scheme_enum_has_five_keys_and_none() -> None:
    enum = ROUTER_SCHEMA["properties"]["scheme"]["enum"]
    assert "none" in enum and "hdfc-elss" not in enum and len(enum) == 6


def _router(**kw: Any) -> dict[str, Any]:
    base = {"intent": "factual", "may_be_advice": False, "scheme": "none", "rewritten_query": "q"}
    return base | kw


def test_router_maps_none_to_python_none() -> None:
    assert RouterDecision.model_validate(_router()).scheme is None
    assert RouterDecision.model_validate(_router(scheme="hdfc-mid-cap")).scheme == "hdfc-mid-cap"


def test_router_rejects_unknown_scheme() -> None:
    with pytest.raises(ValidationError):
        RouterDecision.model_validate(_router(scheme="hdfc-elss"))


@pytest.mark.parametrize(
    ("intent", "may_be_advice", "final"),
    [
        ("factual", True, "advisory"),  # EV-3-06 tie-break
        ("procedural", True, "advisory"),
        ("factual", False, "factual"),
        ("performance", True, "performance"),
    ],
)
def test_tie_break(intent: str, may_be_advice: bool, final: str) -> None:
    d = RouterDecision.model_validate(_router(intent=intent, may_be_advice=may_be_advice))
    assert d.final_intent == final


def _answer(sentences: list[str], cite: str = "x#y", answerable: bool = True) -> dict[str, Any]:
    return {"answerable": answerable, "sentences": sentences, "source_chunk_id": cite}


def test_answer_maps_empty_citation_to_none() -> None:
    assert Answer.model_validate(_answer([], cite="", answerable=False)).source_chunk_id is None


def test_answer_rejects_four_sentences() -> None:
    with pytest.raises(ValidationError):
        Answer.model_validate(_answer(["a.", "b.", "c.", "d."]))


def test_answer_with_no_sentences_is_not_an_answer() -> None:
    # Pydantic accepts 0 items, but `is_answer` treats it as not answerable (EC-GEN-08).
    assert not Answer.model_validate(_answer([])).is_answer
    assert not Answer.model_validate(_answer(["  ", ""])).is_answer
    assert Answer.model_validate(_answer(["One."])).is_answer


def test_answerable_false_with_citation_is_not_an_answer() -> None:
    assert not Answer.model_validate(_answer(["x."], answerable=False)).is_answer  # EC-GEN-09


@pytest.mark.parametrize("name", ["router_system", "answer_system"])
def test_prompts_are_static(name: str) -> None:
    text = load_prompt(name)
    assert len(text) > 200
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text)  # no dates
    assert "{" not in text.replace('{"answerable"', "")  # no template placeholders
    assert load_prompt(name) is load_prompt(name)  # cached: byte-identical every call
