"""Reminders, follow-ups and chat (push replies and the panel), admin only.

- A due `reminder` is a deterministic push (no model) with Done and Snooze
  buttons, plus a Reply button when asked for.
- A due `followup` is a `followup` profile run; its reply (if any) is a push.
- A chat message (a verified push reply or a panel message) is stored at
  once as a queued admin turn and answered by a `chat` run on a small
  bounded job queue. The reply is a turn in the thread, published live to
  the panel, and pushed only when the admin is not watching that thread.
- A message that gets no answer is never dropped silently: the thread gets
  a card (a `system` turn) that says why. Blocks that lift by themselves
  (usage limits, caps, the account yielding) are retried automatically
  unless the admin turns that off (see `chatcards`). A retry moves the turn
  `refused -> queued` with a compare-and-set, so it runs once however many
  times a retry fires or is requested; the agent's reply and the turn's
  `ok` are written in one transaction.
- A long thread is summarised by a capability-free `summary` run and then
  continues; the summary is a fenced `system` turn, the old turns stay.
- The chat prompt also lists what is open around the thread
  (updates waiting for the admin, held versions, open health items); each
  reply carries a "sources" footer built from the run's audit rows (what
  was read, what was refused), never from the model's own account; and an
  approval request made in the run is shown under the reply with the
  plan's own title and versions.
- An investigation (`task.investigate`) runs as up to three `investigate`
  steps in the background. Its progress is one `system` card in the thread
  (rewritten in place), each step's findings an agent turn, and the admin
  can cancel it from the card: a pending step is cancelled, a running one
  is killed. Steps are normal runs under every usage cap; a blocked step
  waits for the block to lift (at most three times) and then stops.

Chat and follow-ups run one at a time, so they wait for the single Claude
slot without holding dispatcher slots, and a flood of replies cannot crowd
out the health check. Limits: 30 chat turns and 20 chat runs a day, the
`user` budget class, 3 follow-up runs a day, 30 manual retries a day.
Every turn is audited. Panel messages keep the `admin-chat` origin.
"""

import asyncio
import dataclasses
import hashlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from opsd import proposals
from opsd.activity import ActivityLog
from opsd.capabilities import CAPABILITIES
from opsd.channels import REPLY_ACTION, Action, ActionKind, BudgetClass, Message, agent_message, task_from_tag, task_tag
from opsd.chatcards import error_card, plan_retry, refusal_card, split_reason
from opsd.conversation import (
    CHAT_TURNS_PER_DAY,
    CONTEXT_TURNS,
    FINDINGS_CHARS,
    MAX_FINDINGS,
    MESSAGE_CHARS,
    SUMMARY_CHARS,
    SUMMARY_EVERY,
    THREAD_ID_RE,
    build_chat_prompt,
    build_followup_prompt,
    build_investigation_prompt,
    build_summary_prompt,
    clean_text,
    new_thread_id,
    sources_line,
    sources_of,
    step_label,
    validate_investigation,
    validate_reply,
)
from opsd.events import Event
from opsd.live import Hub
from opsd.notify import deep_link
from opsd.policy import Origin
from opsd.redact import Redactor
from opsd.runctx import AGENT_SELF, Principal, RunContext
from opsd.runner import BLOCKED_STATUSES, CHAT, FOLLOWUP, INVESTIGATE, SUMMARY, AgentRunner
from opsd.store import IN_FLIGHT, RETRYABLE, Store, StoreError, Task, Turn

_LOGGER = logging.getLogger(__name__)

MAX_JOBS = 10
CHAT_WAIT_S = 25 * 60.0
MAX_FOLLOWUP_RUNS_PER_DAY = 3
FOLLOWUP_RETRY = timedelta(hours=6)
MAX_TASK_ATTEMPTS = 3
SNOOZE = timedelta(days=1)
MAX_MANUAL_RETRIES_PER_DAY = 30
# The one conversation with the admin.
MAIN_THREAD_KEY = "chat.main_thread"
RETRY_QUEUE_FULL = timedelta(minutes=15)
MAX_STEPS = 5
MAX_INVESTIGATION_STEPS = 3
# A blocked step (share cap, usage limit) waits this many times, then the investigation stops.
MAX_INVESTIGATION_WAITS = 3
INVESTIGATION_RETRY = timedelta(hours=1)
INVESTIGATION_MAX_WAIT = timedelta(hours=24)
# The next step's fire time must differ from the last one (the at-least-once guard keys on it).
STEP_GAP = timedelta(seconds=5)
CANCEL_KEY = "investigation.cancel:"
# Run ids an investigation keeps (its approval requests are voided if it is cancelled).
MAX_RUN_IDS = 6
INVESTIGATION_LIVE = ("queued", "running", "next", "waiting")
VERBS = {c.name: c.verb for c in CAPABILITIES.values()}

Job = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Submitted:
    """What happened to a submitted message: `queued`, `duplicate`, `in_flight`, `refused` or `failed`."""

    result: str
    turn: Turn | None = None
    position: int = 0


