"""Reminder and follow-up capabilities (`task.create`, `task.list`, `task.cancel`).

Tasks are rows in the store, so they survive restarts, and the scheduler
fires them. Limits, all enforced here rather than trusted from Claude:
- a due time between one minute and one year ahead;
- at most `MAX_OPEN_TASKS` open tasks in total (one admin, one agent);
- a follow-up created by a follow-up run is one generation deeper, and the
  chain stops at `MAX_GENERATION`, so a run cannot schedule itself forever;
- creation is idempotent per run (the same request twice in one run creates
  one task), and every task carries an idempotency key.
A task records the origin of the run that made it; a follow-up runs with
that origin, so a chat-created follow-up stays limited to chat powers.

`task.investigate` is a follow-up marked as an investigation: it
belongs to the chat thread that started it, is due at once, and runs up to
three steps (`investigate` profile runs) that report into that thread. Each
step is a normal run under every usage cap; the admin can cancel it from
the thread. A chat may have one open investigation, the house two.
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta, tzinfo

from opsd.capabilities import Args, CapabilityError
from opsd.policy import Origin
from opsd.runctx import ExecContext, RunContext
from opsd.store import Store, Task

MIN_LEAD = timedelta(minutes=1)
MAX_HORIZON = timedelta(days=366)
# Per owner, so the agent cannot crowd out the admin's own reminders.
MAX_OPEN_TASKS = {"admin": 50, "agent": 10}
# Reminders the agent schedules for itself stay short-lived.
AGENT_HORIZON = timedelta(days=14)
MAX_GENERATION = 3
MAX_TITLE_CHARS = 120
MAX_NOTE_CHARS = 1000
MAX_OPEN_INVESTIGATIONS = 2
MAX_BRIEF_CHARS = 1000
_RELATIVE = re.compile(r"\+(\d{1,4})([mhd])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_at(value: str, *, now: datetime, tz: tzinfo) -> datetime:
    """`+30m` / `+2h` / `+3d`, or local `YYYY-MM-DDTHH:MM`; returned in UTC.

    Minutes and hours are durations. Days are calendar days in `tz`: "+1d" at 09:00 is 09:00 local
    the next day, also across a daylight saving change (then 23 or 25 hours later).
    """
    relative = _RELATIVE.fullmatch(value)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        if _UNITS[unit] == "days":
            local = now.astimezone(tz)
            day = local.date() + timedelta(days=amount)
            return datetime.combine(day, local.time(), tzinfo=tz).astimezone(UTC)
        return now + timedelta(**{_UNITS[unit]: amount})
    try:
        local = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as err:
        raise CapabilityError("at must be local YYYY-MM-DDTHH:MM or +Nm/+Nh/+Nd") from err
    return local.replace(tzinfo=tz).astimezone(UTC)


def _run(ctx: ExecContext) -> RunContext:
    if ctx.run is None:
        raise CapabilityError(f"{ctx.capability.name} is only available inside an agent run")
    return ctx.run


def _store(ctx: ExecContext) -> Store:
    if ctx.env.store is None:
        raise CapabilityError("the task store is not available")
    return ctx.env.store


def owner_of(run: RunContext) -> str:
    """Admin-origin runs act for the admin; everything else is the agent's own."""
    return "admin" if run.principal.origin in (Origin.ADMIN, Origin.ADMIN_CHAT) else "agent"


def describe(task: Task, tz: tzinfo) -> str:
    when = (
        datetime.fromisoformat(task.next_fire_at).astimezone(tz).strftime("%a %d %b %H:%M")
        if task.next_fire_at
        else "-"
    )
    return f"#{task.id} {task.kind} '{task.title}' fires {when} ({task.state})"


def validate_create(ctx: ExecContext, args: Args) -> Args:
    run = _run(ctx)
    store = _store(ctx)
    title = (args.opt_text("title") or "").strip()
    if not title:
        raise CapabilityError("title is required")
    at = args.opt_text("at")
    if not at:
        raise CapabilityError("at is required")
    now = datetime.now(UTC)
    due = parse_at(at, now=now, tz=ctx.env.tz())
    owner = owner_of(run)
    horizon = MAX_HORIZON if owner == "admin" else AGENT_HORIZON
    if due < now + MIN_LEAD or due > now + horizon:
        limit = "one year" if owner == "admin" else "14 days"
        raise CapabilityError(f"the time must be between one minute and {limit} from now")
    kind = args.text("kind")
    generation = run.generation + 1 if run.profile == "followup" else 0
    if kind == "followup" and generation > MAX_GENERATION:
        raise CapabilityError(f"follow-up chain limit reached ({MAX_GENERATION}); tell the admin instead")
    if store.count_open_tasks(owner) >= MAX_OPEN_TASKS[owner]:
        raise CapabilityError(f"too many open {owner} tasks ({MAX_OPEN_TASKS[owner]}); cancel some first")
    return args.replace(
        title=title[:MAX_TITLE_CHARS],
        note=(args.opt_text("note") or "").strip()[:MAX_NOTE_CHARS],
        at=due.isoformat(timespec="seconds"),
        generation=generation,
        # Agent-scheduled reminders never invite a reply.
        reply=args.flag("reply") and owner == "admin",
    )


