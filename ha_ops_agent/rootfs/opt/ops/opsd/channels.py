"""Messages: what the agent says to the admin, channel-neutral (spec items-and-pushes.md N2, N4, N7, N10).

A `Message` carries title, body, urgency, tag, thread, actions and budget class; `opsd.notify.Notifier` is the
one channel that sends it. Agent-written messages are labelled and tagged apart from the daemon's own.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


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
    # Panel deep link opened by tapping the push (an item); None opens the panel.
    url: str | None = None
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
