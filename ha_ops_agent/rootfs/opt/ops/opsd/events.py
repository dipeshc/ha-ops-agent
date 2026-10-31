"""Event queue, dispatcher and task scheduler: one pipeline for every trigger.

Every trigger (a due task, a manual check, Core recovering, a verified tap
from the admin's phone) becomes an `Event` on one bounded priority queue.
The queue drops duplicates of an event that is still pending or in flight
(`dedupe_key`), so a burst of triggers cannot stack runs. The dispatcher
hands each event to its handler as a task, so a long health check does not
hold up later events; the single Claude slot is enforced by the agent
runner, not here.

The scheduler is driven by the store: it claims due tasks with a
compare-and-set (a task fires once even across restarts), turns them into
`schedule.fired` events, and sleeps until the next due time or a wake-up.

Deterministic safety alerts (Core down, critical items) do not depend on
any of this: the liveness loop pushes directly, and the health handler's
safety push does not depend on Claude.
"""

import asyncio
import contextlib
import heapq
import itertools
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any

from opsd.runctx import AGENT_SELF, Principal
from opsd.store import Store, StoreError, Task

_LOGGER = logging.getLogger(__name__)

MAX_QUEUED = 100
MAX_IN_FLIGHT = 4
SCHEDULER_MAX_SLEEP_S = 60.0
RETRY_FULL_QUEUE = timedelta(minutes=1)


class Priority(IntEnum):
    """Lower runs first: safety > user message > approval > follow-up > triage."""

    SAFETY = 0
    USER = 10
    APPROVAL = 20
    FOLLOWUP = 30
    TRIAGE = 40
    MAINTENANCE = 50


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    source: str
    priority: Priority
    payload: Mapping[str, Any] = field(default_factory=dict)
    principal: Principal = AGENT_SELF
    dedupe_key: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[Event], Awaitable[None]]


class EventQueue:
    """Bounded priority queue with dedupe over pending and in-flight events."""

    def __init__(self, maxsize: int = MAX_QUEUED) -> None:
        self._maxsize = maxsize
        self._heap: list[tuple[int, int, Event]] = []
        self._order = itertools.count()
        self._keys: set[str] = set()
        self._ready = asyncio.Event()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._heap)

    def busy(self, key: str) -> bool:
        """An event with this key is queued or being handled."""
        return key in self._keys

    def put(self, event: Event) -> bool:
        """False when a duplicate is pending/in flight or the queue is full."""
        if event.dedupe_key is not None and event.dedupe_key in self._keys:
            return False
        if len(self._heap) >= self._maxsize:
            self.dropped += 1
            _LOGGER.warning("event queue full; event dropped", extra={"event": event.type, "source": event.source})
            return False
        if event.dedupe_key is not None:
            self._keys.add(event.dedupe_key)
        heapq.heappush(self._heap, (int(event.priority), next(self._order), event))
        self._ready.set()
        return True

    async def get(self) -> Event:
        while not self._heap:
            self._ready.clear()
            await self._ready.wait()
        return heapq.heappop(self._heap)[2]

    def done(self, event: Event) -> None:
        if event.dedupe_key is not None:
            self._keys.discard(event.dedupe_key)


class Dispatcher:
    def __init__(self, queue: EventQueue, handlers: Mapping[str, Handler], *, max_in_flight: int = MAX_IN_FLIGHT):
        self._queue = queue
        self._handlers = dict(handlers)
        self._slots = asyncio.Semaphore(max_in_flight)
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, event: Event) -> None:
        handler = self._handlers.get(event.type)
        try:
            if handler is None:
                _LOGGER.warning("no handler for event", extra={"event": event.type, "source": event.source})
                return
            await handler(event)
        except Exception:
            _LOGGER.exception("event handler crashed", extra={"event": event.type, "source": event.source})
        finally:
            self._queue.done(event)
            self._slots.release()

    async def run(self) -> None:
        while True:
            await self._slots.acquire()
            event = await self._queue.get()
            task = asyncio.create_task(self.handle(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


@dataclass(frozen=True, slots=True)
class Schedule:
    """`daily_at` (local HH:MM) or `every_s` seconds."""

    daily_at: str | None = None
    every_s: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in (("daily_at", self.daily_at), ("every_s", self.every_s)) if v is not None}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Schedule:
        daily = raw.get("daily_at")
        every = raw.get("every_s")
        return cls(
            daily_at=daily if isinstance(daily, str) else None,
            every_s=every if isinstance(every, int) and not isinstance(every, bool) and every > 0 else None,
        )


class TaskScheduler:
    def __init__(
        self,
        store: Store,
        queue: EventQueue,
        *,
        next_fire: Callable[[Schedule, datetime], datetime],
        to_event: Callable[[Task], Event],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._queue = queue
        self._next_fire = next_fire
        self._to_event = to_event
        self._clock = clock
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    def tick(self) -> None:
        """Claim every due task and enqueue it (a duplicate counts as fired)."""
        now = self._clock()
        for task in self._store.claim_due(now):
            event = self._to_event(task)
            if self._queue.put(event):
                continue
            if event.dedupe_key is not None and self._queue.busy(event.dedupe_key):
                # The same work is already queued or running; this firing is covered.
                self.complete(task)
            else:
                self._store.reschedule(task.id, next_fire_at=now + RETRY_FULL_QUEUE, now=now)

    def complete(self, task: Task, *, payload: dict[str, Any] | None = None) -> None:
        """Recurring tasks go back to pending at their next time; others are done."""
        now = self._clock()
        if task.kind == "recurring":
            due = self._next_fire(Schedule.from_json(task.schedule), now)
            self._store.reschedule(task.id, next_fire_at=due, now=now, payload=payload)
        else:
            self._store.finish_task(task.id, "done", now=now)
        self.wake()

    def delay_s(self) -> float:
        due = self._store.next_due_at()
        if due is None:
            return SCHEDULER_MAX_SLEEP_S
        return max(0.0, min(SCHEDULER_MAX_SLEEP_S, (due - self._clock()).total_seconds()))

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                self.tick()
                delay = self.delay_s()
            except (StoreError, sqlite3.Error, OSError, ValueError) as err:
                # The liveness loop's overdue check covers a scheduler that cannot run.
                _LOGGER.error("scheduler tick failed", extra={"error": str(err)[:200]})
                delay = SCHEDULER_MAX_SLEEP_S
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.5, delay))
