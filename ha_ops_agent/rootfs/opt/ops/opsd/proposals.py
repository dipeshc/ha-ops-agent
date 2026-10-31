"""`approval.request`: a chat asks the admin to approve one update.

A proposal is a highlight in the panel, never an approval. It names one
APPROVE-tier update of the *stored* plan: the AI passes only the entity id,
and the versions, title and tier are looked up here from the latest plan
run, so a model (or text injected into what it read) cannot choose them. It
lives in its own table, so nothing in the approval state machine can read it
as pending or approved; approving still takes the panel request (ingress
admin, CSRF), exactly as without it.

Limits: chat-origin runs only (the policy origin list), two per run and six
a day (registry quotas), five open at a time, one per update version, none
for a version the admin rejected in the last week, and 72 hours of life.
A proposal closes when the admin acts on that update in the panel, and is
void as soon as the plan no longer offers exactly that version.

`opsctl request-approval backup` asks the admin to back up now: a
proposal with the fixed entity `BACKUP_PROPOSAL` whose chat card has a
"Back up now" button (the panel's high-impact action). It never runs a
backup itself; it closes when a backup finishes and expires after 72 hours.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from opsd import selfguard
from opsd.approvals import LIVE, approvable
from opsd.capabilities import Args, CapabilityError
from opsd.conversation import clean_text
from opsd.policy import same_version
from opsd.runctx import ExecContext, RunContext
from opsd.store import Store

PROPOSAL_TTL = timedelta(hours=72)
MAX_OPEN = 5
REJECT_MEMORY = timedelta(days=7)
PROFILES = frozenset({"chat", "followup", "investigate"})
BACKUP_PROPOSAL = "backup.now"
BACKUP_TITLE = "Back up now"


def _store(ctx: ExecContext) -> Store:
    if ctx.env.store is None:
        raise CapabilityError("the store is not available")
    return ctx.env.store


def _run(ctx: ExecContext) -> RunContext:
    if ctx.run is None or ctx.run.profile not in PROFILES:
        raise CapabilityError("approval requests come from a chat with the admin")
    return ctx.run


def validate_request(ctx: ExecContext, args: Args) -> Args:
    _run(ctx)
    entity = args.opt_text("entity")
    if not entity:
        raise CapabilityError("entity is required: an update entity from `opsctl update-plan`")
    if selfguard.protected_update_entity(entity) and entity != selfguard.OS_UPDATE_ENTITY:
        raise CapabilityError(f"{entity}: the agent never updates itself or the Supervisor; the admin does")
    return args


def _latest_plan(store: Store) -> dict[str, Any] | None:
    return next(iter(store.recent_update_runs(limit=1)), None)


def _matches(proposal: Mapping[str, Any], item: Mapping[str, Any] | None) -> bool:
    return (
        item is not None
        and approvable(item)
        and same_version(item.get("installed"), proposal["from_version"])
        and same_version(item.get("latest"), proposal["to_version"])
    )


def refresh(store: Store, *, now: datetime) -> list[dict[str, Any]]:
    """Open proposals that still match the latest plan; the rest become expired or void."""
    latest = _latest_plan(store)
    items = {str(i.get("entity_id")): i for i in (latest or {}).get("plan", [])}
    kept = []
    for proposal in store.proposals(states=("open",), limit=100):
        if now >= datetime.fromisoformat(proposal["expires_at"]):
            store.settle_proposal(proposal["id"], to="expired", now=now)
        elif proposal["entity_id"] == BACKUP_PROPOSAL:
            kept.append(proposal)
        elif not _matches(proposal, items.get(proposal["entity_id"])):
            store.settle_proposal(proposal["id"], to="void", now=now, detail="the plan no longer offers this version")
        else:
            kept.append(proposal)
    return kept


def settle(store: Store, entity_id: str, *, now: datetime, detail: str) -> int:
    """The admin acted on this update in the panel: its open proposals are done."""
    closed = 0
    for proposal in store.proposals(states=("open",), limit=100):
        if proposal["entity_id"] == entity_id and store.settle_proposal(proposal["id"], to="done", now=now,
                                                                        detail=detail):  # fmt: skip
            closed += 1
    return closed


def settle_backup(store: Store, *, now: datetime, detail: str) -> int:
    """A backup finished (or failed): open "Back up now" requests are done."""
    return settle(store, BACKUP_PROPOSAL, now=now, detail=detail)


def _request_backup(store: Store, run: RunContext, now: datetime) -> str:
    open_now = refresh(store, now=now)
    if any(p["entity_id"] == BACKUP_PROPOSAL for p in open_now):
        return "ok: already asked the admin to back up now; the button is waiting in the chat"
    if len(open_now) >= MAX_OPEN:
        raise CapabilityError(f"{MAX_OPEN} approval requests are already waiting for the admin")
    store.create_proposal(entity_id=BACKUP_PROPOSAL, from_version="", to_version="", plan_run=0, title=BACKUP_TITLE,
                          thread_id=run.thread_id, run_id=run.run_id, requested_by=run.principal.label(), now=now,
                          expires_at=now + PROPOSAL_TTL)  # fmt: skip
    return (
        "ok: asked the admin to back up now. Nothing runs until the admin taps Back up now (in the chat or in System,"
        " the gear). Tell them that."
    )


def void_runs(store: Store, run_ids: list[str], *, now: datetime, detail: str) -> int:
    """Void the open requests these runs made (a cancelled investigation's)."""
    voided = 0
    for run_id in run_ids:
        for proposal in store.proposals(states=("open",), run_id=run_id, limit=10):
            voided += store.settle_proposal(proposal["id"], to="void", now=now, detail=detail)
    return voided


def _recently_rejected(store: Store, subject: str, now: datetime) -> str | None:
    for found in store.approvals(states=("rejected",), limit=200):
        decided = found.get("decided_at")
        if found["subject"] == subject and decided and now - datetime.fromisoformat(decided) < REJECT_MEMORY:
            return str(decided)[:10]
    return None


def exec_request(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    store = _store(ctx)
    now = datetime.now(UTC)
    entity = args.text("entity")
    if entity == "backup":
        return _request_backup(store, run, now)
    latest = _latest_plan(store)
    if latest is None:
        raise CapabilityError(f"there is no update plan yet (the window plans at {ctx.options.updates.window})")
    item = next((i for i in latest["plan"] if i.get("entity_id") == entity), None)
    if item is None:
        raise CapabilityError(f"{entity} is not in the latest update plan ({latest['window_day']})")
    if not approvable(item):
        reason = clean_text(str(item.get("reason") or ""), 160)
        raise CapabilityError(f"{entity} is not an update to approve: tier {item.get('tier')} ({reason})")
    frm, to = str(item["installed"]), str(item["latest"])
    subject = f"{entity}@{to}"
    live = next((a for a in store.approvals(states=LIVE, limit=200) if a["subject"] == subject), None)
    if live is not None:
        raise CapabilityError(f"the admin already {'approved' if live['state'] == 'approved' else 'started'}"
                              " an approval for this update; nothing to ask")  # fmt: skip
    rejected = _recently_rejected(store, subject, now)
    if rejected is not None:
        raise CapabilityError(f"the admin rejected this update on {rejected}; do not ask again this week")
    open_now = refresh(store, now=now)
    title = clean_text(str(item.get("title") or entity), 60)
    same = next((p for p in open_now if p["entity_id"] == entity and same_version(p["to_version"], to)), None)
    if same is not None:
        return f"ok: already asked the admin to approve {title} {frm} -> {to}; it is waiting in Needs you on Home"
    if len(open_now) >= MAX_OPEN:
        raise CapabilityError(f"{MAX_OPEN} approval requests are already waiting for the admin")
    store.create_proposal(entity_id=entity, from_version=frm, to_version=to, plan_run=int(latest["id"]), title=title,
                          thread_id=run.thread_id, run_id=run.run_id, requested_by=run.principal.label(), now=now,
                          expires_at=now + PROPOSAL_TTL)  # fmt: skip
    return (
        f"ok: asked the admin to approve {title} {frm} -> {to}. Nothing is approved: the admin approves or"
        " rejects it on its item in Needs you on Home. Tell them that."
    )


def for_run(store: Store, run_id: str) -> list[dict[str, Any]]:
    """What a run asked for, for its chat turn (titles and versions come from the plan, not the model)."""
    return [
        {
            "entity_id": p["entity_id"],
            "title": p["title"],
            "from": p["from_version"],
            "to": p["to_version"],
            "kind": "backup" if p["entity_id"] == BACKUP_PROPOSAL else "update",
        }
        for p in store.proposals(states=("open", "done", "void", "expired"), run_id=run_id, limit=5)
    ]
