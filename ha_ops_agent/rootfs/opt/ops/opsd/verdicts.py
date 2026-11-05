"""Verdicts: the agent's checked conclusion on one cause, before (or instead of) asking the admin.

A verdict answers "does this really need the admin here?" for one cause: a maintenance finding
(`maint:<finding>`), a health item (`health:<key>`) or the release notes that hold a Core update into a
minor (`core:<minor>`). It is produced by the agent's own investigation (the maintenance plan run, Plan a
fix, triage, the isolated update review) and stored here, in `kv` `verdict:<cause>`, with the fingerprint
of the evidence it was made for. A changed fingerprint is a new question: the old verdict is not read.

- `not_affected`: checked, nothing here is affected; the item may close (within the guards, `closes`).
- `fixable`: the agent can propose the fix (a proposal follows the usual path).
- `needs_owner`: only the admin can do or decide it; the item asks, with the agent's reason.
- `unknown`: the agent could not tell; the item behaves as without a verdict.

Fail safe: no verdict, `unknown`, a run that failed or gave no answer twice: everything behaves as
before verdicts existed. The model's reason is data shown under The agent says, never a title, a need or
a push.
"""

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from opsd.access import PROTECTED_DOMAINS, named_like_access
from opsd.conversation import clean_text
from opsd.store import Store, StoreError

_LOGGER = logging.getLogger(__name__)

KEY = "verdict:"
VERDICTS = ("not_affected", "fixable", "needs_owner", "unknown")
# A health verdict's word for a problem the agent itself fixed (a reload): it closes like `not_affected`.
TRIAGE_VERDICTS = ("not_affected", "fixed", "needs_owner")
REASON_CHARS = 300
EVIDENCE_MAX = 6
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


def answer(raw: Any, *, allowed: Iterable[str] = VERDICTS) -> tuple[str, str, list[str]] | None:
    """A model's verdict (`{verdict, reason, evidence}`), strictly shaped and cleaned; None when there is none."""
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
    return str(verdict), reason, evidence


def load(store: Store, cause: str) -> Verdict | None:
    try:
        raw = json.loads(store.kv_get(KEY + cause) or "null")
    except (StoreError, sqlite3.Error, ValueError) as err:
        _LOGGER.info("verdict unreadable", extra={"cause": cause[:80], "error": str(err)[:120]})
        return None
    if not isinstance(raw, dict) or raw.get("verdict") not in (*VERDICTS, "fixed"):
        return None
    try:
        return Verdict(verdict=str(raw["verdict"]), reason=str(raw.get("reason") or ""),
                       fingerprint=str(raw.get("fingerprint") or ""), at=str(raw.get("at") or ""),
                       by=str(raw.get("by") or ""), evidence=[str(e) for e in raw.get("evidence") or []][:EVIDENCE_MAX],
                       tries=int(raw.get("tries") or 0), failed=bool(raw.get("failed")))  # fmt: skip
    except TypeError, ValueError:
        return None


def current(store: Store, cause: str, fingerprint: str) -> Verdict | None:
    """The verdict for this cause's evidence as it is now; None when there is none or the evidence changed."""
    found = load(store, cause)
    return found if found is not None and found.fingerprint == fingerprint else None


def record(store: Store, cause: str, *, fingerprint: str, verdict: str, reason: str, by: str, now: datetime,
           evidence: Iterable[str] = ()) -> Verdict:  # fmt: skip
    """Keep a verdict for this cause and fingerprint (a kv row: the items it shows are composed in the same
    transaction)."""
    if verdict not in (*VERDICTS, "fixed") or by not in BY:
        raise ValueError(f"not a verdict: {verdict} by {by}")
    found = Verdict(verdict=verdict, reason=clean_text(reason, REASON_CHARS), fingerprint=fingerprint,
                    at=now.astimezone(UTC).isoformat(timespec="seconds"), by=by,
                    evidence=[clean_text(str(e), 120) for e in evidence][:EVIDENCE_MAX])  # fmt: skip
    store.kv_set(KEY + cause, json.dumps(found.to_json()), now=now)
    return found


def no_answer(store: Store, cause: str, *, fingerprint: str, by: str, now: datetime, failed: bool) -> Verdict:
    """A check that gave no answer for this cause: counted, so it asks as before once `MAX_TRIES` is reached.
    `failed` (AI off, capped, the run failed): it asks as before at once."""
    earlier = current(store, cause, fingerprint)
    if earlier is not None and earlier.settled:
        return earlier
    tries = MAX_TRIES if failed else (earlier.tries if earlier is not None else 0) + 1
    found = Verdict(verdict="unknown", reason="", fingerprint=fingerprint, by=by, tries=tries, failed=failed,
                    at=now.astimezone(UTC).isoformat(timespec="seconds"))  # fmt: skip
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


def forget(store: Store, cause: str, now: datetime) -> None:
    store.kv_set(KEY + cause, "", now=now)


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
           now: datetime, deterministic: bool = False) -> str:  # fmt: skip
    """What a verdict may do to its item: `close` (resolved, in History with the reason), `quiet` (Coming up with
    the reason: a repair Home Assistant still lists, or a deadline within `CLOSE_MARGIN` that only model text
    vouches for) or `none` (no closing verdict, or a guarded cause)."""
    if verdict is None or not verdict.closing or guard:
        return "none"
    if listed:
        return "quiet"
    if deadline is not None and deadline - now <= CLOSE_MARGIN and not deterministic:
        return "quiet"
    return "close"
