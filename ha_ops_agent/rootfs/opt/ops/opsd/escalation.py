"""The one escalation gate: what may enter Needs you, and why (spec items-and-pushes.md I22).

A need is made only by `need`, with one reason of the closed list `Why`; `gate` is the only function that decides
whether a record may be in Needs you. The item book applies it to every item it composes and the store applies it
again to every record it writes, so a record without a reason from the list never reaches Needs you, whatever
composed it. Unknown never asks: no verdict, an `unknown` one, a check that could not run or an answer that was
rejected is no reason on the list. Every reason but `critical` and `approval` asks only once the self-resolve
ladder was climbed (spec I22a), and the need records what was tried (`tried`, daemon words): a need for such a
reason without it is refused like one without a reason.
"""

import logging
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)


class Why(StrEnum):
    """The closed list of reasons an item may ask the admin."""

    # A critical health item of the deterministic safety net: never AI-gated, never closed by a verdict.
    CRITICAL = "critical"
    # An approval the rules require (an update, a reboot, a config change, a fork rebase): bound, with expiry.
    APPROVAL = "approval"
    # A named removal within `maintenance.ASK_BEFORE` of its deadline, or with its removal installed.
    DEADLINE = "deadline"
    # A `needs_owner` verdict that passed `verdicts.grounded`: the only way in for model text.
    GROUNDED_NEEDS_OWNER = "grounded_needs_owner"
    # A warning still reported and undecided for `STUCK_DAYS`, or past its own watch window.
    STUCK = "stuck"
    # Home Assistant itself says only the admin can do it by hand (re-auth, re-pair), or the vacuums drift.
    PHYSICAL = "physical"
    # Something the daemon did failed and nothing retries it by itself.
    FAILED = "failed"


# What the panel shows: "Asks you because: <words>" (daemon words, one per reason).
BECAUSE: dict[Why, str] = {
    Why.CRITICAL: "the safety net found something critical",
    Why.APPROVAL: "it needs your approval",
    Why.DEADLINE: "its removal deadline is close",
    Why.GROUNDED_NEEDS_OWNER: "the agent checked it and found something you must do",
    Why.STUCK: "it has stayed undecided for 7 days",
    Why.PHYSICAL: "only you can do this, by hand",
    Why.FAILED: "something the daemon did failed and nothing retries it",
}
# The reasons that ask only after the self-resolve ladder (spec I22a): their need must say what was tried.
LADDER = frozenset({Why.DEADLINE, Why.GROUNDED_NEEDS_OWNER, Why.STUCK, Why.PHYSICAL, Why.FAILED})
TRIED_MAX = 8
TRIED_CHARS = 200
# A warning that stays undecided this long, and is still reported, asks as `stuck`.
STUCK_DAYS = 7
STUCK_AFTER = timedelta(days=STUCK_DAYS)
NEED_KINDS = ("approval", "ack")
# States in which a record with a need is in Needs you (`items.in_needs`).
_ASKING = ("waiting", "failed")


def need(why: Why, kind: str, text: str, primary: dict[str, Any], secondary: list[dict[str, Any]] | None = None, *,
         expires_at: str | None = None, tried: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """The only constructor of a need: its reason from the list, the daemon's words for it, what was tried before
    it asks (`tried`, required for the `LADDER` reasons) and its actions."""
    if not isinstance(why, Why) or kind not in NEED_KINDS:
        raise ValueError(f"not a need: {why!r} {kind!r}")
    return {"kind": kind, "why": why.value, "because": BECAUSE[why], "tried": _tried(tried), "text": text,
            "primary": primary, "secondary": secondary or [], "expires_at": expires_at}  # fmt: skip


def _tried(raw: Any) -> list[str]:
    """What was tried, as stored: strings only, capped."""
    if not isinstance(raw, list):
        return []
    return [" ".join(str(t).split())[:TRIED_CHARS] for t in raw if isinstance(t, str) and t.strip()][:TRIED_MAX]


def why_of(found: Any) -> Why | None:
    """The reason a stored need gives, when it is one of the list (and the need is well formed); else None."""
    if not isinstance(found, Mapping) or found.get("kind") not in NEED_KINDS:
        return None
    try:
        return Why(str(found.get("why")))
    except ValueError:
        return None


def gate(state: str, found: Any, *, key: str = "") -> tuple[str, dict[str, Any] | None]:
    """The single escalation gate: the `(state, need)` a record may be written with. A need with a reason from the
    list passes (its `because` is the daemon's words for that reason), a `LADDER` reason only with what was tried
    (spec I22a); any other need is refused: a `waiting`
    record becomes `proposed` (Coming up) and a `failed` one keeps its state without the need. A record in any
    other state passes unchanged."""
    if found is None or state not in _ASKING:
        # Not asking: nothing to admit (the store's CHECK refuses a need outside `waiting` and `failed`).
        return state, found
    why = why_of(found)
    tried = _tried(found.get("tried")) if isinstance(found, Mapping) else []
    if why is not None and (why not in LADDER or tried):
        return state, dict(found) | {"why": why.value, "because": BECAUSE[why], "tried": tried}
    said = found.get("why") if isinstance(found, Mapping) else None
    _LOGGER.warning("need refused by the escalation gate",
                    extra={"key": key[:120], "state": state, "why": str(said)[:40],
                           "ladder": "not climbed" if why is not None else "no reason"})  # fmt: skip
    return ("proposed" if state == "waiting" else state), None
