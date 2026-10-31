"""Quiet hours: non-urgent pushes wait until they end.

The window is local wall-clock time (`quiet_start`, `quiet_end`, HH:MM in
the house's time zone) and may wrap midnight; an empty or equal pair turns
it off. Wall-clock comparison means a DST change moves nothing: 07:00 stays
07:00 local.

Never held: urgent pushes, the safety class (Core down, critical health
items, update escalations) and pushes marked `quiet_ok` (a reminder the
admin scheduled themselves, which fires at the time they chose). Everything
else (replies, follow-ups, agent reminders, Needs-you items) is stored and
delivered at the end of the window, one push each (no digest), within the
daily caps. Agent runs are never delayed; only delivery is.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from opsd.channels import Action, ActionKind, BudgetClass, Message

_HM_RE = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


@dataclass(frozen=True, slots=True)
class Window:
    start: time
    end: time


MAX_HOURS = 12


def problem(start: str, end: str) -> str | None:
    """Why a configured window is ignored, or None (off by choice or valid)."""
    if not start.strip() or not end.strip():
        return None
    first, last = _HM_RE.fullmatch(start.strip()), _HM_RE.fullmatch(end.strip())
    if first is None or last is None:
        return "quiet_start and quiet_end must be HH:MM"
    minutes = (int(last.group(1)) * 60 + int(last.group(2)) - int(first.group(1)) * 60 - int(first.group(2))) % 1440
    if minutes > MAX_HOURS * 60:
        return f"quiet hours longer than {MAX_HOURS} h ({start}-{end}) are not allowed"
    return None


def window(start: str, end: str) -> Window | None:
    """The configured window, or None when it is off (empty, invalid, zero-length or longer than 12 h)."""
    if problem(start, end) is not None:
        return None
    first, last = _HM_RE.fullmatch(start.strip()), _HM_RE.fullmatch(end.strip())
    if first is None or last is None:
        return None
    begin = time(int(first.group(1)), int(first.group(2)))
    finish = time(int(last.group(1)), int(last.group(2)))
    return None if begin == finish else Window(begin, finish)


def is_quiet(win: Window | None, now: datetime, tz: tzinfo) -> bool:
    if win is None:
        return False
    local = now.astimezone(tz).time().replace(second=0, microsecond=0, tzinfo=None)
    if win.start < win.end:
        return win.start <= local < win.end
    return local >= win.start or local < win.end


def ends_at(win: Window, now: datetime, tz: tzinfo) -> datetime:
    """The next local `end` after `now`, in UTC (wall-clock arithmetic, so DST days keep 07:00)."""
    local = now.astimezone(tz)
    candidate = datetime.combine(local.date(), win.end, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), win.end, tzinfo=tz)
    return candidate.astimezone(UTC)


def bypass(message: Message) -> bool:
    return message.urgent or message.quiet_ok or message.budget_class is BudgetClass.SAFETY


def to_json(message: Message) -> str:
    data = asdict(message)
    data["actions"] = [asdict(a) | {"kind": str(a.kind)} for a in message.actions]
    data["budget_class"] = str(message.budget_class)
    data["action_ttl"] = message.action_ttl.total_seconds() if message.action_ttl is not None else None
    return json.dumps(data, default=str)


def from_json(raw: str) -> Message:
    data: dict[str, Any] = json.loads(raw)
    ttl = data.get("action_ttl")
    return Message(
        title=str(data["title"]),
        body=str(data["body"]),
        urgent=bool(data.get("urgent")),
        tag=str(data.get("tag") or "ha-ops"),
        thread_id=data.get("thread_id"),
        actions=tuple(
            Action(
                ActionKind(a["kind"]),
                str(a["title"]),
                bool(a.get("authentication_required")),
                bool(a.get("text_input")),
            )
            for a in data.get("actions") or []
        ),
        budget_class=BudgetClass(data.get("budget_class") or BudgetClass.AGENT),
        url=data.get("url"),
        action_ttl=timedelta(seconds=float(ttl)) if ttl is not None else None,
        quiet_ok=bool(data.get("quiet_ok")),
    )
