"""Live updates for the panel: an in-memory event hub and presence leases.

The hub numbers every event (a monotonic `seq`) and keeps the last few
hundred in a ring buffer, so a long-poll (or an SSE reconnect with
`Last-Event-ID`) can resume. A gap older than the buffer is reported as
`None` and the client re-fetches from the store, which stays the source of
truth. Subscribers get a bounded queue each; a slow consumer is dropped
rather than buffered without limit. Live connections (SSE streams and
waiting long-polls together) are capped.

Presence: the panel posts a heartbeat while a thread is visible. A lease
lasts `LEASE_S` seconds and only ever extends from a fresh heartbeat; the
client cannot choose its length. While a thread has a fresh lease, chat
replies for it are not pushed. Reminders and safety pushes ignore presence.
Everything here is lost on restart, which errs towards pushing.
"""

import asyncio
import contextlib
import itertools
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

MAX_LIVE = 4
QUEUE_SIZE = 50
BUFFER = 256
LEASE_S = 20.0
MAX_LEASES = 8


@dataclass(frozen=True, slots=True)
class LiveEvent:
    seq: int
    kind: str
    thread: str | None
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "thread": self.thread, "data": self.data}


@dataclass(slots=True, eq=False)
class Subscriber:
    """One live connection, watching global events plus (optionally) one thread."""

    thread: str | None
    queue: asyncio.Queue[LiveEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))
    dropped: asyncio.Event = field(default_factory=asyncio.Event)

    def wants(self, event: LiveEvent) -> bool:
        return event.thread is None or event.thread == self.thread


class Hub:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_live: int = MAX_LIVE) -> None:
        self._clock = clock
        self._max_live = max_live
        self._seq = itertools.count(1)
        self.last_seq = 0
        self._buffer: deque[LiveEvent] = deque(maxlen=BUFFER)
        # Highest seq pushed out of the buffer by its size: a client behind it has missed events.
        self._evicted = 0
        self._subscribers: set[Subscriber] = set()
        self._leases: dict[str, float] = {}
        # In-process listeners (the item book re-derives its items when a record they show changed).
        self.listeners: list[Callable[[LiveEvent], None]] = []

    # --------------------------------------------------------------- events

    def publish(self, kind: str, thread: str | None, data: dict[str, Any]) -> LiveEvent:
        event = LiveEvent(next(self._seq), kind, thread, data)
        self.last_seq = event.seq
        if len(self._buffer) == BUFFER:
            self._evicted = self._buffer[0].seq
        self._buffer.append(event)
        for sub in list(self._subscribers):
            if not sub.wants(event):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                _LOGGER.warning("live subscriber too slow; dropped", extra={"thread_id": sub.thread})
                self._subscribers.discard(sub)
                sub.dropped.set()
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                _LOGGER.exception("live listener failed", extra={"kind": kind})
        return event

    def since(self, after: int, thread: str | None) -> list[LiveEvent] | None:
        """Buffered events after `after` for this view, or None when some were already evicted."""
        if after > self.last_seq or after < 0 or after < self._evicted:
            return None
        probe = Subscriber(thread)
        return [e for e in self._buffer if e.seq > after and probe.wants(e)]

    # ---------------------------------------------------------- connections

    @property
    def live(self) -> int:
        return len(self._subscribers)

    def subscribe(self, thread: str | None) -> Subscriber | None:
        """A new live connection, or None at the cap."""
        if len(self._subscribers) >= self._max_live:
            return None
        sub = Subscriber(thread)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def close_all(self) -> None:
        for sub in list(self._subscribers):
            sub.dropped.set()
        self._subscribers.clear()

    async def wait(self, after: int, thread: str | None, hold_s: float) -> list[LiveEvent] | None:
        """Long-poll: events after `after` (waiting up to `hold_s`); None for a gap. Raises at the cap."""
        ready = self.since(after, thread)
        if ready is None or ready:
            return ready
        sub = self.subscribe(thread)
        if sub is None:
            raise OverflowError("too many live connections")
        try:
            with contextlib.suppress(TimeoutError):
                first = await asyncio.wait_for(sub.queue.get(), hold_s)
                events = [first]
                while not sub.queue.empty():
                    events.append(sub.queue.get_nowait())
                return events
            return []
        finally:
            self.unsubscribe(sub)

    # ------------------------------------------------------------- presence

    def touch(self, thread: str, *, visible: bool) -> None:
        now = self._clock()
        self._leases = {t: exp for t, exp in self._leases.items() if exp > now}
        if not visible:
            self._leases.pop(thread, None)
            return
        self._leases[thread] = now + LEASE_S
        while len(self._leases) > MAX_LEASES:
            oldest = min(self._leases, key=lambda t: self._leases[t])
            del self._leases[oldest]

    def is_watching(self, thread: str) -> bool:
        expiry = self._leases.get(thread)
        return expiry is not None and expiry > self._clock()
