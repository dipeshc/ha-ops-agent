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

Chat and follow-ups run one at a time, so they wait for the single AI
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from opsd import forkrebase, proposals
from opsd.activity import ActivityLog
from opsd.capabilities import CAPABILITIES
from opsd.channels import (
    AGENT_TAG,
    REPLY_ACTION,
    Action,
    ActionKind,
    BudgetClass,
    Message,
    agent_message,
    task_from_tag,
    task_tag,
)
from opsd.chatcards import error_card, plan_retry, refusal_card, split_reason
from opsd.conversation import (
    CHAT_TURNS_PER_DAY,
    CONTEXT_TURNS,
    FINDINGS_CHARS,
    MAX_FINDINGS,
    MESSAGE_CHARS,
    SUMMARY_CHARS,
    SUMMARY_EVERY,
    build_chat_prompt,
    build_followup_prompt,
    build_investigation_prompt,
    build_summary_prompt,
    clean_text,
    sources_line,
    sources_of,
    step_label,
    validate_investigation,
    validate_reply,
)
from opsd.effects import PUSH_REASONS
from opsd.events import Event
from opsd.items import ITEM_ID_RE, ItemBook, is_question, item_context, item_id, thread_of, update_key
from opsd.items import deep_link as item_link
from opsd.live import Hub
from opsd.policy import Origin
from opsd.redact import Redactor
from opsd.runctx import AGENT_SELF, Principal, RunContext
from opsd.runner import BLOCKED_STATUSES, CHAT, FOLLOWUP, INVESTIGATE, SUMMARY, AgentRunner
from opsd.store import IN_FLIGHT, ITEM_THREAD_RE, QUESTION_PREFIX, RETRYABLE, Store, StoreError, Task, Turn
from opsd.tasks import MAX_OPEN_INVESTIGATIONS, MAX_OPEN_TASKS, MAX_TITLE_CHARS

_LOGGER = logging.getLogger(__name__)