class Assistant:
    def __init__(
        self,
        *,
        store: Store,
        runner: AgentRunner,
        send: Callable[[Message], Awaitable[str]],
        redactor: Callable[[], Redactor],
        tz: Callable[[], tzinfo],
        day: Callable[[datetime], str],
        hub: Hub | None = None,
        context: Callable[[], str] | None = None,
        wake: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        # What is open around a chat (updates, health items), built by the daemon.
        self._context = context
        # Wakes the task scheduler (an investigation is due at once).
        self.wake = wake or (lambda: None)
        # Investigation card per thread while its step runs (for the live step labels).
        self._investigating: dict[str, int] = {}
        self.runner = runner
        self._send = send
        self._redactor = redactor
        self._tz = tz
        self._day = day
        self._jobs: asyncio.Queue[Job] = asyncio.Queue(maxsize=MAX_JOBS)
        self.dropped = 0
        self.hub = hub if hub is not None else Hub()
        # Chat turns waiting for the worker, in order (for the queue position).
        self._pending: list[int] = []
        # Live status per in-flight turn, and which turn a thread's run answers.
        self._live: dict[int, dict[str, Any]] = {}
        self._running: dict[str, int] = {}
        # The activity log (set by the daemon); entries that need the admin post in the chat.
        self.activity: ActivityLog | None = None
        self._main: str | None = None

    # --------------------------------------------------------------- jobs

    def _enqueue(self, job: Job, what: str) -> bool:
        try:
            self._jobs.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            _LOGGER.warning("assistant job queue full; dropped", extra={"job": what})
            return False
        return True

    async def worker(self) -> None:
        while True:
            job = await self._jobs.get()
            try:
                await job()
            except Exception:
                _LOGGER.exception("assistant job crashed")
            finally:
                self._jobs.task_done()

    async def drain(self) -> None:
        """Run every queued job now (tests and `opsd once`)."""
        while not self._jobs.empty():
            job = self._jobs.get_nowait()
            try:
                await job()
            finally:
                self._jobs.task_done()

    def audit(
        self, capability: str, principal: str, *, args: dict[str, object], outcome: str, detail: str = ""
    ) -> None:
        try:
            self.store.audit(
                at=datetime.now(UTC), run_id=None, principal=principal, channel="assistant", capability=capability,
                args=args, tier=None, decision=outcome, outcome=outcome, detail=detail,
            )  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    # ------------------------------------------------------------ pushes

    def main_thread(self) -> str:
        """The one conversation with the admin: panel messages and push replies all land here."""
        if self._main is not None and self.store.get_thread(self._main) is not None:
            return self._main
        found = self.store.kv_get(MAIN_THREAD_KEY)
        if found and THREAD_ID_RE.fullmatch(found) and self.store.get_thread(found) is not None:
            self._main = found
            return found
        now = datetime.now(UTC)
        thread = new_thread_id()
        self.store.create_thread(thread, principal="admin", channel="panel", now=now)
        self.store.kv_set(MAIN_THREAD_KEY, thread, now=now)
        self._main = thread
        self.thread_changed(thread)
        return thread

    def post_activity(self, entry: dict[str, Any], text: str | None) -> int | None:
        """One short agent message in the chat for an activity entry that needs the admin."""
        body = text or (f"{entry['title']}. {clean_text(str(entry['detail']), 300)}" if entry["detail"]
                        else str(entry["title"]))  # fmt: skip
        turn = self.store.append_turn(self.main_thread(), role="agent", text=f"{body}\n(activity #{entry['id']})",
                                      now=datetime.now(UTC), meta={"activity": entry["id"]})  # fmt: skip
        self._turn_changed(turn)
        self.thread_changed(turn.thread_id)
        return turn.id

    def note(self, kind: str, title: str, detail: str = "", *, needs_admin: bool = False,
             ref: dict[str, Any] | None = None, chat_text: str | None = None) -> None:  # fmt: skip
        if self.activity is None:
            return
        task = (ref or {}).get("task")
        if isinstance(task, int):
            # About a task (a reminder, a follow-up, an investigation): an event on that task's item.
            self.activity.record(kind, title, detail, needs_admin=needs_admin, ref=ref, chat_text=chat_text,
                                 item=f"task:{task}")  # fmt: skip
            return
        self.activity.record(kind, title, detail, needs_admin=needs_admin, ref=ref, chat_text=chat_text)

    def with_reply(self, message: Message, *, now: datetime, source: str = "alert") -> Message:
        """A push that needs the admin: an activity entry, one message in the chat, and a Reply button into it.

        Reminders are logged by `deliver_reminder` and post nothing in the chat.
        """
        title = message.title.removeprefix("Agent: ")
        try:
            thread = self.main_thread()
            if source != "reminder":
                kind = "followup" if source == "followup" else "notice"
                self.note(kind, title, message.body, needs_admin=True,
                          chat_text=message.body if source == "followup" else None)  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.warning("no chat for push; sent without reply", extra={"error": str(err)[:200]})
            return message
        return Message(
            message.title, message.body, urgent=message.urgent, tag=message.tag, thread_id=thread,
            actions=(*message.actions, REPLY_ACTION), budget_class=message.budget_class, url=deep_link(thread),
            quiet_ok=message.quiet_ok,
        )  # fmt: skip

    # --------------------------------------------------------------- live

    @property
    def queued(self) -> int:
        return len(self._pending)

    def publish(self, kind: str, thread: str | None, data: dict[str, Any]) -> None:
        self.hub.publish(kind, thread, data)

    def _turn_changed(self, turn: Turn | None) -> None:
        if turn is not None:
            self.publish("turn", turn.thread_id, turn.to_json())

    def thread_changed(self, thread: str) -> None:
        try:
            row = self.store.get_thread(thread)
        except StoreError, sqlite3.Error:
            return
        if row is not None:
            self.publish("thread", None, row)

    def _waiting_for(self) -> str | None:
        current = self.runner.current
        return current.profile if current is not None else None

    def _status(self, turn: Turn, state: str, *, step: str | None = None) -> None:
        live = self._live.setdefault(
            turn.id, {"turn_id": turn.id, "started_at": datetime.now(UTC).isoformat(timespec="seconds"), "steps": []}
        )
        live["state"] = state
        if state == "queued":
            live["position"] = self._pending.index(turn.id) + 1 if turn.id in self._pending else 0
            live["waiting_for"] = self._waiting_for()
        elif state == "waiting":
            live["waiting_for"] = self._waiting_for()
        if step is not None and (not live["steps"] or live["steps"][-1] != step):
            live["steps"] = [*live["steps"], step][-MAX_STEPS:]
        self.publish("status", turn.thread_id, dict(live))

    def _queue_changed(self) -> None:
        for turn_id in list(self._pending):
            turn = self.store.get_turn(turn_id)
            if turn is not None:
                self._status(turn, "queued")

    def live_status(self, thread: str) -> dict[str, Any] | None:
        """The thread's in-flight turn and what it is doing, for a panel (re)load."""
        turn = self.store.in_flight(thread)
        if turn is None:
            return None
        live = self._live.get(turn.id)
        if live is not None:
            return dict(live)
        position = self._pending.index(turn.id) + 1 if turn.id in self._pending else 0
        return {"turn_id": turn.id, "state": turn.status, "position": position, "steps": [],
                "waiting_for": self._waiting_for()}  # fmt: skip

    def on_capability(self, run: RunContext, capability: str) -> None:
        """A capability request from a chat or investigation run: show its fixed step label."""
        if run.thread_id is None:
            return
        if run.profile == INVESTIGATE.name:
            self._investigation_step(run.thread_id, step_label(capability))
            return
        if run.profile != CHAT.name:
            return
        turn_id = self._running.get(run.thread_id)
        turn = self.store.get_turn(turn_id) if turn_id is not None else None
        if turn is not None:
            self._status(turn, "running", step=step_label(capability))

    async def _push_unless_watching(self, thread: str, message: Message, *, what: str) -> str:
        """Chat output is pushed only when the admin is not looking at the thread."""
        if self.hub.is_watching(thread):
            self.audit("chat.push", "agent", args={"thread": thread, "what": what}, outcome="skipped",
                        detail="push skipped: watching")  # fmt: skip
            return "skipped_watching"
        return await self._send(message)

    # ---------------------------------------------------------- reminders

    async def on_task_fired(self, task: Task) -> None:
        now = datetime.now(UTC)
        fire_at = task.next_fire_at or ""
        if not self.store.mark_fired(task.id, fire_at, now=now):
            # Already delivered before a crash or restart; do not repeat it.
            self.store.finish_task(task.id, "done", now=now)
            return
        if task.kind == "reminder":
            await self.deliver_reminder(task, now)
            self.store.finish_task(task.id, "done", now=now)
            self.publish("tasks", None, {})
        elif task.kind == "followup" and task.payload.get("investigation"):
            if not self._enqueue(lambda: self.run_investigation(task), "investigation"):
                self.store.reschedule(task.id, next_fire_at=now + RETRY_QUEUE_FULL, now=now)
        elif task.kind == "followup":
            if not self._enqueue(lambda: self.run_followup(task), "followup"):
                self.store.reschedule(task.id, next_fire_at=now + FOLLOWUP_RETRY, now=now)
        else:
            _LOGGER.warning("no handler for task kind; marking failed", extra={"task_id": task.id, "kind": task.kind})
            self.store.finish_task(task.id, "failed", now=now)

    async def deliver_reminder(self, task: Task, now: datetime) -> str:
        actions: tuple[Action, ...] = (Action(ActionKind.DONE, "Done"), Action(ActionKind.SNOOZE, "Snooze 1 day"))
        note = str(task.payload.get("note") or "")
        label = "Agent-scheduled" if task.owner == "agent" else "Reminder"
        message = agent_message(
            f"{label}: {task.title}", note or task.title, urgent=False, budget_class=BudgetClass.USER,
            tag_suffix=task_tag(task.id), actions=actions,
        )  # fmt: skip
        if task.owner == "admin":
            # The admin chose this time, quiet hours or not.
            message = dataclasses.replace(message, quiet_ok=True)
        if task.payload.get("reply") and task.owner == "admin":
            message = self.with_reply(message, now=now, source="reminder")
        outcome = await self._send(message)
        self.audit("task.fired", "system", args={"task": task.id, "kind": task.kind}, outcome=outcome)
        self.note("reminder", f"{label}: {task.title}", note, ref={"task": task.id})
        return outcome

    async def run_followup(self, task: Task) -> None:
        now = datetime.now(UTC)
        if not self.store.bump(self._day(now), "followup_runs", limit=MAX_FOLLOWUP_RUNS_PER_DAY):
            if task.attempts >= MAX_TASK_ATTEMPTS:
                self.store.finish_task(task.id, "failed", now=now)
            else:
                self.store.reschedule(task.id, next_fire_at=now + FOLLOWUP_RETRY, now=now)
            self.audit("task.fired", "system", args={"task": task.id}, outcome="deferred", detail="daily cap")
            return
        principal = _principal(task)
        redactor = self._redactor()
        outcome = await self.runner.run_agent(
            FOLLOWUP, redactor.redact(build_followup_prompt(task, now=now, tz=self._tz())), principal=principal,
            now=now, redactor=redactor, generation=task.generation, wait_s=CHAT_WAIT_S,
        )  # fmt: skip
        self.store.finish_task(task.id, "done" if outcome.run.status == "ok" else "failed", now=now)
        reply, summary = validate_reply(outcome.run.structured) if outcome.run.status == "ok" else (None, "")
        self.audit("task.fired", principal.label(), args={"task": task.id, "run": outcome.context.run_id},
                    outcome=outcome.run.status, detail=summary)  # fmt: skip
        if not reply:
            self.note("followup", f"Follow-up: {task.title}",
                      "nothing to report" if outcome.run.status == "ok" else f"not run: {outcome.run.status}",
                      ref={"task": task.id})  # fmt: skip
        if reply:
            budget = BudgetClass.USER if task.payload.get("origin") != str(Origin.AGENT_SELF) else BudgetClass.AGENT
            message = agent_message(f"Follow-up: {task.title}", redactor.redact(reply), urgent=False,
                                    budget_class=budget, tag_suffix=task_tag(task.id))  # fmt: skip
            await self._send(self.with_reply(message, now=now, source="followup"))

    async def on_user_action(self, event: Event) -> None:
        """Done or Snooze on a reminder push (admin-chat origin: schedule effects only)."""
        action = event.payload.get("action")
        task_id = task_from_tag(event.payload.get("tag"))
        now = datetime.now(UTC)
        outcome = "ignored"
        if task_id is not None and action == "SNOOZE":
            outcome = (
                "snoozed" if self.store.reopen_task(task_id, next_fire_at=now + SNOOZE, now=now) else "not snoozable"
            )
        elif task_id is not None and action == "DONE":
            # Delivered reminders are already done; a snoozed one is closed here.
            self.store.mark_done(task_id, now=now)
            outcome = "done"
        self.audit("task.action", event.principal.label(), args={"task": task_id, "action": str(action)},
                    outcome=outcome)  # fmt: skip
        self.publish("tasks", None, {})

    # ---------------------------------------------------------------- chat

    async def on_user_message(self, event: Event) -> None:
        """A verified push reply: continue its thread (or a new one) like a panel message."""
        text = event.payload.get("reply_text")
        if not isinstance(text, str) or not text.strip():
            self.audit("chat.turn", event.principal.label(), args={"role": "admin"}, outcome="empty")
            return
        now = datetime.now(UTC)
        thread = self._thread_for(event, now)
        action = event.payload.get("action_id")
        # The action id is single use; as a client id it also makes a redelivered event a no-op.
        client_id = hashlib.sha256(action.encode()).hexdigest()[:32] if isinstance(action, str) else None
        await self.submit(thread, text, principal=event.principal, channel=event.source, client_id=client_id,
                    exclusive=False, via="notification" if event.source == "companion" else None)  # fmt: skip

    def _thread_for(self, event: Event, now: datetime) -> str:
        """Every push reply continues the one chat."""
        return self.main_thread()

    async def submit(
        self,
        thread: str,
        text: str,
        *,
        principal: Principal,
        channel: str,
        client_id: str | None = None,
        exclusive: bool = True,
        via: str | None = None,
    ) -> Submitted:
        """Store the admin's message at once, then queue its answer.

        `exclusive` (the panel): at most one message in flight per thread.
        The same `client_id` returns the stored turn instead of a second one.
        The message is stored even when the daily cap refuses it.
        """
        now = datetime.now(UTC)
        text = clean_text(text, MESSAGE_CHARS)
        if client_id is not None:
            existing = self.store.turn_by_client(thread, client_id)
            if existing is not None:
                return Submitted("duplicate", existing, self._position(existing.id))
        if exclusive:
            busy = self.store.in_flight(thread)
            if busy is not None:
                return Submitted("in_flight", busy, self._position(busy.id))
        meta: dict[str, Any] = {"user": principal.user_id, "channel": channel}
        if via is not None:
            meta["via"] = via
        turn, created = self.store.submit_turn(thread, text=text, now=now, client_id=client_id, meta=meta)
        if not created:
            return Submitted("duplicate", turn, self._position(turn.id))
        who = principal.label()
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        self._turn_changed(turn)
        if not self.store.bump(self._day(now), "chat_turns", limit=CHAT_TURNS_PER_DAY):
            self.audit("chat.turn", who, args={"role": "admin", "thread": thread, "sha": digest}, outcome="refused",
                        detail="daily cap")  # fmt: skip
            card = refusal_card("chat_cap", "", now=now, tz=self._tz(), reset_at=self._midnight(now),
                                limit=CHAT_TURNS_PER_DAY)  # fmt: skip
            refused = self._write_card(turn, "refused", card.status, card.text, card.meta, now)
            self.thread_changed(thread)
            if self.store.bump(self._day(now), "chat_cap_notice", limit=1):
                notice = agent_message("Chat limit", "Today's chat limit is reached; try again tomorrow.",
                                       urgent=False, budget_class=BudgetClass.USER, url=deep_link(thread))  # fmt: skip
                await self._push_unless_watching(thread, notice, what="chat_cap")
            return Submitted("refused", refused or turn)
        self.audit("chat.turn", who, args={"role": "admin", "thread": thread, "chars": len(text), "sha": digest},
                    outcome="accepted")  # fmt: skip
        if not self._enqueue_turn(turn):
            self.audit("chat.turn", who, args={"role": "admin", "thread": thread}, outcome="dropped",
                        detail="queue full")  # fmt: skip
            card = refusal_card("busy", "", now=now, tz=self._tz())
            failed = self._write_card(turn, "failed", card.status, card.text, card.meta, now, expect=("queued",))
            return Submitted("failed", failed or turn)
        self.thread_changed(thread)
        return Submitted("queued", turn, self._position(turn.id))

    def _position(self, turn_id: int) -> int:
        return self._pending.index(turn_id) + 1 if turn_id in self._pending else 0

    def _enqueue_turn(self, turn: Turn) -> bool:
        if not self._enqueue(lambda: self._run_turn(turn.id), "chat"):
            return False
        self._pending.append(turn.id)
        self._status(turn, "queued")
        return True

    def _write_card(  # noqa: PLR0917 - internal helper with one call shape
        self,
        turn: Turn,
        outcome: str,
        status: str,
        text: str,
        meta: dict[str, Any],
        now: datetime,
        *,
        expect: tuple[str, ...] = IN_FLIGHT,
        turn_meta: dict[str, Any] | None = None,
    ) -> Turn | None:
        result = self.store.refuse_turn(turn.id, outcome=outcome, card=status, text=text, now=now, meta=meta,
                                        turn_meta=turn_meta, expect=expect)  # fmt: skip
        if result is None:
            return None
        admin, card = result
        self._live.pop(turn.id, None)
        self._turn_changed(admin)
        self._turn_changed(card)
        self.publish("status", turn.thread_id, {"turn_id": turn.id, "state": outcome})
        return admin

    def _midnight(self, now: datetime) -> datetime:
        tz = self._tz()
        local = now.astimezone(tz)
        return datetime.combine(local.date() + timedelta(days=1), time(0, 0), tz).astimezone(UTC)

    def _reset_hint(self, reason: str, detail: str, now: datetime) -> datetime | None:
        """The earliest time the block can lift, from the daemon's own figures."""
        try:
            if reason == "usage_limited":
                return self.runner.control.backoff_until()
            if reason in ("share_cap", "share_reserve") and detail == "seven_day":
                return self.store.window_frees_at(at=now, span=timedelta(days=7))
            if reason in ("share_cap", "share_reserve"):
                return self.store.window_frees_at(at=now)
            if reason == "run_cap_reached":
                return self._midnight(now)
        except (StoreError, sqlite3.Error, ValueError) as err:
            _LOGGER.warning("no reset time for refusal", extra={"reason": reason, "error": str(err)[:200]})
        return None

    async def _run_turn(self, turn_id: int) -> None:
        if turn_id in self._pending:
            self._pending.remove(turn_id)
        if not self.store.set_turn_status(turn_id, "running", expect=("queued",)):
            # Cancelled, answered or already taken by another job: run nothing.
            self._queue_changed()
            return
        turn = self.store.get_turn(turn_id)
        if turn is None:
            return
        self._queue_changed()
        self._running[turn.thread_id] = turn.id
        self._status(turn, "waiting" if self.runner.busy else "running")
        self.publish("state", None, {})
        try:
            await self._answer(turn)
        except Exception:
            _LOGGER.exception("chat turn crashed", extra={"turn": turn.id})
            card = error_card("crashed", now=datetime.now(UTC))
            self._write_card(turn, "failed", card.status, card.text, card.meta, datetime.now(UTC),
                             expect=("running",))  # fmt: skip
        finally:
            self._running.pop(turn.thread_id, None)
            self._live.pop(turn.id, None)
            self.thread_changed(turn.thread_id)
            self.publish("state", None, {})

    async def _answer(self, turn: Turn) -> None:
        principal = _chat_principal(turn.meta)
        channel = str(turn.meta.get("channel") or "panel")
        thread = turn.thread_id
        redactor = self._redactor()
        await self._maybe_summarise(turn, principal, redactor)
        now = datetime.now(UTC)
        summary = self.store.latest_summary(thread)
        covers = _covers(summary)
        context = self.store.context_turns(thread, after=covers, before=turn.id, limit=CONTEXT_TURNS)
        prompt = build_chat_prompt(
            turn.text, [t.to_json() for t in context], self.store.open_tasks(), now=now, tz=self._tz(),
            channel=channel, summary=summary.text if summary else None, thread=thread, context=self._context_text(),
        )  # fmt: skip
        outcome = await self.runner.run_agent(
            CHAT, redactor.redact(prompt), principal=principal, now=now, redactor=redactor, thread_id=thread,
            wait_s=CHAT_WAIT_S, on_slot=lambda _ctx: self._status(turn, "running"),
        )  # fmt: skip
        run = outcome.run
        self.publish("tasks", None, {})
        if run.status in BLOCKED_STATUSES or run.status == "skipped_busy":
            await self._refused(turn, run.status, run.error)
            return
        reply, _summary = validate_reply(run.structured) if run.status == "ok" else (None, "")
        if reply is None:
            await self._failed(turn, run.status if run.status != "ok" else "no_reply")
            return
        reply = redactor.redact(reply)
        run_meta = self._run_meta(outcome.context.run_id)
        agent = self.store.complete_turn(turn.id, text=reply, now=datetime.now(UTC), run_id=outcome.context.run_id,
                                         meta={"duration_s": round(run.duration_s, 1), "turns": run.turns,
                                               **run_meta})  # fmt: skip
        if agent is None:
            return
        self._after_chat_run(outcome.context.run_id, thread, run_meta)
        self._turn_changed(self.store.get_turn(turn.id))
        card = self.store.card_for(turn.id)
        if card is not None:
            self._turn_changed(self.store.update_turn_meta(card.id, {"resolved": True, "auto_retry": False,
                                                                     "retry_at": None, "actions": []}))  # fmt: skip
        footer = sources_line(run_meta)
        delivered = await self._push_unless_watching(thread, agent_message(
            "Reply", f"{reply}\n\n{footer}" if footer else reply, urgent=False, budget_class=BudgetClass.USER,
            tag_suffix=f"chat-{thread}",
            thread_id=thread, actions=(REPLY_ACTION,), url=deep_link(thread)), what="reply")  # fmt: skip
        self._turn_changed(self.store.update_turn_meta(agent.id, {"push": delivered}))
        self.publish("status", thread, {"turn_id": turn.id, "state": "ok"})
        self.audit("chat.turn", "agent", args={"role": "agent", "thread": thread, "run": outcome.context.run_id,
                    "chars": len(reply)}, outcome=run.status, detail=f"push {delivered}")  # fmt: skip

    def _context_text(self) -> str:
        if self._context is None:
            return ""
        try:
            return self._context()
        except (StoreError, sqlite3.Error, OSError, ValueError) as err:
            _LOGGER.warning("no chat context", extra={"error": str(err)[:200]})
            return ""

    def _run_meta(self, run_id: str) -> dict[str, Any]:
        """Sources, refusals and approval requests of a run, from the daemon's own records."""
        try:
            meta: dict[str, Any] = dict(sources_of(self.store.audit_rows(run_id=run_id, limit=300), VERBS))
            meta["proposals"] = proposals.for_run(self.store, run_id)
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.warning("no sources for run", extra={"error": str(err)[:200]})
            return {}
        return {k: v for k, v in meta.items() if v}

    def _after_chat_run(self, run_id: str, thread: str, run_meta: dict[str, Any]) -> None:
        """Cards for investigations the run started; approval requests are on the update's item on Home."""
        now = datetime.now(UTC)
        started = False
        for task in self.store.tasks_created_by(f"{CHAT.name}:{run_id}"):
            if task.payload.get("investigation") and task.thread_id == thread and task.state == "pending":
                self._investigation_card(task, now)
                self.note("investigation", f"Investigation started: {task.title}", ref={"task": task.id})
                started = True
        if started:
            self.publish("tasks", None, {})
            self.wake()
        self._proposal_cards(thread, run_meta, now)

    def _proposal_cards(self, thread: str, run_meta: dict[str, Any], now: datetime) -> None:
        """A daemon card per approval request, after the reply: plan data only, and where to decide."""
        for found in run_meta.get("proposals") or []:
            if found.get("kind") == "backup":
                self._turn_changed(self.store.append_turn(
                    thread, role="system", status="proposal", now=now,
                    text="Back up now? A complete, encrypted backup with your automatic backup settings. Nothing"
                         " runs until you tap Back up now.",
                    meta={"actions": ["backup"]}))  # fmt: skip
                continue
            text = (f"{clean_text(str(found['title']), 60)} {clean_text(str(found['from']), 40)} →"
                    f" {clean_text(str(found['to']), 40)}. Nothing runs until you approve it on its item in Needs you"
                    " on Home. You can also reject it there.")  # fmt: skip
            self._turn_changed(self.store.append_turn(thread, role="system", status="proposal", text=text, now=now,
                                                      meta={"entity_id": found["entity_id"],
                                                            "actions": ["updates"]}))  # fmt: skip
        if run_meta.get("proposals"):
            self.publish("updates", None, {})

    async def _refused(self, turn: Turn, status: str, error: str) -> None:
        """A cap or control refused the run: a card, and an automatic retry when the block can lift."""
        now = datetime.now(UTC)
        current = self.store.get_turn(turn.id) or turn
        reason, detail = split_reason(status, error)
        first = current.meta.get("first_refused_at")
        first_at = datetime.fromisoformat(first) if isinstance(first, str) else now
        attempt = int(current.meta.get("retries") or 0)
        auto_off = bool(current.meta.get("auto_retry_off"))
        hint = self._reset_hint(reason, detail, now)
        retry_at = None if auto_off else plan_retry(reason, attempt=attempt, first_at=first_at, now=now, hint=hint)
        card = refusal_card(reason, detail, now=now, tz=self._tz(), reset_at=hint, retry_at=retry_at,
                            attempt=attempt, auto_off=auto_off)  # fmt: skip
        written = self._write_card(turn, "refused", card.status, card.text, card.meta, now, expect=("running",),
                                   turn_meta={"first_refused_at": first_at.isoformat(timespec="seconds")})  # fmt: skip
        if written is None:
            return
        if retry_at is not None:
            self.store.schedule_retry(turn.id, at=retry_at, attempt=attempt + 1, thread_id=turn.thread_id, now=now)
        self.audit("chat.turn", "agent", args={"role": "agent", "thread": turn.thread_id, "turn": turn.id},
                    outcome=status, detail=f"retry at {retry_at.isoformat() if retry_at else 'none'}")  # fmt: skip
        if card.meta["gave_up"]:
            await self._push_unless_watching(turn.thread_id, agent_message(
                "Message not answered", f"I could not answer your message: {card.text}", urgent=False,
                budget_class=BudgetClass.USER, url=deep_link(turn.thread_id)), what="gave_up")  # fmt: skip

    async def _failed(self, turn: Turn, status: str) -> None:
        now = datetime.now(UTC)
        card = error_card(status, now=now)
        if self._write_card(turn, "failed", card.status, card.text, card.meta, now, expect=("running",)) is None:
            return
        self.audit("chat.turn", "agent", args={"role": "agent", "thread": turn.thread_id, "turn": turn.id},
                    outcome=status, detail="failed")  # fmt: skip
        await self._push_unless_watching(turn.thread_id, agent_message(
            "Reply failed", card.text, urgent=False, budget_class=BudgetClass.USER, tag_suffix=f"chat-{turn.thread_id}",
            url=deep_link(turn.thread_id)), what="failed")  # fmt: skip

    async def _maybe_summarise(self, turn: Turn, principal: Principal, redactor: Redactor) -> None:
        """Condense a long thread before answering, so it can go on (a no-op for short threads)."""
        thread = turn.thread_id
        summary = self.store.latest_summary(thread)
        covers = _covers(summary)
        if self.store.get_thread(thread) is None:
            _LOGGER.debug("thread deleted; no summary", extra={"thread_id": thread})
            return
        if self.store.count_context(thread, after=covers, before=turn.id) < SUMMARY_EVERY:
            return
        older = self.store.context_turns(thread, after=covers, before=turn.id, limit=SUMMARY_EVERY * 2)
        # The last few turns stay verbatim; the summary covers everything before them.
        body = older[:-CONTEXT_TURNS]
        if not body:
            return
        now = datetime.now(UTC)
        self._status(turn, "summarising")
        prompt = build_summary_prompt(summary.text if summary else None, [t.to_json() for t in body])
        outcome = await self.runner.run_agent(SUMMARY, redactor.redact(prompt), principal=principal, now=now,
                                              redactor=redactor, thread_id=thread, wait_s=CHAT_WAIT_S)  # fmt: skip
        text, _ = validate_reply(outcome.run.structured) if outcome.run.status == "ok" else (None, "")
        self.audit("chat.summary", principal.label(), args={"thread": thread, "turns": len(body)},
                    outcome=outcome.run.status if text else f"{outcome.run.status}:empty")  # fmt: skip
        if not text:
            return
        if self.store.get_thread(thread) is None:
            _LOGGER.debug("thread deleted during its summary; nothing stored", extra={"thread_id": thread})
            return
        written = self.store.append_turn(
            thread, role="system", status="summary", text=redactor.redact(clean_text(text, SUMMARY_CHARS)),
            now=datetime.now(UTC), run_id=outcome.context.run_id, meta={"covers_to": body[-1].id, "turns": len(body)},
        )  # fmt: skip
        self._turn_changed(written)

    # ------------------------------------------------------ investigations

    def _card_text(self, task: Task, state: str, step: int, of: int, detail: str = "") -> str:
        """Daemon text only (the title is the admin's topic as the chat run worded it)."""
        body = {
            "queued": "Starts shortly.",
            "running": f"Step {step} of {of} running" + (f": {detail}." if detail else "."),
            "next": f"Step {step} of {of} done; continuing.",
            "waiting": f"Waiting: {detail}",
            "done": f"Finished after {step} step{'s' if step != 1 else ''}.",
            "cancelled": "Cancelled.",
            "failed": f"Stopped: {detail}",
        }[state]
        return f"{clean_text(task.title, 120)}. {body}"

    def _investigation_card(self, task: Task, now: datetime) -> Turn:
        thread = task.thread_id or ""
        card = self.store.card_by_status(thread, "investigation", task.id)
        if card is not None:
            return card
        of = int(task.payload.get("max_steps") or 1)
        card = self.store.append_turn(thread, role="system", status="investigation", now=now,
                                      text=self._card_text(task, "queued", 0, of),
                                      meta={"task": task.id, "state": "queued", "step": 0, "of": of, "steps": [],
                                            "actions": ["cancel"]})  # fmt: skip
        self._turn_changed(card)
        self.thread_changed(thread)
        return card

    def _set_card(self, card: Turn, task: Task, state: str, step: int, of: int, *, detail: str = "",
                  steps: list[str] | None = None) -> None:  # fmt: skip
        live = state in INVESTIGATION_LIVE
        meta: dict[str, Any] = {"state": state, "step": step, "of": of, "actions": ["cancel"] if live else [],
                                "done": not live}  # fmt: skip
        if steps is not None:
            meta["steps"] = steps
        updated = self.store.update_card(card.id, text=self._card_text(task, state, step, of, detail), meta=meta,
                                         now=datetime.now(UTC))  # fmt: skip
        self._turn_changed(updated)
        if updated is not None:
            self.thread_changed(updated.thread_id)

    def _investigation_step(self, thread: str, label: str) -> None:
        """The running step's card names what it is doing (fixed labels, never model text)."""
        card_id = self._investigating.get(thread)
        card = self.store.get_turn(card_id) if card_id is not None else None
        if card is None:
            return
        steps = [str(x) for x in card.meta.get("steps") or []]
        task = self.store.get_task(int(card.meta.get("task") or 0))
        if task is None or (steps and steps[-1] == label):
            return
        step, of = int(card.meta.get("step") or 1), int(card.meta.get("of") or 1)
        self._set_card(card, task, "running", step, of, detail=label, steps=[*steps, label][-MAX_STEPS:])

    def _cancel_requested(self, task_id: int) -> bool:
        return bool(self.store.kv_get(f"{CANCEL_KEY}{task_id}"))

    def _void_requests(self, run_ids: list[str], now: datetime) -> None:
        if proposals.void_runs(self.store, run_ids, now=now, detail="the investigation was cancelled"):
            self.publish("updates", None, {})

    async def _end_investigation(
        self, task: Task, card: Turn, state: str, step: int, of: int, *, detail: str = "", runs: list[str] | None = None
    ) -> None:
        now = datetime.now(UTC)
        self.store.finish_task(task.id, state, now=now)
        if state == "cancelled":
            self._void_requests(runs if runs is not None else _run_ids(task.payload), now)
        self._set_card(card, task, state, step, of, detail=detail)
        self.audit("task.investigate", "system", args={"task": task.id, "step": step}, outcome=state,
                   detail=detail[:200])  # fmt: skip
        self.note("investigation", f"Investigation {state}: {task.title}", detail or f"{step} of {of} steps",
                  needs_admin=state == "failed", ref={"task": task.id})  # fmt: skip
        self.publish("tasks", None, {})
        if state == "failed" and task.thread_id:
            await self._push_unless_watching(task.thread_id, agent_message(
                f"Investigation stopped: {task.title}", detail or "It could not finish.", urgent=False,
                budget_class=BudgetClass.USER, tag_suffix=f"chat-{task.thread_id}",
                url=deep_link(task.thread_id)), what="investigation")  # fmt: skip

    async def run_investigation(self, task: Task) -> None:
        """One step of an investigation; schedules the next while the result asks for one and steps remain."""
        now = datetime.now(UTC)
        thread = task.thread_id
        if thread is None or self.store.get_thread(thread) is None:
            self.store.finish_task(task.id, "cancelled", now=now)
            self.audit("task.investigate", "system", args={"task": task.id}, outcome="cancelled", detail="no thread")
            return
        payload = dict(task.payload)
        of = max(1, min(MAX_INVESTIGATION_STEPS, int(payload.get("max_steps") or 1)))
        done = int(payload.get("step") or 0)
        step = done + 1
        card = self._investigation_card(task, now)
        if self._cancel_requested(task.id) or step > of:
            await self._end_investigation(task, card, "cancelled" if step <= of else "done", done, of)
            return
        self._set_card(card, task, "running", step, of, steps=[])
        redactor = self._redactor()
        prompt = build_investigation_prompt(task, step=step, max_steps=of, now=now, tz=self._tz(),
                                            context=self._context_text())  # fmt: skip
        self._investigating[thread] = card.id
        try:
            # Checked again once the Claude slot is held: a cancel while waiting never starts the step.
            outcome = await self.runner.run_agent(
                INVESTIGATE, redactor.redact(prompt), principal=_principal(task), now=now, redactor=redactor,
                thread_id=thread, generation=task.generation, wait_s=CHAT_WAIT_S,
                cancelled=lambda: self._cancel_requested(task.id),
            )  # fmt: skip
        finally:
            self._investigating.pop(thread, None)
        run = outcome.run
        now = datetime.now(UTC)
        payload["runs"] = [*_run_ids(payload), outcome.context.run_id][-MAX_RUN_IDS:]
        if self._cancel_requested(task.id) or run.status == "cancelled":
            await self._end_investigation(task, card, "cancelled", done, of, runs=payload["runs"])
            return
        if run.status in BLOCKED_STATUSES or run.status == "skipped_busy":
            await self._investigation_blocked(task, card, payload, run.status, run.error, done, of, now)
            return
        reply, summary, following = validate_investigation(run.structured) if run.status == "ok" else (None, "", None)
        if reply is None:
            why = "the step gave no findings" if run.status == "ok" else f"the step ended with {run.status}"
            await self._end_investigation(task, card, "failed", done, of, detail=why)
            return
        reply = redactor.redact(reply)
        run_meta = self._run_meta(outcome.context.run_id)
        findings = self.store.append_turn(thread, role="agent", text=reply, now=now, run_id=outcome.context.run_id,
                                          meta={"investigation": task.id, "step": step, "of": of,
                                                "duration_s": round(run.duration_s, 1), **run_meta})  # fmt: skip
        self._turn_changed(findings)
        self._proposal_cards(thread, run_meta, now)
        earlier = [str(f) for f in payload.get("findings") or [] if isinstance(f, str)]
        noted = clean_text(redactor.redact(summary or reply), FINDINGS_CHARS)
        payload |= {"step": step, "waits": 0, "findings": [*earlier, noted][-MAX_FINDINGS:]}
        self.note("investigation", f"Investigation step {step} of {of}: {task.title}", summary, ref={"task": task.id})
        self.audit("task.investigate", _principal(task).label(), args={"task": task.id, "step": step,
                   "run": outcome.context.run_id}, outcome="step", detail=summary)  # fmt: skip
        if following and step < of:
            payload["next"] = redactor.redact(following)
            self.store.reschedule(task.id, next_fire_at=now + STEP_GAP, now=now, payload=payload)
            self._set_card(card, task, "next", step, of)
            self.wake()
            return
        await self._end_investigation(task, card, "done", step, of)
        footer = sources_line(run_meta)
        await self._push_unless_watching(thread, agent_message(
            f"Investigation: {task.title}", f"{reply}\n\n{footer}" if footer else reply, urgent=False,
            budget_class=BudgetClass.USER, tag_suffix=f"chat-{thread}", thread_id=thread, actions=(REPLY_ACTION,),
            url=deep_link(thread)), what="investigation")  # fmt: skip

    async def _investigation_blocked(  # noqa: PLR0917 - internal step of run_investigation
        self,
        task: Task,
        card: Turn,
        payload: dict[str, Any],
        status: str,
        error: str,
        done: int,
        of: int,
        now: datetime,
    ) -> None:
        """A cap or control refused the step: wait for it to lift (a few times), then stop."""
        reason, detail = split_reason(status, error)
        text = refusal_card(reason, detail, now=now, tz=self._tz()).text
        waits = int(payload.get("waits") or 0)
        hint = self._reset_hint(reason, detail, now)
        at = max(hint or now + INVESTIGATION_RETRY, now + STEP_GAP)
        if waits >= MAX_INVESTIGATION_WAITS or at - now > INVESTIGATION_MAX_WAIT:
            await self._end_investigation(task, card, "failed", done, of, detail=text)
            return
        payload["waits"] = waits + 1
        self.store.reschedule(task.id, next_fire_at=at, now=now, payload=payload)
        local = at.astimezone(self._tz()).strftime("%a %H:%M")
        self._set_card(card, task, "waiting", done, of, detail=f"{text} Next try {local}.")
        self.audit("task.investigate", "system", args={"task": task.id}, outcome="waiting", detail=status)

    def cancel_investigation(self, thread: str, task_id: int) -> str:
        """The admin's Cancel: `ok`, `not_found` or `not_open`. A running step is stopped at once."""
        task = self.store.get_task(task_id)
        if task is None or task.thread_id != thread or not task.payload.get("investigation") or task.system_key:
            return "not_found"
        if task.state not in ("pending", "running"):
            return "not_open"
        now = datetime.now(UTC)
        # Checked before and after every step, so a cancel can never be lost to a race.
        self.store.kv_set(f"{CANCEL_KEY}{task.id}", now.isoformat(timespec="seconds"), now=now)
        if task.state == "pending" and self.store.admin_cancel_task(task.id, now=now):
            self._void_requests(_run_ids(task.payload), now)
            card = self.store.card_by_status(thread, "investigation", task.id)
            if card is not None:
                self._set_card(card, task, "cancelled", int(task.payload.get("step") or 0),
                               int(task.payload.get("max_steps") or 1))  # fmt: skip
        else:
            current = self.runner.current
            if current is not None and current.profile == INVESTIGATE.name and current.thread_id == thread:
                self.runner.kill("investigation cancelled by the admin")
        self.audit("task.investigate", "admin", args={"task": task.id, "thread": thread}, outcome="cancel")
        self.publish("tasks", None, {})
        return "ok"

    # ------------------------------------------------------------- retries

    def _admin_turn(self, thread: str, turn_id: int) -> Turn | None:
        turn = self.store.get_turn(turn_id)
        if turn is None or turn.thread_id != thread or turn.role != "admin":
            return None
        return turn

    def retry_turn(self, thread: str, turn_id: int) -> str:
        """The admin's Retry: `queued`, `not_found`, `not_retryable`, `cap` or `busy`. Runs at most once."""
        now = datetime.now(UTC)
        turn = self._admin_turn(thread, turn_id)
        if turn is None:
            return "not_found"
        if turn.status not in RETRYABLE:
            return "not_retryable"
        if not self.store.bump(self._day(now), "chat_retries", limit=MAX_MANUAL_RETRIES_PER_DAY):
            return "cap"
        fresh = {"retries": 0, "first_refused_at": None, "auto_retry_off": False}
        if not self.store.set_turn_status(turn.id, "queued", expect=RETRYABLE, meta=fresh):
            return "not_retryable"
        self.store.cancel_retry(turn.id, now=now)
        self.audit("chat.retry", "admin", args={"thread": thread, "turn": turn.id}, outcome="queued")
        return self._requeue(turn, now)

    def _requeue(self, turn: Turn, now: datetime) -> str:
        card = self.store.card_for(turn.id)
        if card is not None:
            self._turn_changed(self.store.update_turn_meta(card.id, {"retrying": True, "retry_at": None,
                                                                     "auto_retry": False, "actions": []}))  # fmt: skip
        queued = self.store.get_turn(turn.id) or turn
        self._turn_changed(queued)
        if self._enqueue_turn(queued):
            return "queued"
        busy = refusal_card("busy", "", now=now, tz=self._tz())
        self._write_card(queued, "failed", busy.status, busy.text, busy.meta, now, expect=("queued",))
        return "busy"

    def set_auto_retry(self, thread: str, turn_id: int, *, on: bool) -> str:
        """Turn automatic retry off (cancels the scheduled one) or back on: `ok`, `not_found`, `not_retryable`."""
        now = datetime.now(UTC)
        turn = self._admin_turn(thread, turn_id)
        card = self.store.card_for(turn_id) if turn is not None else None
        if turn is None or card is None:
            return "not_found"
        if turn.status != "refused":
            return "not_retryable"
        reason = str(card.meta.get("reason") or "")
        detail = str(card.meta.get("detail") or "")
        if not on:
            self.store.cancel_retry(turn.id, now=now)
            self.store.update_turn_meta(turn.id, {"auto_retry_off": True})
            updated = refusal_card(reason, detail, now=now, tz=self._tz(), reset_at=_parse(card.meta.get("reset_at")),
                                   attempt=int(turn.meta.get("retries") or 0), auto_off=True)  # fmt: skip
            self._update_card(card, updated.text, updated.meta)
            self.audit("chat.retry", "admin", args={"thread": thread, "turn": turn.id}, outcome="auto_off")
            return "ok"
        attempt = int(turn.meta.get("retries") or 0)
        hint = self._reset_hint(reason, detail, now)
        # Re-enabling keeps the original 24-hour horizon: toggling cannot extend retries indefinitely.
        first_at = _parse(turn.meta.get("first_refused_at")) or now
        retry_at = plan_retry(reason, attempt=attempt, first_at=first_at, now=now, hint=hint)
        if retry_at is None:
            return "not_retryable"
        self.store.update_turn_meta(turn.id, {"auto_retry_off": False})
        self.store.schedule_retry(turn.id, at=retry_at, attempt=attempt + 1, thread_id=thread, now=now)
        updated = refusal_card(reason, detail, now=now, tz=self._tz(), reset_at=hint, retry_at=retry_at,
                               attempt=attempt)  # fmt: skip
        self._update_card(card, updated.text, updated.meta)
        self.audit("chat.retry", "admin", args={"thread": thread, "turn": turn.id}, outcome="auto_on")
        return "ok"

    def _update_card(self, card: Turn, text: str, meta: dict[str, Any]) -> None:
        result = self.store.refuse_turn(card.reply_to or 0, outcome="refused", card=card.status, text=text,
                                        now=datetime.now(UTC), meta=meta, expect=("refused",))  # fmt: skip
        if result is not None:
            self._turn_changed(result[1])

    async def on_retry_fired(self, task: Task) -> None:
        """A scheduled automatic retry is due. At-least-once firing, at-most-once run (compare-and-set)."""
        now = datetime.now(UTC)
        if not self.store.mark_fired(task.id, task.next_fire_at or "", now=now):
            self.store.finish_task(task.id, "done", now=now)
            return
        self.store.finish_task(task.id, "done", now=now)
        turn_id = task.payload.get("turn_id")
        turn = self.store.get_turn(turn_id) if isinstance(turn_id, int) else None
        if turn is None or turn.status != "refused" or turn.meta.get("auto_retry_off"):
            self.audit("chat.retry", "system", args={"task": task.id, "turn": turn_id}, outcome="skipped")
            return
        retries = int(turn.meta.get("retries") or 0) + 1
        if not self.store.set_turn_status(turn.id, "queued", expect=("refused",), meta={"retries": retries}):
            return
        self.audit("chat.retry", "system", args={"task": task.id, "turn": turn.id, "attempt": retries},
                    outcome="queued")  # fmt: skip
        card = self.store.card_for(turn.id)
        if card is not None:
            self._turn_changed(self.store.update_turn_meta(card.id, {"retrying": True, "retry_at": None,
                                                                     "actions": []}))  # fmt: skip
        queued = self.store.get_turn(turn.id) or turn
        self._turn_changed(queued)
        if not self._enqueue_turn(queued):
            # Put it back and try again later; the attempt is not consumed.
            self.store.set_turn_status(turn.id, "refused", expect=("queued",), meta={"retries": retries - 1})
            self.store.schedule_retry(turn.id, at=now + RETRY_QUEUE_FULL, attempt=retries, thread_id=turn.thread_id,
                                      now=now)  # fmt: skip

    def recover(self, now: datetime) -> int:
        """At start: turns a restart caught queued or running get an `interrupted` card (retry by hand)."""
        stuck = self.store.stuck_turns()
        for turn in stuck:
            card = refusal_card("interrupted", "", now=now, tz=self._tz())
            self._write_card(turn, "failed", card.status, card.text, card.meta, now)
        return len(stuck)


def _covers(summary: Turn | None) -> int:
    value = summary.meta.get("covers_to") if summary is not None else None
    return value if isinstance(value, int) else 0


def _run_ids(payload: dict[str, Any]) -> list[str]:
    return [str(r) for r in payload.get("runs") or [] if isinstance(r, str)]


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _chat_principal(meta: dict[str, Any]) -> Principal:
    """Chat always runs as `admin-chat`, whatever the turn's metadata says."""
    user = meta.get("user")
    channel = meta.get("channel")
    return Principal(Origin.ADMIN_CHAT, user_id=user if isinstance(user, str) else None,
                     channel=channel if channel in ("panel", "companion") else "panel")  # fmt: skip


def _principal(task: Task) -> Principal:
    """A follow-up runs with the origin of whoever created it (never stronger)."""
    origin = task.payload.get("origin")
    if origin == str(Origin.ADMIN_CHAT) or origin == str(Origin.ADMIN):
        user = task.payload.get("user_id")
        return Principal(Origin.ADMIN_CHAT, user_id=user if isinstance(user, str) else None, channel="followup")
    return AGENT_SELF
