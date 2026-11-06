"""Verdicts: the agent's checked conclusion on one cause, before (or instead of) asking the admin.

A verdict (`not_affected`, `fixed`, `fixable`, `needs_owner`, `unknown`) is kept per cause (`maint:<finding>`,
`health:<key>`, `core:<minor>`) in kv `verdict:<cause>` with the fingerprint of the evidence it was made for; a
changed fingerprint is a new question. Fail safe: no usable verdict behaves as before verdicts. Spec maintenance.md
M13d, health.md H16a; the reason is model text, shown as data only.
"""

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from opsd.access import PROTECTED_DOMAINS, named_like_access
from opsd.conversation import clean_text
from opsd.store import Store

_LOGGER = logging.getLogger(__name__)

KEY = "verdict:"
VERDICTS = ("not_affected", "fixable", "needs_owner", "unknown")
# A health verdict's word for a problem the agent itself fixed (a reload): it closes like `not_affected`.
TRIAGE_VERDICTS = ("not_affected", "fixed", "needs_owner")
REASON_CHARS = 300
# What the admin must do or decide, for a `needs_owner` verdict (model text: shown fenced, never as daemon words).
OWNER_ACTION_CHARS = 160
OWNER_ACTION_MIN = 8
# An owner action that names nothing to do is none: the verdict is then `unknown`.
_NO_ACTION = re.compile(
    r"(?i)^\W*(none|nothing|n/?a|no(thing)?\s+(action|change|fix|changes|fixes)?\s*(is\s+)?"
    r"(needed|required|to do)?)\W*$"
)
EVIDENCE_MAX = 6
# Causes whose verdicts must be grounded (`grounded`, spec maintenance.md M13g): a maintenance finding and a Core
# minor's notes. A health verdict (triage) keeps its own rules.
GROUNDED_CAUSES = ("maint:", "core:")
# Hedged reasoning is no conclusion (small keyword checks, never more prose parsing than this).
_HEDGED = re.compile(
    r"(?i:\b(?:probably|likely|unlikely|guess(?:ed|ing)?|perhaps|possibly|presumably|might|maybe|"
    r"not sure|unsure|unclear|confirm (?:it )?after|check after)\b)|\bmay\b"
)
# An owner action that only offers to keep the configuration as it is names nothing to do.
_KEEP_CHOICE = re.compile(r"(?i)\b(?:keep|keeping|or leave|leave (?:it|them|this)|as[- ]is|unchanged)\b")
# A concrete read: a config file (with or without its line), a core-compare result, an opsctl read, or an entity id.
_CONCRETE_REF = re.compile(
    r"(?i)(?:[\w./-]+\.(?:ya?ml|py|json|jinja)\b|\bcore-compare\b|\bopsctl\s+[a-z-]+|"
    r"\bentit(?:y|ies)\b|\bentr(?:y|ies)\b)|^\s*[a-z_]+\.[a-z0-9_]+\s*$"
)
# A check that gave no answer this often behaves as without a verdict (it asks as before).
MAX_TRIES = 2
# A check that could not run is tried again this long after (the item asks meanwhile).
RETRY = timedelta(days=1)
# Model text alone never closes something whose deadline is this close.
CLOSE_MARGIN = timedelta(days=14)
# Who may write a verdict (the run profile that made it, or the admin's own reopen).
BY = frozenset({"maintenance_plan", "plan_fix", "triage", "update_review", "admin"})
# Health keys a verdict never closes or quiets: access hardware, Assist exposure, Core down, a broken custom
# integration (and, by severity, anything critical).
GUARDED_HEALTH = ("access:", "assist:", "core:down", "custom:")


