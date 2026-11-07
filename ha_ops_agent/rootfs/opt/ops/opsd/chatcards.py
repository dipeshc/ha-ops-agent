"""Inline chat cards: why a message got no answer, and when it is retried (spec chat.md T5).

A refused or failed turn gets one `system` card with fixed text per reason, filled only with daemon-computed times
and numbers. Blocks that lift by themselves are retried automatically on a capped backoff (`plan_retry`), unless the
admin turns that off on the card.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from opsd.budget import DEFAULT_PROFILE_RUNS, OWNER_RESERVED_RUNS, PROFILE_WORDS, RUNS_PER_DAY

AUTO_RETRY = frozenset({"usage_limited", "share_cap", "run_cap_reached", "account_busy", "skipped_busy",
                        "share_reserve", "cost_cap"})  # fmt: skip
RETRY_BACKOFF: tuple[timedelta, ...] = tuple(timedelta(minutes=15 * 2**n) for n in range(6))
RETRY_HORIZON = timedelta(hours=24)
RETRY_MARGIN = timedelta(seconds=30)
_DETAIL_RE = re.compile(r"[a-z0-9_]{1,40}")

_TEXT: dict[str, str] = {
    "ai_disabled": "AI is off, so nothing runs until it is turned back on. Your message is saved.",
    "share_cap": "The agent has used its {window} run-time budget.",
    "share_reserve": "Saving budget for monitoring: chat has used half of the agent's {window} run-time budget, and"
    " the rest is kept for health checks and update reviews.",
    "cost_cap": "Today's AI cost cap is reached.",
    "run_cap_reached": "Today's limit for {runs} is reached ({cap} runs a day{kept}).",
    "usage_limited": "The AI provider reported a usage limit, so the agent is backing off.",
    "account_busy": "Your AI subscription is near its limit from other use, so the agent yields.",
    "skipped_busy": "The agent was busy with another run for too long.",
    "busy": "Not sent: the agent's queue is full.",
    "interrupted": "The agent restarted before answering this. Your message is saved.",
}
_WINDOWS = {"five_hour": "5-hour", "seven_day": "7-day", "run_time": "5-hour"}


@dataclass(frozen=True, slots=True)
class Card:
    status: str
    text: str
    meta: dict[str, Any]


def split_reason(status: str, error: str) -> tuple[str, str]:
    """`(reason, detail)` from a run status and the runner's `reason:detail` error (daemon text)."""
    reason = status.split(":", 1)[0]
    _, _, detail = error.partition(":")
    detail = detail.strip().lower()
    return reason, detail if _DETAIL_RE.fullmatch(detail) else ""


def _when(at: datetime, now: datetime, tz: tzinfo) -> str:
    local = at.astimezone(tz)
    return local.strftime("%H:%M") if local.date() == now.astimezone(tz).date() else local.strftime("%a %H:%M")


def check_wait(reason: str, detail: str, *, retry_at: datetime, now: datetime, tz: tzinfo) -> str:
    """The maintenance scan's own check could not run: why, in daemon words, and that it retries by itself. Never
    chat wording (the admin asked for nothing and has nothing to retry)."""
    nxt = f"(next try {_when(retry_at, now, tz)})"
    if reason == "ai_disabled":
        return f"AI is off, so the agent's check waits {nxt}"
    if reason in ("run_cap_reached", "share_cap", "share_reserve", "cost_cap", "account_busy"):
        return f"the agent's check waits for its daily allowance {nxt}"
    if reason == "usage_limited":
        return f"the agent's check waits for the AI provider's usage limit to lift {nxt}"
    if reason == "no_answer":
        return f"the agent's check reached no conclusion and runs again by itself {nxt}"
    return f"the agent's check failed and will run again by itself {nxt}"


def plan_retry(
    reason: str, *, attempt: int, first_at: datetime, now: datetime, hint: datetime | None
) -> datetime | None:
    """When to retry automatically, or None (not a retryable reason, too many attempts, too far off)."""
    if reason not in AUTO_RETRY or attempt >= len(RETRY_BACKOFF):
        return None
    at = now + RETRY_BACKOFF[attempt]
    if hint is not None:
        at = max(at, hint + RETRY_MARGIN)
    if at - first_at > RETRY_HORIZON:
        return None
    return at


def refusal_card(
    reason: str,
    detail: str,
    *,
    now: datetime,
    tz: tzinfo,
    reset_at: datetime | None = None,
    retry_at: datetime | None = None,
    attempt: int = 0,
    auto_off: bool = False,
) -> Card:
    template = _TEXT.get(reason)
    if template is None:
        return error_card(reason, now=now)
    kept = OWNER_RESERVED_RUNS.get(detail, 0)
    text = template.format(window=_WINDOWS.get(detail, "subscription"), detail=detail.replace("_", " ") or reason,
                           runs=PROFILE_WORDS.get(detail, "this kind of run"),
                           cap=RUNS_PER_DAY.get(detail, DEFAULT_PROFILE_RUNS),
                           kept=f", the last {kept} kept for your Plan a fix taps" if kept else "")  # fmt: skip
    actions: list[str] = []
    if reset_at is not None and reason != "ai_disabled":
        text += f" It resets about {_when(reset_at, now, tz)}."
    gave_up = False
    if retry_at is not None:
        text += f" Retrying automatically at {_when(retry_at, now, tz)}."
        actions = ["retry", "cancel_auto"]
    elif reason in AUTO_RETRY:
        gave_up = not auto_off
        text += " It will not be retried automatically." if gave_up else " Automatic retry is off."
        actions = ["retry", "enable_auto"] if auto_off else ["retry"]
    elif reason == "ai_disabled":
        actions = ["turn_on_retry"]
    elif reason in ("busy", "interrupted"):
        actions = ["retry"]
    meta: dict[str, Any] = {
        "reason": reason,
        "detail": detail,
        "reset_at": reset_at.isoformat(timespec="seconds") if reset_at else None,
        "retry_at": retry_at.isoformat(timespec="seconds") if retry_at else None,
        "auto_retry": retry_at is not None,
        "attempt": attempt,
        "gave_up": gave_up,
        "actions": actions,
        "resolved": False,
    }
    return Card(reason, text, meta)


def error_card(status: str, *, now: datetime) -> Card:
    """A run that started (or tried to) and failed: the admin may retry by hand."""
    safe = status if _DETAIL_RE.fullmatch(status) else "error"
    label = safe.replace("_", " ")
    return Card(
        "error",
        f"Sorry, that run failed ({label}). Your message is saved; retry when you like.",
        {"reason": safe, "detail": "", "reset_at": None, "retry_at": None, "auto_retry": False, "attempt": 0,
         "gave_up": False, "actions": ["retry"], "resolved": False, "at": now.isoformat(timespec="seconds")},
    )  # fmt: skip