MAX_JOBS = 10
CHAT_WAIT_S = 25 * 60.0
MAX_FOLLOWUP_RUNS_PER_DAY = 3
FOLLOWUP_RETRY = timedelta(hours=6)
MAX_TASK_ATTEMPTS = 3
SNOOZE = timedelta(days=1)
MAX_MANUAL_RETRIES_PER_DAY = 30
# A question's title: the admin's text, one line.
QUESTION_TITLE_CHARS = 120
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
# "Plan a fix" on a maintenance item: the brief of its investigation (daemon text; the item is fenced beside it).
PLAN_FIX_STEPS = 2
PLAN_FIX_BRIEF = (
    "The admin tapped Plan a fix on the item below and wants a concrete plan for it on that item. Read the evidence"
    " (the item, the repairs, the logs, the config and the integration it names) and write the plan: the steps, the"
    " risk of each, and which of them you can do yourself and which you cannot. The admin wants a fix proposed where"
    " the proposal rules allow it: then propose it, so it waits for his approval like any proposal. Where the fix"
    " is outside what you may propose (for example a custom integration's own code, an integration to replace, or"
    " anything in the UI), say so plainly, say that the admin or an engineering session must do it, and that he can"
    " use Remind me on the item to come back to it."
)
# A plan's text on its finding (the last step's findings).
PLAN_CHARS = 3000
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
        # The activity log (set by the daemon): what the agent did, on items and in History, never in a chat.
        self.activity: ActivityLog | None = None
        # The item book (set by the daemon): writes the questions the admin asks.
        self.items: ItemBook | None = None

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

    # ------------------------------------------------------------ threads

    def record_of(self, thread: str) -> dict[str, Any] | None:
        """The item a conversation belongs to (`item:<id>` or `ask:<id>`), from the store; None for anything else."""
        found = ITEM_THREAD_RE.fullmatch(thread)
        record = self.store.item(found.group(2)) if found is not None else None
        return record if record is not None and thread_of(record) == thread else None

    def writable(self, thread: str) -> bool:
        """Messages go only to the conversation of an item or a question that is stored; the earlier one chat is
        read only."""
        return self.record_of(thread) is not None

    def item_message(self, thread: str, title: str, body: str, *, reply: bool = True,
                     budget_class: BudgetClass = BudgetClass.USER) -> Message:  # fmt: skip
        """An agent push about a conversation's item: opens the item, Reply continues its conversation. It carries
        the item's tag (the push the admin replied to), except for an urgent item, whose daemon push model text never
        replaces."""
        record = self.record_of(thread)
        ident = str(record["id"]) if record is not None else ""
        message = agent_message(title, body, urgent=False, budget_class=budget_class, thread_id=thread,
                                actions=(REPLY_ACTION,) if reply and record is not None else (),
                                url=item_link(ident) if ident else None, tag_suffix=ident or None)  # fmt: skip
        if record is not None and not (record.get("source") or {}).get("urgent"):
            message = dataclasses.replace(message, tag=ident)
        return message

    def _title(self, thread: str, fallback: str) -> str:
        record = self.record_of(thread)
        return clean_text(str(record["title"]), 70) if record is not None else fallback

    def note(self, kind: str, title: str, detail: str = "", *, needs_admin: bool = False,
             ref: dict[str, Any] | None = None, chat_text: str | None = None) -> str | None:  # fmt: skip
        """An activity entry; returns the item it went on (the task's item, a finding for "Plan a fix", or its
        own standalone item)."""
        if self.activity is None:
            return None
        task = (ref or {}).get("task")
        if isinstance(task, int):
            # About a task (a reminder, a follow-up, an investigation): an event on that task's item, or on the
            # finding a "Plan a fix" runs on.
            key = self._task_item_key(task)
            entry = self.activity.record(kind, title, detail, needs_admin=needs_admin, ref=ref, chat_text=chat_text,
                                         item=key)  # fmt: skip
        else:
            entry = self.activity.record(kind, title, detail, needs_admin=needs_admin, ref=ref, chat_text=chat_text)
        found = (entry or {}).get("item")
        return found if isinstance(found, str) else None

    def _task_item_key(self, task_id: int) -> str:
        task = self.store.get_task(task_id)
        target = task.payload.get("item") if task is not None and task.payload.get("plan_fix") else None
        record = self.store.item(target) if isinstance(target, str) and ITEM_ID_RE.fullmatch(target) else None
        return str(record["key"]) if record is not None else f"task:{task_id}"

    def with_reply(self, message: Message, *, now: datetime, source: str = "alert", reply: bool = True,
                   item: str | None = None) -> Message:  # fmt: skip
        """A push that needs the admin: an activity entry (its own item), opening that item (or `item`, the task's
        item for a reminder), with a Reply button into that item's conversation.

        Reminders are logged by `deliver_reminder`. Without `reply` (a push from the agent's own run) the push opens
        the item but has no Reply button: model text never invites an answer.
        """
        del now
        title = message.title.removeprefix("Agent: ")
        target = item
        try:
            if source != "reminder":
                kind = "followup" if source == "followup" else "notice"
                noted = self.note(kind, title, message.body, needs_admin=True,
                                  chat_text=message.body if source == "followup" else None)  # fmt: skip
                target = target or noted
            record = self.store.item(target) if isinstance(target, str) and ITEM_ID_RE.fullmatch(target) else None
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.warning("no item for push; sent without reply", extra={"error": str(err)[:200]})
            return message
        thread = thread_of(record) if record is not None else None
        if record is None or thread is None:
            return message
        return Message(
            message.title, message.body, urgent=message.urgent, tag=message.tag, thread_id=thread,
            actions=(*message.actions, *((REPLY_ACTION,) if reply else ())), budget_class=message.budget_class,
            url=item_link(str(record["id"])),
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
        item = task.payload.get("item")
        message = agent_message(
            f"{label}: {task.title}", note or task.title, urgent=False, budget_class=BudgetClass.USER,
            tag_suffix=task_tag(task.id), actions=actions,
            # A reminder about an item opens that item.
            url=item_link(item) if isinstance(item, str) and ITEM_ID_RE.fullmatch(item) else None,
        )  # fmt: skip
        if task.owner == "admin":
            # The admin chose this time, quiet hours or not.
            message = dataclasses.replace(message, quiet_ok=True)
        if task.payload.get("reply") and task.owner == "admin":
            message = self.with_reply(message, now=now, source="reminder", item=item_id(f"task:{task.id}"))
        if task.owner != "admin" and task.payload.get("reason") not in PUSH_REASONS:
            # The agent's own reminder for information: History only (its words under `agent`, never a need).
            self.note("note", f"{label}: {task.title}", note)
            self.audit("task.fired", "system", args={"task": task.id, "kind": task.kind}, outcome="noted")
            return "noted"
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
        if reply and task.owner != "admin" and task.payload.get("reason") not in PUSH_REASONS:
            # The agent's own follow-up for information: History only, never a push or a need.
            self.note("note", f"Follow-up: {task.title}", redactor.redact(reply))
        elif reply:
            own = task.owner != "admin"
            budget = BudgetClass.AGENT if own else BudgetClass.USER
            message = agent_message(f"Follow-up: {task.title}", redactor.redact(reply), urgent=False,
                                    budget_class=budget, tag_suffix=task_tag(task.id))  # fmt: skip
            # The agent's own follow-up never invites a reply (N7): only the admin's own tasks do.
            await self._send(self.with_reply(message, now=now, source="followup", reply=not own))

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
        """A verified push reply: a message on the item the push was about (its conversation), else a new
        question."""
        text = event.payload.get("reply_text")
        if not isinstance(text, str) or not text.strip():
            self.audit("chat.turn", event.principal.label(), args={"role": "admin"}, outcome="empty")
            return
        action = event.payload.get("action_id")
        # The action id is single use; as a client id it also makes a redelivered event a no-op.
        client_id = hashlib.sha256(action.encode()).hexdigest()[:32] if isinstance(action, str) else None
        via = "notification" if event.source == "companion" else None
        thread = self._thread_for(event)
        if thread is None:
            await self.ask(text, principal=event.principal, channel=event.source, client_id=client_id, via=via)
            return
        await self.submit(thread, text, principal=event.principal, channel=event.source, client_id=client_id,
                          exclusive=False, via=via, item=self.record_of(thread))  # fmt: skip

    def _thread_for(self, event: Event) -> str | None:
        """The conversation of the item the push was about: the thread recorded with the action id when the push
        was sent (the daemon's record, never the event's own words), else the item its tag names. A push about
        nothing that still exists (or the earlier one chat) starts a question."""
        thread = event.payload.get("thread_id")
        if isinstance(thread, str) and self.writable(thread):
            return thread
        tag = event.payload.get("tag")
        ident = tag.removeprefix(f"{AGENT_TAG}-") if isinstance(tag, str) else ""
        record = self.store.item(ident) if ITEM_ID_RE.fullmatch(ident) else None
        return thread_of(record) if record is not None else None

    async def ask(self, text: str, *, principal: Principal, channel: str, client_id: str | None,
                  via: str | None = None) -> tuple[dict[str, Any] | None, Submitted]:  # fmt: skip
        """A question the admin asks (Ask in the panel, or a reply to a push about nothing current): it is an item of
        its own (`ask:<token>`), Running until it is answered, and its conversation holds the answer and any
        follow-ups. The same `client_id` returns the same question."""
        if self.items is None:
            return None, Submitted("failed")
        now = datetime.now(UTC)
        title = " ".join(clean_text(text, MESSAGE_CHARS).split())[:QUESTION_TITLE_CHARS]
        if not title:
            return None, Submitted("failed")
        seed = client_id or hashlib.sha256(f"{now.isoformat()}{text}".encode()).hexdigest()
        key = f"{QUESTION_PREFIX}{hashlib.sha256(seed.encode()).hexdigest()[:16]}"
        record = self.store.item_by_key(key)
        if record is None:
            record = self.items.question(key, state="running", gist=f"asked {self._hhmm(now)}", title=title,
                                         actor="you", now=now)  # fmt: skip
            record = record or self.store.item_by_key(key)
        thread = thread_of(record) if record is not None else None
        if record is None or thread is None:
            return None, Submitted("failed")
        sent = await self.submit(thread, text, principal=principal, channel=channel, client_id=client_id,
                                 exclusive=False, via=via)  # fmt: skip
        return self.store.item(str(record["id"])), sent

    def _hhmm(self, when: datetime | str) -> str:
        moment = datetime.fromisoformat(when) if isinstance(when, str) else when
        return moment.astimezone(self._tz()).strftime("%H:%M")

    def sync_question(self, thread: str) -> None:
        """A question's item follows its conversation: Running while the last message waits for its answer, Done
        with the answer (the reply under `agent`, the gist in the daemon's words), Failed when it went unanswered
        (its card says why)."""
        if self.items is None or not thread.startswith("ask:"):
            return
        record = self.record_of(thread)
        if record is None:
            return
        turns = self.store.turns_page(thread, limit=20)
        last = next((t for t in reversed(turns) if t.role == "admin"), None)
        if last is None:
            return
        agent: dict[str, Any] | None = None
        actor = "daemon"
        if last.status in ("queued", "running"):
            state, gist = "running", f"asked {self._hhmm(last.at)}"
        elif last.status == "ok":
            reply = next((t for t in turns if t.reply_to == last.id and t.role == "agent"), None)
            if reply is None:
                return
            sources = [str(x) for x in reply.meta.get("sources") or []]
            state, actor = "done", "agent"
            gist = f"answered {self._hhmm(reply.at)}" + (
                f" · {len(sources)} source{'s' if len(sources) != 1 else ''}" if sources else "")  # fmt: skip
            agent = {"text": reply.text, "sources": sources, "not_read": [], "run_id": reply.run_id}
        else:
            card = self.store.card_for(last.id)
            why = clean_text(card.text, 160) if card is not None else "no answer"
            later = card is not None and bool(card.meta.get("auto_retry")) and last.status == "refused"
            state, gist = "failed", f"not answered {'yet' if later else ''}: {why}".replace(" :", ":")
        try:
            self.items.question(str(record["key"]), state=state, gist=gist, agent=agent, actor=actor)
        except (StoreError, sqlite3.Error, ValueError) as err:
            _LOGGER.warning("question item not written", extra={"error": str(err)[:200]})

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
        item: Mapping[str, Any] | None = None,
    ) -> Submitted:
        """`_submit`, then a question's item follows its conversation (`sync_question`)."""
        sent = await self._submit(thread, text, principal=principal, channel=channel, client_id=client_id,
                                  exclusive=exclusive, via=via, item=item)  # fmt: skip
        self.sync_question(thread)
        return sent

    async def _submit(
        self,
        thread: str,
        text: str,
        *,
        principal: Principal,
        channel: str,
        client_id: str | None = None,
        exclusive: bool = True,
        via: str | None = None,
        item: Mapping[str, Any] | None = None,
    ) -> Submitted:
        """Store the admin's message at once, then queue its answer.

        `exclusive` (the panel): at most one message in flight per thread.
        The same `client_id` returns the stored turn instead of a second one.
        The message is stored even when the daily cap refuses it. `item`: the item whose conversation this is;
        its record is fenced into the run's prompt (`items.item_context`). A question needs none (it is the
        question itself).
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
        if item is not None and not is_question(item):
            meta["item"], meta["item_title"] = str(item["id"]), clean_text(str(item.get("title") or ""), 120)
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
                notice = self.item_message(thread, "Chat limit", "Today's chat limit is reached; try again tomorrow.",
                                           reply=False)  # fmt: skip
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
        self.sync_question(turn.thread_id)
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
            if reason in ("run_cap_reached", "cost_cap"):
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
            channel=channel, summary=summary.text if summary else None, thread=thread,
            context=self._with_item(self._context_text(), turn.meta.get("item")),
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
        self.sync_question(thread)
        footer = sources_line(run_meta)
        delivered = await self._push_unless_watching(
            thread,
            self.item_message(thread, self._title(thread, "Reply"), f"{reply}\n\n{footer}" if footer else reply),
            what="reply",
        )
        self._turn_changed(self.store.update_turn_meta(agent.id, {"push": delivered}))
        self.publish("status", thread, {"turn_id": turn.id, "state": "ok"})
        self.audit("chat.turn", "agent", args={"role": "agent", "thread": thread, "run": outcome.context.run_id,
                    "chars": len(reply)}, outcome=run.status, detail=f"push {delivered}")  # fmt: skip

    def _with_item(self, context: str, item: object) -> str:
        """The run's context plus the item the admin asked about, if it still exists."""
        record = self.store.item(item) if isinstance(item, str) and ITEM_ID_RE.fullmatch(item) else None
        if record is None:
            return context
        block = item_context(record)
        return f"{context}\n\n{block}" if context else block

    # ------------------------------------------------------ item actions

    def plan_fix(self, record: Mapping[str, Any], *, user: str) -> str:
        """ "Plan a fix" on a maintenance item: an investigation on that item, as if the admin had asked for it in the
        item's conversation (the `task.investigate` limits and daily quota, the `investigate` profile and its caps,
        the admin-chat origin). It has no item of its own: the finding shows its phases, its plan and any proposal
        it makes (`Composer._with_plan`). Returns "ok" or why it did not start (daemon words)."""
        now = datetime.now(UTC)
        thread = thread_of(record)
        if thread is None or not str(record.get("key") or "").startswith("maint:"):
            return "Only a maintenance item can plan a fix."
        open_now = self.store.open_investigations()
        if any(t.thread_id == thread for t in open_now):
            return "A plan is already being made for this item; wait for it or stop it."
        if len(open_now) >= MAX_OPEN_INVESTIGATIONS:
            return f"{MAX_OPEN_INVESTIGATIONS} investigations are already open; try again later."
        if self.store.count_open_tasks("admin") >= MAX_OPEN_TASKS["admin"]:
            return "Too many open reminders and follow-ups; cancel some first."
        quota = CAPABILITIES["task.investigate"].quota.per_day
        day = self._day(now)
        if not self.store.bump(day, "cap:task.investigate", limit=quota or None):
            return "Today's investigations are used up; try again tomorrow."
        title = clean_text(f"Plan a fix: {record.get('title') or 'maintenance item'}", MAX_TITLE_CHARS)
        task, created = self.store.create_task(
            kind="followup", title=title, owner="admin", created_by=f"panel:{user[:64]}", next_fire_at=now,
            payload={"investigation": True, "plan_fix": True, "note": PLAN_FIX_BRIEF, "max_steps": PLAN_FIX_STEPS,
                     "step": 0, "findings": [], "origin": str(Origin.ADMIN_CHAT), "user_id": user,
                     "item": str(record["id"]), "started_at": now.isoformat(timespec="seconds")},
            idempotency_key=f"plan_fix:{record['id']}:{record.get('revision')}:{now.date().isoformat()}", now=now,
            thread_id=thread,
        )  # fmt: skip
        if not created:
            # The same request again (same item, revision and day): the quota unit goes back.
            self.store.unbump(day, "cap:task.investigate")
            return "This fix was already planned today; the plan is on the item."
        self.audit("task.investigate", f"admin-chat:{user[:64]}", args={"task": task.id, "item": record["id"]},
                   outcome="accepted", detail="plan a fix (panel)")  # fmt: skip
        self._investigation_card(task, now)
        self.note("investigation", "Plan a fix: started from this item", ref={"task": task.id})
        self.publish("tasks", None, {})
        self.wake()
        return "ok"

    def remind_about(self, record: Mapping[str, Any], *, at: datetime, user: str) -> str:
        """A reminder about an item, set by the admin in the panel (one per item and due day). "ok" or why not."""
        now = datetime.now(UTC)
        if at < now + timedelta(minutes=1) or at > now + timedelta(days=366):
            return "That reminder time is not between a minute and a year from now."
        if self.store.count_open_tasks("admin") >= MAX_OPEN_TASKS["admin"]:
            return "Too many open reminders and follow-ups; cancel some first."
        title = clean_text(str(record.get("title") or "an item"), MAX_TITLE_CHARS)
        self.store.create_task(
            kind="reminder", title=title, owner="admin", created_by=f"panel:{user[:64]}", next_fire_at=at,
            payload={"note": f"Reminder about the item {record['id']} on Home: open it to decide.", "reply": False,
                     "origin": str(Origin.ADMIN), "user_id": user, "item": str(record["id"])},
            idempotency_key=f"remind:{record['id']}:{at.date().isoformat()}", now=now,
        )  # fmt: skip
        self.audit("task.create", f"admin:{user[:64]}", args={"item": record["id"], "at": at.isoformat()},
                   outcome="accepted", detail="remind me (panel)")  # fmt: skip
        self.publish("tasks", None, {})
        self.wake()
        return "ok"

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
            meta["forks"] = forkrebase.for_run(self.store, run_id)
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.warning("no sources for run", extra={"error": str(err)[:200]})
            return {}
        return {k: v for k, v in meta.items() if v}

    def _after_chat_run(self, run_id: str, thread: str, run_meta: dict[str, Any]) -> None:
        """What the run started or proposed has an item of its own: the conversation gets a link card to each, and
        an investigation links back to where it was asked (`payload.from_item`)."""
        now = datetime.now(UTC)
        started = False
        origin = self.record_of(thread)
        for task in self.store.tasks_created_by(f"{CHAT.name}:{run_id}"):
            if task.payload.get("investigation") and task.thread_id == thread and task.state == "pending":
                if origin is not None and not task.payload.get("from_item"):
                    self.store.set_task_payload(task.id, task.payload | {"from_item": str(origin["key"])}, now=now)
                self._investigation_card(task, now)
                self._link_card(thread, f"task:{task.id}", "Investigation", now)
                self.note("investigation", f"Investigation started: {task.title}", ref={"task": task.id})
                started = True
        if started:
            self.publish("tasks", None, {})
            self.wake()
        self._link_cards(thread, run_id, run_meta, now)

    def _link_card(self, thread: str, key: str, kicker: str, now: datetime) -> None:
        """A card in the conversation that opens the item `key` (its title and state are read when shown)."""
        record = self.store.item_by_key(key)
        if record is None or any(t.status == "link" and t.meta.get("item") == record["id"]
                                 for t in self.store.turns_page(thread, limit=30)):  # fmt: skip
            return
        text = f"{kicker}: {clean_text(str(record['title']), 120)}"
        self._turn_changed(self.store.append_turn(thread, role="system", status="link", text=text, now=now,
                                                  meta={"item": str(record["id"])}))  # fmt: skip

    def _link_cards(self, thread: str, run_id: str, run_meta: dict[str, Any], now: datetime) -> None:
        """After a run: a link card per approval request, config change and fork rebase it made (each is an item,
        decided there), and the Back up now card for a backup request."""
        for found in run_meta.get("proposals") or []:
            if found.get("kind") == "backup":
                self._turn_changed(self.store.append_turn(
                    thread, role="system", status="proposal", now=now,
                    text="Back up now? A complete, encrypted backup with your automatic backup settings. Nothing"
                         " runs until you tap Back up now.",
                    meta={"actions": ["backup"]}))  # fmt: skip
                continue
            self._link_card(thread, update_key(str(found["entity_id"]), str(found["to"])), "Approval requested", now)
        for row in self.store.changes_of_runs([run_id]):
            self._link_card(thread, f"change:{row['id']}", "Proposed change", now)
        for fork in run_meta.get("forks") or []:
            self._link_card(thread, f"fork:{fork['id']}", "Rebase onto Core", now)
        if run_meta.get("proposals") or run_meta.get("forks"):
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
            await self._push_unless_watching(turn.thread_id, self.item_message(
                turn.thread_id, "Message not answered", f"I could not answer your message: {card.text}",
                reply=False), what="gave_up")  # fmt: skip

    async def _failed(self, turn: Turn, status: str) -> None:
        now = datetime.now(UTC)
        card = error_card(status, now=now)
        if self._write_card(turn, "failed", card.status, card.text, card.meta, now, expect=("running",)) is None:
            return
        self.audit("chat.turn", "agent", args={"role": "agent", "thread": turn.thread_id, "turn": turn.id},
                    outcome=status, detail="failed")  # fmt: skip
        await self._push_unless_watching(turn.thread_id, self.item_message(
            turn.thread_id, "Reply failed", card.text, reply=False), what="failed")  # fmt: skip

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
            "done": "Plan ready: it is above, under The agent says."
            if task.payload.get("plan_fix")
            else f"Finished after {step} step{'s' if step != 1 else ''}.",
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
        self,
        task: Task,
        card: Turn,
        state: str,
        step: int,
        of: int,
        *,
        detail: str = "",
        runs: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        # The runs (a plan's proposals are found by them), the plan and why it stopped stay on the task.
        kept = dict(payload if payload is not None else task.payload)
        if runs is not None:
            kept["runs"] = runs
        kept.pop("waiting", None)
        if state == "failed":
            kept["stopped"] = clean_text(detail, 300) or "it could not finish"
        self.store.finish_task(task.id, state, now=now, payload=kept)
        if state == "cancelled":
            self._void_requests(runs if runs is not None else _run_ids(kept), now)
        self._set_card(card, task, state, step, of, detail=detail)
        self.audit("task.investigate", "system", args={"task": task.id, "step": step}, outcome=state,
                   detail=detail[:200])  # fmt: skip
        self.note("investigation", f"Investigation {state}: {task.title}", detail or f"{step} of {of} steps",
                  needs_admin=state == "failed", ref={"task": task.id})  # fmt: skip
        self.publish("tasks", None, {})
        if state == "failed" and task.thread_id:
            stopped = "The plan stopped" if task.payload.get("plan_fix") else f"Investigation stopped: {task.title}"
            await self._push_unless_watching(
                task.thread_id,
                self.item_message(task.thread_id, stopped, detail or "It could not finish.", reply=False),
                what="investigation",
            )

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
                                            context=self._with_item(self._context_text(),
                                                                    task.payload.get("item")))  # fmt: skip
        self._investigating[thread] = card.id
        try:
            # Checked again once the AI slot is held: a cancel while waiting never starts the step.
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
            await self._end_investigation(task, card, "cancelled", done, of, payload=payload)
            return
        if run.status in BLOCKED_STATUSES or run.status == "skipped_busy":
            await self._investigation_blocked(task, card, payload, run.status, run.error, done, of, now)
            return
        reply, summary, following = validate_investigation(run.structured) if run.status == "ok" else (None, "", None)
        if reply is None:
            why = "the step gave no findings" if run.status == "ok" else f"the step ended with {run.status}"
            await self._end_investigation(task, card, "failed", done, of, detail=why, payload=payload)
            return
        reply = redactor.redact(reply)
        run_meta = self._run_meta(outcome.context.run_id)
        final = not (following and step < of)
        if not (task.payload.get("plan_fix") and final):
            # A plan's last findings are the plan itself: shown once, as the finding's "The agent says"; its card in
            # the conversation says so in one line.
            findings = self.store.append_turn(thread, role="agent", text=reply, now=now, run_id=outcome.context.run_id,
                                              meta={"investigation": task.id, "step": step, "of": of,
                                                    "duration_s": round(run.duration_s, 1), **run_meta})  # fmt: skip
            self._turn_changed(findings)
        if not task.payload.get("plan_fix"):
            # A plan's proposals are decided on its finding; anything else the step made gets a link card.
            self._link_cards(thread, outcome.context.run_id, run_meta, now)
        earlier = [str(f) for f in payload.get("findings") or [] if isinstance(f, str)]
        noted = clean_text(redactor.redact(summary or reply), FINDINGS_CHARS)
        payload |= {"step": step, "waits": 0, "findings": [*earlier, noted][-MAX_FINDINGS:]}
        payload.pop("waiting", None)
        self.note("investigation", f"Investigation step {step} of {of}: {task.title}", summary, ref={"task": task.id})
        self.audit("task.investigate", _principal(task).label(), args={"task": task.id, "step": step,
                   "run": outcome.context.run_id}, outcome="step", detail=summary)  # fmt: skip
        if following and step < of:
            payload["next"] = redactor.redact(following)
            self.store.reschedule(task.id, next_fire_at=now + STEP_GAP, now=now, payload=payload)
            self._set_card(card, task, "next", step, of)
            self.wake()
            return
        if task.payload.get("plan_fix"):
            # The last step's findings are the plan: the finding shows them under The agent says.
            payload |= {"plan": clean_text(reply, PLAN_CHARS), "sources": list(run_meta.get("sources") or [])}
        await self._end_investigation(task, card, "done", step, of, payload=payload)
        footer = sources_line(run_meta)
        record = self.record_of(thread)
        if task.payload.get("plan_fix") and record is not None and record.get("needs"):
            # The finding now asks (a proposal waits for approval): the item book's push says so; one push per item.
            return
        title = f"Plan ready: {self._title(thread, '')}" if task.payload.get("plan_fix") else (
            f"Investigation: {task.title}")  # fmt: skip
        await self._push_unless_watching(
            thread, self.item_message(thread, title, f"{reply}\n\n{footer}" if footer else reply), what="investigation"
        )

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
            await self._end_investigation(task, card, "failed", done, of, detail=text, payload=payload)
            return
        payload["waits"] = waits + 1
        local = at.astimezone(self._tz()).strftime("%a %H:%M")
        # Daemon words for the finding's state line while a plan waits.
        payload["waiting"] = f"{text} Next try {local}."
        self.store.reschedule(task.id, next_fire_at=at, now=now, payload=payload)
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
        if turn is None or turn.thread_id != thread or turn.role != "admin" or not self.writable(thread):
            return None
        return turn

    def retry_turn(self, thread: str, turn_id: int) -> str:
        """The admin's Retry: `queued`, `not_found`, `not_retryable`, `cap` or `busy`. Runs at most once."""
        now = datetime.now(UTC)
        turn = self._admin_turn(thread, turn_id)
        if turn is None or not self.writable(thread):
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
            self.sync_question(turn.thread_id)
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
        # The earlier one chat is read only: a retry left scheduled there never runs.
        if (
            turn is None
            or turn.status != "refused"
            or turn.meta.get("auto_retry_off")
            or not self.writable(turn.thread_id)
        ):
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
        self.sync_question(turn.thread_id)
        if not self._enqueue_turn(queued):
            # Put it back and try again later; the attempt is not consumed.
            self.store.set_turn_status(turn.id, "refused", expect=("queued",), meta={"retries": retries - 1})
            self.store.schedule_retry(turn.id, at=now + RETRY_QUEUE_FULL, attempt=retries, thread_id=turn.thread_id,
                                      now=now)  # fmt: skip

    def keep_earlier(self) -> None:
        """At start: the one chat of earlier versions becomes one read-only History item (written once)."""
        try:
            if self.items is None or not self.store.chat_id():
                return
            turns, first, last = self.store.legacy_turns()
            self.items.earlier(turns=turns, first=first, last=last)
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.warning("earlier conversation not kept as an item", extra={"error": str(err)[:200]})

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