@dataclass(slots=True)
class Verdict:
    verdict: str
    reason: str
    fingerprint: str
    at: str
    by: str
    evidence: list[str] = field(default_factory=list)
    # Checks that gave no answer for this fingerprint (`MAX_TRIES`: behave as without a verdict).
    tries: int = 0
    # The check could not run (AI off, capped, failed): it asks as before, and is tried again after `RETRY`.
    failed: bool = False
    # Why the scan's check could not run: the run's status reason (`run_cap_reached`, `ai_disabled`, `usage_limited`,
    # `error`...) and its detail (the capped profile); the item's line is composed from them when shown.
    why: str = ""
    detail: str = ""
    # For `needs_owner`: what the admin must do or decide (model text, `OWNER_ACTION_CHARS`).
    owner_action: str = ""

    @property
    def settled(self) -> bool:
        """A verdict that decides something (not `unknown`)."""
        return self.verdict in ("not_affected", "fixed", "fixable", "needs_owner")

    @property
    def exhausted(self) -> bool:
        """No usable answer is coming: the item asks as it did before verdicts."""
        return self.verdict == "unknown" and self.tries >= MAX_TRIES

    @property
    def closing(self) -> bool:
        return self.verdict in ("not_affected", "fixed")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Answer(NamedTuple):
    verdict: str
    reason: str
    evidence: list[str]
    owner_action: str = ""


def owner_action_of(raw: Any) -> str:
    """A usable owner action (cleaned, capped), or "" when it names nothing the admin must do."""
    text = " ".join(clean_text(raw, OWNER_ACTION_CHARS * 2).split()) if isinstance(raw, str) else ""
    if len(text) < OWNER_ACTION_MIN or _NO_ACTION.match(text) or not re.search(r"[A-Za-z]{3}", text):
        return ""
    return text[:OWNER_ACTION_CHARS]


def hedged(text: str) -> bool:
    """Reasoning that only guesses ("probably", "likely", "may", "confirm after the update")."""
    return bool(_HEDGED.search(text or ""))


def concrete(evidence: Iterable[str]) -> bool:
    """At least one reference names a concrete read (`_CONCRETE_REF`)."""
    return any(_CONCRETE_REF.search(str(ref)) for ref in evidence)


def grounded(verdict: str, reason: str, evidence: Iterable[str], owner_action: str) -> str:
    """The verdict a grounded cause may keep (spec maintenance.md M13g), else `unknown`: `needs_owner` needs an
    unhedged reason, a concrete read among its evidence and an owner action that does more than keep things as
    they are; a hedged `not_affected` needs a concrete read."""
    refs = list(evidence)
    if verdict == "needs_owner" and (hedged(reason) or hedged(owner_action) or not concrete(refs)
                                     or _KEEP_CHOICE.search(owner_action or "")):  # fmt: skip
        return "unknown"
    if verdict == "not_affected" and hedged(reason) and not concrete(refs):
        return "unknown"
    return verdict


def holds(earlier: Verdict | None, verdict: str) -> bool:
    """An earlier checked `not_affected` or `unknown` for the same fingerprint holds against a new `needs_owner`:
    no new evidence, no escalation (spec maintenance.md M13h). Placeholders (a queued or failed check, no reason)
    and the admin's own reopen never hold."""
    return (verdict == "needs_owner" and earlier is not None and earlier.by != "admin" and bool(earlier.reason)
            and earlier.verdict in ("not_affected", "unknown") and not earlier.failed)  # fmt: skip


def answer(raw: Any, *, allowed: Iterable[str] = VERDICTS, strict: bool = False) -> Answer | None:
    """A model's verdict (`{verdict, reason, evidence, owner_action}`), strictly shaped and cleaned; None when there
    is none. A `needs_owner` without a usable owner action is `unknown` (spec maintenance.md M13d), or None where
    `unknown` is not allowed (triage: no verdict)."""
    if not isinstance(raw, Mapping):
        return None
    verdict = raw.get("verdict")
    if verdict not in tuple(allowed):
        return None
    reason = clean_text(str(raw.get("reason") or ""), REASON_CHARS)
    found = raw.get("evidence")
    refs: list[Any] = found if isinstance(found, list) else []
    evidence = [clean_text(str(r), 120) for r in refs[:EVIDENCE_MAX] if str(r).strip()]
    if not reason and verdict != "unknown":
        # A conclusion without a reason is no conclusion.
        return None
    action = owner_action_of(raw.get("owner_action")) if verdict == "needs_owner" else ""
    if verdict == "needs_owner" and not action:
        # Nothing named for the admin to do: it never asks on its own.
        if "unknown" not in tuple(allowed):
            return None
        verdict = "unknown"
    if strict:
        verdict = grounded(str(verdict), reason, evidence, action)
        action = action if verdict == "needs_owner" else ""
    return Answer(str(verdict), reason, evidence, action)


