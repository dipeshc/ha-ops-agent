"""Channels: how the agent reaches the admin, independent of who is speaking.

A `Message` is channel-neutral (title, body, urgency, thread tag, actions,
budget class). `CompanionPush` is the only channel today: it maps a message
onto the existing rate-limited `Notifier` (Core `notify.mobile_app_*`, with
the push-relay fallback while Core is down) and records every action id it
puts on a push, so an inbound tap can later be matched to a notification the
agent really sent to the admin's device. Every message goes to the single
configured admin device; there is no recipient parameter.
"""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from opsd.notify import Notifier, Push
from opsd.store import Store

ACTION_TTL = timedelta(days=3)
ACTION_PREFIX = "OPS"


class BudgetClass(StrEnum):
    """Daily budgets are per class so one kind of traffic cannot starve another.

    SAFETY: deterministic daemon pushes (critical items, Core down, the kill switch), never model text.
    USER: answers to the admin's own messages and reminders the admin set. AGENT: everything else.
    """

    SAFETY = "safety"
    USER = "user"
    AGENT = "agent"


class ActionKind(StrEnum):
    REPLY = "REPLY"
    SNOOZE = "SNOOZE"
    DONE = "DONE"


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    title: str
    authentication_required: bool = False
    text_input: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    title: str
    body: str
    urgent: bool = False
    tag: str = "ha-ops"
    thread_id: str | None = None
    actions: tuple[Action, ...] = ()
    budget_class: BudgetClass = BudgetClass.AGENT
    # Panel deep link opened by tapping the push (a chat thread); None opens the panel.
    url: str | None = None
    # How long the action ids on this push stay valid (default ACTION_TTL).
    action_ttl: timedelta | None = None
    # Delivered during quiet hours anyway (a reminder the admin set for that time).
    quiet_ok: bool = False


AGENT_TITLE_PREFIX = "Agent: "
PUSH_BODY_CHARS = 180
AGENT_TAG = "ha-ops-agent"


def agent_message(
    title: str,
    body: str,
    *,
    urgent: bool,
    budget_class: BudgetClass = BudgetClass.AGENT,
    tag_suffix: str | None = None,
    thread_id: str | None = None,
    actions: tuple[Action, ...] = (),
    url: str | None = None,
) -> Message:
    """A push written by (or on behalf of) the agent: labelled and tagged apart from the daemon's own.

    The prefix and tag mean model text can never pass for (or replace, via
    the notification tag) a deterministic safety or Core-down push.
    """
    if budget_class is BudgetClass.SAFETY:
        budget_class = BudgetClass.AGENT
    label = title.strip().removeprefix(AGENT_TITLE_PREFIX.strip()).lstrip(": ").strip() or "Ops Agent"
    # A push is a pointer: the full text is in the panel (about 180 characters).
    if len(body) > PUSH_BODY_CHARS:
        body = body[: PUSH_BODY_CHARS - 1].rstrip() + "…"
    suffix = re.sub(r"[^a-z0-9-]", "", tag_suffix or "")[:40]
    return Message(
        f"{AGENT_TITLE_PREFIX}{label}"[:70],
        body,
        urgent=urgent,
        tag=f"{AGENT_TAG}-{suffix}" if suffix else AGENT_TAG,
        thread_id=thread_id,
        actions=actions,
        budget_class=budget_class,
        url=url,
    )


REPLY_ACTION = Action(ActionKind.REPLY, "Reply", text_input=True)


def task_tag(task_id: int) -> str:
    return f"task-{task_id}"


def task_from_tag(tag: object) -> int | None:
    """The task id from an agent reminder push's tag (`ha-ops-agent-task-<id>`)."""
    match = re.fullmatch(rf"{AGENT_TAG}-task-(\d{{1,9}})", tag) if isinstance(tag, str) else None
    return int(match.group(1)) if match else None


def new_action_id(kind: ActionKind) -> str:
    """128 random bits; the id is the only thing a forged tap could not know."""
    return f"{ACTION_PREFIX}_{kind}_{secrets.token_hex(16)}"


def push_kind(message: Message) -> str:
    """The notifier's daily bucket. An urgent agent push is time-sensitive but never uses the safety reserve."""
    match message.budget_class:
        case BudgetClass.SAFETY:
            return "safety"
        case BudgetClass.USER:
            return "user"
        case _:
            return "normal"


def companion_action(action_id: str, action: Action) -> dict[str, Any]:
    data: dict[str, Any] = {"action": action_id, "title": action.title[:40]}
    if action.authentication_required:
        data["authenticationRequired"] = True
    if action.text_input:
        data["behavior"] = "textInput"
    return data


class CompanionPush:
    name = "companion"

    def __init__(
        self,
        notifier: Notifier,
        store: Store | None,
        *,
        new_id: Callable[[ActionKind], str] = new_action_id,
    ) -> None:
        self._notifier = notifier
        self._store = store
        self._new_id = new_id

    def send(self, message: Message, *, core_up: bool, now: datetime) -> str:
        actions: list[dict[str, Any]] = []
        if message.actions:
            if self._store is None:
                # Without the store a tap could never be verified: send no buttons.
                return self._notifier.send(self._push(message, ()), core_up=core_up, now=now)
            for action in message.actions:
                action_id = self._new_id(action.kind)
                self._store.issue_action(
                    action_id,
                    kind=str(action.kind),
                    tag=message.tag,
                    thread_id=message.thread_id,
                    now=now,
                    expires_at=now + (message.action_ttl or ACTION_TTL),
                )
                actions.append(companion_action(action_id, action))
        return self._notifier.send(self._push(message, tuple(actions)), core_up=core_up, now=now)

    @staticmethod
    def _push(message: Message, actions: tuple[dict[str, Any], ...]) -> Push:
        return Push(
            message.title,
            message.body,
            urgent=message.urgent,
            tag=message.tag,
            kind=push_kind(message),
            actions=actions,
            url=message.url,
        )
