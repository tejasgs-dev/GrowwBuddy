"""Per-client and per-IP limits on model-answered messages (task 6.4; EV-6-01, EV-6-02).

The plan sizes the limits to Groq's budget: about 2 model-answered messages a minute and
20 a day per client, a little more per IP. A message over the limit gets the busy reply
(not stored, so it can be resent). Rule-handled messages are never counted.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.api.limits import LimitConfig, MessageLimiter
from growwbuddy.pipeline import refusals
from growwbuddy.pipeline.orchestrator import needs_model
from tests.api_helpers import CLIENT_A, client_for, make_services, new_thread, send


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


MODEL_Q = "Who is the registrar of HDFC Mid Cap Fund?"  # reaches the LLM router
RULE_Q = "Should I invest in HDFC Mid Cap?"  # advisory rule, no model call


def test_needs_model_only_for_messages_the_rules_dont_decide() -> None:
    assert needs_model(MODEL_Q)
    assert not needs_model(RULE_Q)
    assert not needs_model("What's the NAV of HDFC Large Cap?")
    assert not needs_model("My PAN is ABCDE1234F")
    assert not needs_model("hi")
    assert not needs_model("   ")
    assert not needs_model("x" * 1001)


def test_client_minute_and_day_limits_with_reset() -> None:
    clock = Clock()
    lim = MessageLimiter(LimitConfig(2, 3, 100, 100), clock=clock)
    assert lim.try_acquire("c", None).ok
    assert lim.try_acquire("c", None).ok
    third = lim.try_acquire("c", None)
    assert not third.ok and third.scope == "client/min"
    assert third.retry_after_s is not None and 0 < third.retry_after_s <= 60
    assert lim.try_acquire("other", None).ok  # another client is unaffected
    clock.t += 61
    assert lim.try_acquire("c", None).ok  # 3rd of the day
    clock.t += 61
    day = lim.try_acquire("c", None)
    assert not day.ok and day.scope == "client/day"
    clock.t += 86400
    assert lim.try_acquire("c", None).ok


def test_EV_6_02_ip_limit_applies_across_new_client_ids() -> None:
    lim = MessageLimiter(LimitConfig(2, 20, 3, 40), clock=Clock())
    results = [lim.try_acquire(str(uuid.uuid4()), "203.0.113.7") for _ in range(4)]
    assert [r.ok for r in results] == [True, True, True, False]
    assert results[-1].scope == "ip/min"
    assert lim.try_acquire(str(uuid.uuid4()), "198.51.100.1").ok  # another IP


def test_refused_message_is_not_counted() -> None:
    lim = MessageLimiter(LimitConfig(1, 20, 1, 40), clock=Clock())
    assert lim.try_acquire("a", "ip1").ok
    assert not lim.try_acquire("b", "ip1").ok  # IP full: client b not charged
    assert lim.try_acquire("b", "ip2").ok


def test_EV_6_01_api_returns_busy_over_the_client_limit(
    session_factory: sessionmaker[Session],
) -> None:
    svc = make_services(session_factory)
    svc.limiter = MessageLimiter(LimitConfig(2, 20, 10, 40), clock=Clock())
    client = client_for(svc)
    tid = new_thread(client)
    assert send(client, tid, MODEL_Q)["type"] == "answer"
    assert send(client, tid, MODEL_Q + " ")["type"] == "answer"
    over = send(client, tid, MODEL_Q + "  ")
    assert over["type"] == "busy"
    assert over["text"] == refusals.BUSY
    assert over["message_id"] is None  # not stored: the user can send it again
    assert over["citation"] is None and over["footer"] is None
    # Rule-handled messages still work and don't count.
    assert send(client, tid, RULE_Q)["type"] != "busy"
    assert svc.store.count_messages(tid) == 6  # 3 stored exchanges


def test_rule_handled_messages_never_limited(session_factory: sessionmaker[Session]) -> None:
    svc = make_services(session_factory)
    svc.limiter = MessageLimiter(LimitConfig(1, 1, 1, 1), clock=Clock())
    client = client_for(svc)
    tid = new_thread(client)
    for _ in range(5):
        assert send(client, tid, "hello", client_id=CLIENT_A)["type"] == "greeting"
    assert len(svc.limiter) == 0