def load(store: Store, cause: str) -> Verdict | None:
    raw = store.kv_json(KEY + cause)
    if raw is None or raw.get("verdict") not in (*VERDICTS, "fixed"):
        return None
    try:
        found = Verdict(verdict=str(raw["verdict"]), reason=str(raw.get("reason") or ""),
                       fingerprint=str(raw.get("fingerprint") or ""), at=str(raw.get("at") or ""),
                       by=str(raw.get("by") or ""), evidence=[str(e) for e in raw.get("evidence") or []][:EVIDENCE_MAX],
                       tries=int(raw.get("tries") or 0), failed=bool(raw.get("failed")),
                       why=str(raw.get("why") or "")[:40], detail=str(raw.get("detail") or "")[:40],
                       owner_action=owner_action_of(raw.get("owner_action")))  # fmt: skip
    except TypeError, ValueError:
        return None
    if found.verdict == "needs_owner" and not found.owner_action:
        # An older verdict without an owner action (or a damaged one) asks nothing: it is checked again.
        found.verdict = "unknown"
    if cause.startswith(GROUNDED_CAUSES) and found.by != "admin":
        # Also a verdict an earlier version stored: an ungrounded one asks nothing and is checked again.
        found.verdict = grounded(found.verdict, found.reason, found.evidence, found.owner_action)
    if found.verdict != "needs_owner":
        found.owner_action = ""
    return found


def current(store: Store, cause: str, fingerprint: str) -> Verdict | None:
    """The verdict for this cause's evidence as it is now; None when there is none or the evidence changed."""
    found = load(store, cause)
    return found if found is not None and found.fingerprint == fingerprint else None


def record(store: Store, cause: str, *, fingerprint: str, verdict: str, reason: str, by: str, now: datetime,
           evidence: Iterable[str] = (), owner_action: str = "") -> Verdict:  # fmt: skip
    """Keep a verdict for this cause and fingerprint (a kv row: the items it shows are composed in the same
    transaction). A `needs_owner` without a usable owner action is kept as `unknown`; for a grounded cause an
    ungrounded one too (`grounded`), and an earlier checked verdict for the same fingerprint holds against it
    (`holds`: the earlier one is kept, one more try counted on an `unknown`)."""
    if verdict not in (*VERDICTS, "fixed") or by not in BY:
        raise ValueError(f"not a verdict: {verdict} by {by}")
    evidence = list(evidence)
    action = owner_action_of(owner_action) if verdict == "needs_owner" else ""
    if verdict == "needs_owner" and not action:
        verdict = "unknown"
    if cause.startswith(GROUNDED_CAUSES):
        verdict = grounded(verdict, reason, evidence, action)
        earlier = current(store, cause, fingerprint)
        if holds(earlier, verdict) and earlier is not None:
            if earlier.verdict == "unknown":
                earlier.tries += 1
                store.kv_set(KEY + cause, json.dumps(earlier.to_json()), now=now)
            return earlier
    if verdict != "needs_owner":
        action = ""
    found = Verdict(verdict=verdict, reason=clean_text(reason, REASON_CHARS), fingerprint=fingerprint,
                    at=now.astimezone(UTC).isoformat(timespec="seconds"), by=by,
                    evidence=[clean_text(str(e), 120) for e in evidence][:EVIDENCE_MAX],
                    owner_action=action)  # fmt: skip
    store.kv_set(KEY + cause, json.dumps(found.to_json()), now=now)
    return found


