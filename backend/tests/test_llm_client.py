"""Groq response handling: only a clean `stop` with JSON is accepted (§6.5, EC-FCT-12)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from growwbuddy.config import Settings
from growwbuddy.llm.client import GroqClient, read_completion, strict_format


def completion(
    content: str | None, finish: str = "stop", refusal: str | None = None
) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, refusal=refusal)
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=msg)], usage=usage
    )


def test_clean_stop_is_accepted() -> None:
    r = read_completion(completion('{"stated": true, "exit_load": "Nil"}'))
    assert r.ok and r.data == {"stated": True, "exit_load": "Nil"}
    assert r.usage["prompt_tokens"] == 10


@pytest.mark.parametrize(
    ("c", "error"),
    [
        (completion('{"a": 1}', finish="length"), "finish_reason=length"),
        (completion('{"a": 1}', refusal="I can't help"), "refused"),
        (completion(""), "empty"),
        (completion(None), "empty"),
        (completion("{not json"), "invalid JSON"),
        (completion("[1, 2]"), "not an object"),
        (SimpleNamespace(choices=[]), "no choices"),
    ],
)
def test_everything_else_is_rejected(c: Any, error: str) -> None:
    r = read_completion(c)
    assert not r.ok and r.error and error in r.error


def test_strict_format() -> None:
    f = strict_format("x", {"type": "object"})
    assert f["json_schema"]["strict"] is True


async def test_missing_api_key_is_a_safe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    client = GroqClient(Settings(_env_file=None))
    r = await client.complete_json(
        system="s", user="u", schema_name="x", schema={"type": "object"}, max_tokens=10
    )
    assert not r.ok and r.error and "MissingApiKeyError" in r.error
