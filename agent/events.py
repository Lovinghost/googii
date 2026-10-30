import asyncio
import contextvars
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
_event_sink: contextvars.ContextVar[EventSink | None] = contextvars.ContextVar(
    "agent_event_sink", default=None
)


async def emit_event(event_type: str, data: dict[str, Any] | None = None) -> None:
    sink = _event_sink.get()
    if sink is not None:
        await sink(event_type, data or {})


def bind_event_sink(sink: EventSink):
    return _event_sink.set(sink)


def reset_event_sink(token: contextvars.Token) -> None:
    _event_sink.reset(token)


@dataclass(slots=True)
class AgentEvent:
    type: str
    run_id: str
    session_id: str
    sequence: int
    data: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_api_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "type": value["type"],
            "runId": value["run_id"],
            "sessionId": value["session_id"],
            "sequence": value["sequence"],
            "eventId": value["event_id"],
            "timestamp": value["timestamp"],
            "data": value["data"],
        }


class EventJournal:
    """In-process real-time journal; durable adapters can subscribe later."""

    def __init__(self, run_id: str, session_id: str, history_limit: int = 1000) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.history_limit = history_limit
        self._events: list[AgentEvent] = []
        self._condition = asyncio.Condition()
        self._closed = False

    async def publish(self, event_type: str, data: dict[str, Any] | None = None) -> AgentEvent:
        async with self._condition:
            event = AgentEvent(
                type=event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                sequence=(self._events[-1].sequence + 1 if self._events else 1),
                data=data or {},
            )
            self._events.append(event)
            if len(self._events) > self.history_limit:
                self._events = self._events[-self.history_limit :]
            self._condition.notify_all()
            return event

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def stream(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        cursor = after_sequence
        while True:
            async with self._condition:
                available = [event for event in self._events if event.sequence > cursor]
                if not available and not self._closed:
                    await self._condition.wait()
                    continue
                closed = self._closed
            for event in available:
                cursor = event.sequence
                yield event
            if closed and not any(event.sequence > cursor for event in self._events):
                return

    def snapshot(self) -> list[AgentEvent]:
        return list(self._events)

