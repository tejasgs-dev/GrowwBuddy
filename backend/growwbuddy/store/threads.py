"""Thread and message storage (task 4.2, architecture §9.1).

Synchronous SQLAlchemy; the API calls these through `asyncio.to_thread`. Every write is a
single short transaction (EC-OPS-03). Ownership is checked here: a thread is only visible
to the `client_id` that created it, and a foreign thread looks exactly like a missing one
(EC-THR-11, EC-API-07).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from growwbuddy.constants import THREAD_TITLE_MAX_CHARS, format_footer
from growwbuddy.ids import new_message_id, new_thread_id
from growwbuddy.pipeline.types import Response, Turn
from growwbuddy.store.models import Message, Thread

# A thread gets its title from the first message whose reply is one of these: a factual
# question that was answered or looked up. Greetings, refusals and clarifications don't
# name the thread (EC-THR-15).
TITLE_TYPES: frozenset[str] = frozenset({"answer", "not_found"})

_SPACES = re.compile(r"\s+")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _utc(dt: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone=True columns; they were stored as UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def make_title(text: str) -> str:
    """First question, whitespace collapsed, cut to 60 characters (task 4.7)."""
    return _SPACES.sub(" ", text).strip()[:THREAD_TITLE_MAX_CHARS].rstrip()


@dataclass(frozen=True)
class ThreadInfo:
    id: str
    client_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoredMessage:
    id: str
    thread_id: str
    role: str
    content: str
    response_type: str | None
    reason: str | None
    scheme: str | None
    citation_url: str | None
    citation_title: str | None
    as_of_date: date | None
    client_message_id: str | None
    created_at: datetime

    @property
    def footer(self) -> str | None:
        return format_footer(self.as_of_date) if self.as_of_date else None


def _thread(t: Thread) -> ThreadInfo:
    return ThreadInfo(t.id, t.client_id, t.title, _utc(t.created_at), _utc(t.updated_at))


def _message(m: Message) -> StoredMessage:
    return StoredMessage(
        id=m.id,
        thread_id=m.thread_id,
        role=m.role,
        content=m.content,
        response_type=m.response_type,
        reason=m.reason,
        scheme=m.scheme,
        citation_url=m.citation_url,
        citation_title=m.citation_title,
        as_of_date=m.as_of_date,
        client_message_id=m.client_message_id,
        created_at=_utc(m.created_at),
    )


class ThreadStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sf = session_factory
        self._clock = clock

    # ------------------------------------------------------------ threads

    def create(self, client_id: str) -> ThreadInfo:
        now = self._clock()
        t = Thread(id=new_thread_id(), client_id=client_id, title=None, created_at=now,
                   updated_at=now)  # fmt: skip
        with self._sf.begin() as session:
            session.add(t)
        return _thread(t)

    def list_for(self, client_id: str, limit: int = 100) -> list[ThreadInfo]:
        with self._sf() as session:
            rows = session.scalars(
                select(Thread)
                .where(Thread.client_id == client_id)
                .order_by(Thread.updated_at.desc(), Thread.id.desc())
                .limit(limit)
            ).all()
            return [_thread(t) for t in rows]

    def get(self, thread_id: str, client_id: str) -> ThreadInfo | None:
        """The thread, or None if it doesn't exist or belongs to another client."""
        with self._sf() as session:
            t = session.get(Thread, thread_id)
            if t is None or t.client_id != client_id:
                return None
            return _thread(t)

    def delete(self, thread_id: str, client_id: str) -> bool:
        with self._sf.begin() as session:
            t = session.get(Thread, thread_id)
            if t is None or t.client_id != client_id:
                return False
            session.execute(delete(Message).where(Message.thread_id == thread_id))
            session.delete(t)
        return True

    def delete_inactive(self, days: int) -> int:
        """Delete threads with no activity for more than `days` days, and their messages
        (task 6.7, §11). Returns how many threads were deleted."""
        cutoff = self._clock() - timedelta(days=days)
        with self._sf.begin() as session:
            ids = list(session.scalars(select(Thread.id).where(Thread.updated_at < cutoff)))
            if ids:
                session.execute(delete(Message).where(Message.thread_id.in_(ids)))
                session.execute(delete(Thread).where(Thread.id.in_(ids)))
        return len(ids)

    # ------------------------------------------------------------ messages

    def messages(self, thread_id: str) -> list[StoredMessage]:
        with self._sf() as session:
            rows = session.scalars(
                select(Message).where(Message.thread_id == thread_id).order_by(Message.id)
            ).all()
            return [_message(m) for m in rows]

    def count_messages(self, thread_id: str | None = None) -> int:
        q = select(func.count()).select_from(Message)
        if thread_id is not None:
            q = q.where(Message.thread_id == thread_id)
        with self._sf() as session:
            return int(session.scalar(q) or 0)

    def recent_turns(self, thread_id: str, turns: int) -> list[Turn]:
        """The last `turns` exchanges, oldest first, for query rewriting (EC-THR-05)."""
        if turns <= 0:
            return []
        with self._sf() as session:
            rows = session.scalars(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.id.desc())
                .limit(2 * turns)
            ).all()
        rows = list(reversed(rows))
        out: list[Turn] = []
        for user, reply in pairwise(rows):
            if user.role == "user" and reply.role == "assistant":
                out.append(
                    Turn(user.content, reply.response_type or "", reply.scheme, reply.content)
                )
        return out[-turns:]

    def find_reply(self, thread_id: str, client_message_id: str) -> StoredMessage | None:
        """The stored reply to an earlier send with the same client message ID (EC-THR-07)."""
        with self._sf() as session:
            user = session.scalar(
                select(Message).where(
                    Message.thread_id == thread_id,
                    Message.client_message_id == client_message_id,
                )
            )
            if user is None:
                return None
            reply = session.scalar(
                select(Message)
                .where(Message.thread_id == thread_id, Message.id > user.id)
                .order_by(Message.id)
                .limit(1)
            )
            return _message(reply) if reply is not None and reply.role == "assistant" else None

    def append_exchange(
        self,
        thread_id: str,
        user_text: str,
        resp: Response,
        *,
        client_message_id: str | None = None,
    ) -> tuple[StoredMessage, ThreadInfo] | None:
        """Write a message and its reply in one transaction; set the title if due.

        Returns None, writing nothing, if the thread was deleted while the reply was being
        made (EC-THR-09).
        """
        now = self._clock()
        with self._sf.begin() as session:
            t = session.get(Thread, thread_id)
            if t is None:
                return None
            user = Message(
                id=new_message_id(),
                thread_id=thread_id,
                role="user",
                content=user_text,
                client_message_id=client_message_id,
                created_at=now,
            )
            reply = Message(
                id=new_message_id(),
                thread_id=thread_id,
                role="assistant",
                content=resp.text,
                response_type=resp.type,
                reason=resp.reason,
                scheme=resp.scheme,
                citation_url=resp.citation.url if resp.citation else None,
                citation_title=resp.citation.title if resp.citation else None,
                as_of_date=resp.last_updated,
                created_at=now,
            )
            session.add_all([user, reply])
            t.updated_at = now
            if t.title is None and resp.type in TITLE_TYPES:
                t.title = make_title(user_text)
            session.flush()
            return _message(reply), _thread(t)