def no_answer(store: Store, cause: str, *, fingerprint: str, by: str, now: datetime, failed: bool, why: str = "",
              detail: str = "") -> Verdict:  # fmt: skip
    """A check that gave no answer for this cause: counted, so it asks as before once `MAX_TRIES` is reached.
    `failed` (AI off, capped, the run failed): it asks as before at once."""
    earlier = current(store, cause, fingerprint)
    if earlier is not None and earlier.settled:
        return earlier
    tries = MAX_TRIES if failed else (earlier.tries if earlier is not None else 0) + 1
    found = Verdict(verdict="unknown", reason="", fingerprint=fingerprint, by=by, tries=tries, failed=failed,
                    why=why, detail=detail, at=now.astimezone(UTC).isoformat(timespec="seconds"))  # fmt: skip
    store.kv_set(KEY + cause, json.dumps(found.to_json()), now=now)
    return found


def due(found: Verdict | None, now: datetime) -> bool:
    """Whether a check should run for this cause now: no verdict yet, an `unknown` short of `MAX_TRIES`, or a check
    that could not run `RETRY` ago. A settled verdict and the admin's reopen are never checked again."""
    if found is None:
        return True
    if found.settled or found.by == "admin":
        return False
    if found.failed:
        try:
            at = datetime.fromisoformat(found.at)
        except ValueError:
            return True
        return now - at >= RETRY
    return found.tries < MAX_TRIES


def reopen(store: Store, cause: str, *, fingerprint: str, now: datetime) -> Verdict:
    """The admin reopened a checked item: it behaves as without a verdict until its evidence changes (no check
    closes it again for this fingerprint)."""
    found = Verdict(verdict="unknown", reason="reopened by you", fingerprint=fingerprint,
                    at=now.astimezone(UTC).isoformat(timespec="seconds"), by="admin", tries=MAX_TRIES)  # fmt: skip
    store.kv_set(KEY + cause, json.dumps(found.to_json()), now=now)
    return found


def health_fingerprint(key: str, summary: str) -> str:
    """A health item's evidence for a verdict: its key and its summary (a count that changes is a new question)."""
    return hashlib.sha256(f"{key}|{summary}".encode()).hexdigest()[:16]


def notes_fingerprint(notes: Iterable[tuple[str, str | None, str]]) -> str:
    """The release notes that hold a Core update, for a verdict: their kinds, integrations and titles (a note added,
    removed or retitled is a new question; a reworded text is not)."""
    parts = sorted(f"{kind}|{domain or ''}|{title.strip().lower()}" for kind, domain, title in notes)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def guarded(*, severity: str = "warning", key: str = "", domain: str | None = None, text: str = "") -> bool:
    """A cause no verdict may close or quiet: critical, access hardware, a lock or the alarm, Assist exposure,
    Core down or a broken custom integration."""
    if severity == "critical" or key.startswith(GUARDED_HEALTH):
        return True
    if domain in PROTECTED_DOMAINS:
        return True
    return bool(domain and named_like_access(domain)) or bool(text and named_like_access(text))


def closes(verdict: Verdict | None, *, guard: bool, listed: bool = False, deadline: datetime | None = None,
           now: datetime) -> str:  # fmt: skip
    """What a verdict may do to its item: `close` (resolved, in History with the reason), `quiet` (Coming up with
    the reason: a repair Home Assistant still lists, or a deadline within `CLOSE_MARGIN` that only model text
    vouches for) or `none` (no closing verdict, or a guarded cause)."""
    if verdict is None or not verdict.closing or guard:
        return "none"
    if listed:
        return "quiet"
    if deadline is not None and deadline - now <= CLOSE_MARGIN:
        return "quiet"
    return "close"
