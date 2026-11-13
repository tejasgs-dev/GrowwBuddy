"""EV-1-02, EV-1-05, EV-1-06: fetcher safety and retries (mocked HTTP)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from growwbuddy.ingest.fetch import USER_AGENT, Fetcher, FetchError
from growwbuddy.ingest.manifest import Manifest, SourceEntry
from tests.conftest import FIXED_NOW

Handler = Callable[[httpx.Request], httpx.Response]


def make_fetcher(handler: Handler, sleeps: list[float]) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(client, sleep=sleeps.append, clock=lambda: FIXED_NOW, max_attempts=4)


@pytest.fixture
def source(manifest: Manifest) -> SourceEntry:
    return manifest.get("groww-hdfc-mid-cap")


def test_success_and_user_agent(source: SourceEntry) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200, content="<html>₹</html>".encode(), headers={"Last-Modified": "x"}
        )

    result = make_fetcher(handler, []).fetch(source)
    assert result.html == "<html>₹</html>"
    assert result.fetched_at == FIXED_NOW
    assert result.last_modified == "x"
    ua = seen[0].headers["User-Agent"]
    assert ua == USER_AGENT
    assert "@" not in ua  # EV-1-06: no personal contact details


def test_EV_1_02_off_domain_redirect_never_requested(source: SourceEntry) -> None:
    requested: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested.append(str(req.url))
        if req.url.host == "groww.in":
            return httpx.Response(302, headers={"Location": "https://evil.example.com/page"})
        return httpx.Response(200, text="should never be fetched")

    with pytest.raises(FetchError, match="outside allowed domains"):
        make_fetcher(handler, []).fetch(source)
    assert all("evil.example.com" not in u for u in requested)


def test_on_domain_redirect_followed(source: SourceEntry) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("direct-growth"):
            return httpx.Response(301, headers={"Location": "/mutual-funds/moved"})
        return httpx.Response(200, text="ok")

    result = make_fetcher(handler, []).fetch(source)
    assert result.final_url == "https://groww.in/mutual-funds/moved"


def test_EV_1_05_retries_5xx_then_succeeds(source: SourceEntry) -> None:
    calls = iter([503, 502, 200])
    sleeps: list[float] = []
    result = make_fetcher(lambda _: httpx.Response(next(calls), text="ok"), sleeps).fetch(source)
    assert result.status_code == 200
    assert sleeps == [2.0, 4.0]  # exponential backoff


def test_EV_1_05_honours_retry_after(source: SourceEntry) -> None:
    calls = iter([httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200)])
    sleeps: list[float] = []
    make_fetcher(lambda _: next(calls), sleeps).fetch(source)
    assert sleeps == [30.0]


def test_EV_1_05_timeouts_exhaust_retries(source: SourceEntry) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=req)

    sleeps: list[float] = []
    with pytest.raises(FetchError, match="failed after 4 attempts"):
        make_fetcher(handler, sleeps).fetch(source)
    assert len(sleeps) == 3


def test_404_not_retried(source: SourceEntry) -> None:
    sleeps: list[float] = []
    with pytest.raises(FetchError, match="HTTP 404"):
        make_fetcher(lambda _: httpx.Response(404), sleeps).fetch(source)
    assert sleeps == []
