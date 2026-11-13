"""Download Groww scheme pages (task 1.4).

- Generic User-Agent with no personal details.
- Redirects are followed by hand, and every hop is checked against the allowed domains
  *before* it is requested, so an off-domain redirect is never fetched (EC-ING-04).
- Retries with exponential backoff on timeouts, connection errors, 403, 429 and 5xx;
  `Retry-After` is honoured (EC-ING-01, 07, 08).

The pages render their fund data server-side (embedded Next.js JSON), so no headless
browser is needed; see docs/corpus-notes.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from growwbuddy.constants import ALLOWED_DOMAIN
from growwbuddy.ingest.manifest import SourceEntry, is_allowed_url

USER_AGENT = "Mozilla/5.0 (compatible; GrowwBuddy/0.1; facts-only FAQ indexer)"
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_S = 120.0
RETRYABLE_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """The page could not be fetched safely. The previous version must be kept."""


@dataclass(frozen=True)
class FetchResult:
    source_id: str
    url: str
    final_url: str
    status_code: int
    html: str
    fetched_at: datetime
    last_modified: str | None


def _retry_after_seconds(resp: httpx.Response, now: datetime) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - now).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))


class Fetcher:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        allowed_domains: Iterable[str] = (ALLOWED_DOMAIN,),
        max_attempts: int = 4,
        backoff_base_s: float = 2.0,
        timeout_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout_s)
        self._allowed = tuple(allowed_domains)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_s
        self._sleep = sleep
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get_following_redirects(self, url: str) -> httpx.Response:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not is_allowed_url(current, self._allowed):
                raise FetchError(f"refusing to fetch URL outside allowed domains: {current}")
            resp = self._client.get(
                current,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                follow_redirects=False,
            )
            if resp.is_redirect:
                location = resp.headers.get("Location")
                if not location:
                    raise FetchError(f"redirect without Location from {current}")
                current = urljoin(current, location)
                continue
            return resp
        raise FetchError(f"too many redirects starting at {url}")

    def fetch(self, source: SourceEntry) -> FetchResult:
        last_problem = "no attempt made"
        for attempt in range(1, self._max_attempts + 1):
            delay = self._backoff_base * 2 ** (attempt - 1)
            try:
                resp = self._get_following_redirects(source.url)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_problem = f"{type(e).__name__}"
            else:
                if resp.status_code == 200:
                    resp.encoding = "utf-8"  # Groww serves UTF-8; never guess (EC-ING-19)
                    return FetchResult(
                        source_id=source.id,
                        url=source.url,
                        final_url=str(resp.url),
                        status_code=200,
                        html=resp.text,
                        fetched_at=self._clock(),
                        last_modified=resp.headers.get("Last-Modified"),
                    )
                last_problem = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise FetchError(f"{source.id}: {last_problem}")
                retry_after = _retry_after_seconds(resp, self._clock())
                if retry_after is not None:
                    delay = max(delay, retry_after)
            if attempt < self._max_attempts:
                self._sleep(delay)
        raise FetchError(
            f"{source.id}: failed after {self._max_attempts} attempts ({last_problem})"
        )
