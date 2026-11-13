"""CLI REPL (task 3.13) and the live check script (tasks 3.4, 3.14), with no network."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from growwbuddy import chat
from growwbuddy.llm import check
from growwbuddy.llm.client import JsonResult
from growwbuddy.pipeline.types import Citation, Response, Turn


def test_render_answer() -> None:
    r = Response(
        type="answer",
        text="The TER is 0.74%.",
        citation=Citation("https://groww.in/x", "HDFC Mid Cap Fund Direct Growth – Groww"),
        footer="Last updated from sources: 10 Nov 2026",
        scheme="hdfc-mid-cap",
    )
    out = chat.render(r, debug=True)
    assert "GrowwBuddy: The TER is 0.74%." in out
    assert "<https://groww.in/x>" in out
    assert "Last updated from sources: 10 Nov 2026" in out
    assert "type=answer" in out


class StubPipeline:
    def __init__(self, replies: list[Response]) -> None:
        self.replies = replies
        self.histories: list[list[Turn]] = []

    async def handle_message(self, text: str, history: Sequence[Turn] = ()) -> Response:
        self.histories.append(list(history))
        return self.replies.pop(0)


async def test_repl_keeps_history_but_not_pii_or_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = iter(["q1", "my PAN", "q2", "q3", "/new", "q4", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))
    stub = StubPipeline(
        [
            Response("answer", "a1", scheme="hdfc-mid-cap"),
            Response("pii_rejected", "no PII"),
            Response("busy", "busy"),
            Response("answer", "a3", scheme="hdfc-mid-cap"),
            Response("answer", "a4"),
        ]
    )
    await chat.repl(stub, debug=False)  # type: ignore[arg-type]
    h = stub.histories
    assert [t.user_text for t in h[1]] == ["q1"]  # before "my PAN"
    assert [t.user_text for t in h[2]] == ["q1"]  # PII not stored
    assert [t.user_text for t in h[3]] == ["q1"]  # busy not stored
    assert h[4] == []  # /new cleared the thread
    assert "a3" in capsys.readouterr().out


class FakeGroq:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.n = 0

    async def complete_json(self, **_: Any) -> JsonResult:
        self.n += 1
        cached = 0 if self.n == 1 else 128
        if not self.ok:
            return JsonResult(False, error="api error: BadRequestError")
        return JsonResult(True, data={}, usage={"cached_tokens": cached, "total_tokens": 300})


async def test_live_check_reports_schemas_and_caching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGroq()
    monkeypatch.setattr(check, "GroqClient", lambda _s: fake)
    assert await check.run() == 0
    out = capsys.readouterr().out
    assert fake.n == 4  # three schemas + the repeated answer call
    assert out.count("accepted") == 4
    assert "prompt caching: working" in out


async def test_live_check_fails_on_rejected_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check, "GroqClient", lambda _s: FakeGroq(ok=False))
    assert await check.run() == 1
    assert "FAILED" in capsys.readouterr().out