async def exec_create(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    store = _store(ctx)
    kind, title, due = args.text("kind"), args.text("title"), args.text("at")
    digest = hashlib.sha256(f"{kind}|{title}|{due}".encode()).hexdigest()[:16]
    owner = owner_of(run)
    task, created = store.create_task(
        kind=kind,
        title=title,
        owner=owner,
        created_by=f"{run.profile}:{run.run_id}",
        next_fire_at=datetime.fromisoformat(due),
        payload={
            "note": args.text("note"),
            "reply": args.flag("reply"),
            "origin": str(run.principal.origin),
            "user_id": run.principal.user_id,
        },
        idempotency_key=f"{run.run_id}:{digest}",
        now=datetime.now(UTC),
        generation=args.number("generation"),
        thread_id=run.thread_id,
    )
    prefix = "ok: created" if created else "ok: already created in this run"
    return f"{prefix} {describe(task, ctx.env.tz())}"


def exec_list(ctx: ExecContext, _: Args) -> str:
    store = _store(ctx)
    tasks = store.open_tasks()
    if not tasks:
        return "no open reminders or follow-ups"
    return "\n".join(describe(t, ctx.env.tz()) for t in tasks)


async def exec_cancel(ctx: ExecContext, args: Args) -> str:
    store = _store(ctx)
    owner = owner_of(_run(ctx))
    task_id = args.number("id")
    if not store.cancel_task(task_id, owner=owner, now=datetime.now(UTC)):
        raise CapabilityError(
            f"no pending {owner} reminder or follow-up #{task_id} (only your own tasks; never system tasks)"
        )
    return f"ok: cancelled #{task_id}"


def validate_investigate(ctx: ExecContext, args: Args) -> Args:
    run = _run(ctx)
    store = _store(ctx)
    if run.profile != "chat" or run.thread_id is None:
        raise CapabilityError("investigations start from a chat with the admin (not from a follow-up)")
    title = (args.opt_text("title") or "").strip()
    brief = (args.opt_text("brief") or "").strip()
    if not title or not brief:
        raise CapabilityError("title and brief are required")
    open_now = store.open_investigations()
    if any(t.thread_id == run.thread_id for t in open_now):
        raise CapabilityError("this chat already has an investigation open; wait for it or ask the admin to cancel it")
    if len(open_now) >= MAX_OPEN_INVESTIGATIONS:
        raise CapabilityError(f"{MAX_OPEN_INVESTIGATIONS} investigations are already open; try again later")
    if store.count_open_tasks(owner_of(run)) >= MAX_OPEN_TASKS[owner_of(run)]:
        raise CapabilityError("too many open tasks; cancel some first")
    return args.replace(title=title[:MAX_TITLE_CHARS], brief=brief[:MAX_BRIEF_CHARS])


async def exec_investigate(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    store = _store(ctx)
    now = datetime.now(UTC)
    steps = args.number("steps")
    task, created = store.create_task(
        kind="followup",
        title=args.text("title"),
        owner=owner_of(run),
        created_by=f"{run.profile}:{run.run_id}",
        next_fire_at=now,
        payload={
            "investigation": True,
            "note": args.text("brief"),
            "max_steps": steps,
            "step": 0,
            "findings": [],
            "origin": str(run.principal.origin),
            "user_id": run.principal.user_id,
        },
        idempotency_key=f"{run.run_id}:investigate",
        now=now,
        generation=run.generation,
        thread_id=run.thread_id,
    )
    if not created:
        return f"ok: investigation #{task.id} was already started in this run"
    return (
        f"ok: investigation #{task.id} queued: up to {steps} background run(s) after this reply; progress and"
        " findings appear in this chat. Tell the admin it has started; do not wait for it."
    )
