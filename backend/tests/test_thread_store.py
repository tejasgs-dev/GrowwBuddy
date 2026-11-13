"""Thread store (tasks 4.1, 4.2, 4.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.pipeline.types import Response
from growwbuddy.store.threads import ThreadStore, make_title
from tests.api_helpers import CLIENT_A, CLIENT_B, TER_DATE, answer


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def greeting() -> Response:
    return Response(type="greeting", text="Hi!", reason="greeting")


def test_create_get_list_and_ownership(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory, clock=Clock())
    t1 = store.create(CLIENT_A)
    t2 = store.create(CLIENT_A)
    store.create(CLIENT_B)
    assert t1.title is None
    assert store.get(t1.id, CLIENT_A) == t1
    assert store.get(t1.id, CLIENT_B) is None  # foreign looks like missing
    assert store.get("thr_missing", CLIENT_A) is None
    store.append_exchange(t1.id, "q", greeting())  # t1 is now the most recent
    assert [t.id for t in store.list_for(CLIENT_A)] == [t1.id, t2.id]
    assert len(store.list_for(CLIENT_B)) == 1


def test_timestamps_are_utc(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    got = store.get(t.id, CLIENT_A)
    assert got is not None and got.created_at.tzinfo is not None


def test_append_exchange_stores_pair_in_order(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    saved = store.append_exchange(t.id, "TER of mid cap?", answer("It is 0.74%."))
    assert saved is not None
    reply, thread = saved
    msgs = store.messages(t.id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "TER of mid cap?"),
        ("assistant", "It is 0.74%."),
    ]
    assert msgs[1].id == reply.id
    assert reply.response_type == "answer" and reply.scheme == "hdfc-mid-cap"
    assert reply.citation_url and reply.as_of_date == TER_DATE
    assert reply.footer == "Last updated from sources: 10 Nov 2026"
    assert msgs[0].response_type is None and msgs[0].footer is None
    assert thread.title == "TER of mid cap?"


def test_title_waits_for_first_factual_question(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    store.append_exchange(t.id, "hi", greeting())
    refusal = Response(type="refusal", text="I can't advise.", reason="advisory")
    store.append_exchange(t.id, "should I invest?", refusal)
    assert store.get(t.id, CLIENT_A).title is None  # type: ignore[union-attr]
    q = "What is the exit load of HDFC Small Cap Fund Direct Growth if I redeem early?"
    store.append_exchange(t.id, q, Response(type="not_found", text="Not found."))
    assert store.get(t.id, CLIENT_A).title == q[:60]  # type: ignore[union-attr]
    store.append_exchange(t.id, "another question", answer("x"))
    assert store.get(t.id, CLIENT_A).title == q[:60]  # type: ignore[union-attr]


def test_make_title() -> None:
    assert make_title("  what   is\nthe TER?  ") == "what is the TER?"
    assert len(make_title("x" * 80)) == 60
    assert make_title("a" * 59 + " bcd") == "a" * 59  # no trailing space


def test_recent_turns(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    for i in range(10):
        store.append_exchange(t.id, f"q{i}", answer(f"a{i}"))
    turns = store.recent_turns(t.id, 6)  # EC-THR-05: only the last 6
    assert [x.user_text for x in turns] == [f"q{i}" for i in range(4, 10)]
    assert turns[-1].response_text == "a9" and turns[-1].scheme == "hdfc-mid-cap"
    assert turns[-1].response_type == "answer"
    assert store.recent_turns(t.id, 0) == []


def test_find_reply_by_client_message_id(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    cmid = "11111111-2222-4333-8444-555555555555"
    saved = store.append_exchange(t.id, "q", answer("a"), client_message_id=cmid)
    store.append_exchange(t.id, "q2", answer("a2"))
    assert saved is not None
    found = store.find_reply(t.id, cmid)
    assert found is not None and found.id == saved[0].id and found.content == "a"
    assert store.find_reply(t.id, "99999999-2222-4333-8444-555555555555") is None


def test_delete_removes_messages_and_respects_owner(
    session_factory: sessionmaker[Session],
) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    other = store.create(CLIENT_A)
    store.append_exchange(t.id, "q", answer("a"))
    store.append_exchange(other.id, "q", answer("a"))
    assert not store.delete(t.id, CLIENT_B)
    assert store.count_messages(t.id) == 2
    assert store.delete(t.id, CLIENT_A)
    assert store.get(t.id, CLIENT_A) is None
    assert store.count_messages(t.id) == 0
    assert store.count_messages(other.id) == 2  # untouched
    assert not store.delete(t.id, CLIENT_A)


def test_append_after_delete_writes_nothing(session_factory: sessionmaker[Session]) -> None:
    store = ThreadStore(session_factory)
    t = store.create(CLIENT_A)
    store.delete(t.id, CLIENT_A)
    assert store.append_exchange(t.id, "q", answer("a")) is None  # EC-THR-09
    assert store.count_messages() == 0
