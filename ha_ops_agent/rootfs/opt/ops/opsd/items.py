"""Items: one daemon record, one state, for everything the agent does.

Every surface reads the same record: the Home sections and the Needs you
badge (`api/home`), an item's page (`api/items/<id>`), History, the push
for that item, and the older views (Activity's "needs admin", the update
approvals, tasks and changes, which carry their item's id and state).
Nothing counts or infers "Needs you" anywhere else.

The record (`items` in the store, `item_events` for its timeline):

- `state` is a closed vocabulary: proposed, waiting (shown as "Needs you"),
  scheduled, running, done, failed, cancelled, expired. An item is in Needs
  you if and only if it is `waiting`, or `failed` with a `need` (an
  acknowledgement); the store derives `needs` from those two columns and
  its CHECK keeps `need` present exactly while waiting.
- Daemon-owned fields (`title`, `gist`, `need`, `facts`, `phase`) are
  composed here from daemon records. `agent` is the only field that holds
  model text (a release-note review, a message from the agent), as data.
- `revision` moves on every material change (state, words, need, actions,
  phase). An action is accepted only for the revision the admin saw. `as_of`
  (when the daemon last observed the facts behind it) changes without a new
  revision.

Derived items are recomputed by `ItemBook.sync` from the records that run
the house's processes (update runs and jobs, approvals, change proposals,
house facts, tasks, the health check's open items): the items are their
one view, so a resolved process can never leave a stale "needs you" card.
An item whose source disappears is resolved. A `follows` child (today: the
Supervisor's `reboot_required` issue, which follows the OS update that
needs the reboot) waits on its parent and is done when the parent is done.
Activity entries that belong to no process (a health check, a reload) are
standalone items written by `ActivityLog`.

Pushes (by design): none for routine results, summaries or
digests. The book pushes only when an item enters Needs you (an approval to
give, a reboot to allow, a failure or escalation to acknowledge), once per
need, re-surfaced at most once a day while it waits; time-sensitive for
escalations. Title, body and actions come from the record (never `agent`),
the tag is the item id so the Companion app replaces an item's earlier push,
and tapping opens the item. The health check pushes its own items.
"""

import hashlib
import logging
import re
import sqlite3
import statistics
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

from opsd.approvals import LIVE, approvable, is_change
from opsd.channels import BudgetClass, Message
from opsd.conversation import clean_text
from opsd.live import Hub
from opsd.notify import panel_url
from opsd.policy import Tier, same_version
from opsd.store import JOB_FINAL, Store, StoreError, UpdateJob
from opsd.updates import ABORT_FAILED_KEY, REBOOT_HELP, needs_admin, waits_quietly

_LOGGER = logging.getLogger(__name__)

STATES = ("proposed", "waiting", "scheduled", "running", "done", "failed", "cancelled", "expired")
TERMINAL = frozenset({"done", "failed", "cancelled", "expired"})
WORD = {"proposed": "Proposed", "waiting": "Needs you", "scheduled": "Scheduled", "running": "Running",
        "done": "Done", "failed": "Failed", "cancelled": "Cancelled", "expired": "Expired"}  # fmt: skip
NEED_KINDS = ("approval", "confirm", "physical", "answer", "ack")
ACTORS = ("you", "daemon", "agent")
ITEM_ID_RE = re.compile(r"[a-z]-[0-9a-z]{1,40}")
ACTION_RE = re.compile(r"[a-z_]{1,32}")
_PREFIX = {"updates": "u", "window": "w", "change": "p", "fact": "p", "task": "r", "health": "c", "activity": "a"}
KINDS = {"core": "Core update", "addon": "Add-on update", "hacs": "HACS update", "firmware": "Firmware update",
         "os": "Operating system update"}  # fmt: skip
USUAL_S = {"core": 600, "addon": 240, "hacs": 120, "firmware": 600, "os": 900}
JOBS_SINCE = timedelta(days=14)
RECENT_REJECT = timedelta(days=7)
RESURFACE = timedelta(days=1)
# A push that was not delivered (failed, rate limited) is tried again after this long.
RETRY = timedelta(minutes=15)
# Outcomes that count as delivered: sent, relayed while Core was down, or held for after quiet hours.
DELIVERED = frozenset({"sent", "relayed", "held"})
PUSH_BODY = 400
BACKFILL_KEY = "items.backfill"
ACK_KEY = "item.ack:"
REBOOT_SUBJECT = "reboot:"


def item_id(key: str) -> str:
    """The URL-safe handle of a key (`updates:update.x@1.2` -> `u-…`); stable, so events can precede the row."""
    if key.startswith("activity:") and key[9:].isdigit():
        return f"a-{key[9:]}"
    prefix = _PREFIX.get(key.split(":", 1)[0], "x")
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def update_key(entity_id: str, version: str) -> str:
    return f"updates:{entity_id}@{version}"


def window_key(run_id: int) -> str:
    return f"window:{run_id}"


def change_key(change_id: int) -> str:
    return f"change:{change_id}"


def deep_link(item: str) -> str:
    """Tapping a push opens the item (`<panel>/t/<item id>`; the shell serves it, the panel routes `#t/<id>`)."""
    return f"{panel_url()}/t/{item}" if ITEM_ID_RE.fullmatch(item) else panel_url()


def in_needs(state: str, need: Any) -> bool:
    return state == "waiting" or (state == "failed" and need is not None)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


REVIEW_HELD = "held by the release-note review"


def daemon_reason(item: Mapping[str, Any]) -> str:
    """Why an update needs approval, in the daemon's words: the review's own reasons (model text) stay in `agent`."""
    reason = str(item.get("reason") or "")
    if reason.startswith(REVIEW_HELD):
        return "the release-note review held it back; its reasons are under The agent says"
    return clean_text(reason, 160)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def act(action_id: str, label: str, style: str | None = None) -> dict[str, Any]:
    found = {"id": action_id, "label": label}
    if style:
        found["style"] = style
    return found


@dataclass(slots=True)
class Spec:
    """What the daemon's records say one item is now."""

    key: str
    kind: str
    title: str
    state: str
    gist: str = ""
    kicker: str = ""
    ref: str = ""
    need: dict[str, Any] | None = None
    about: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    phase: dict[str, Any] | None = None
    follows: str | None = None
    facts: list[dict[str, Any]] = field(default_factory=list)
    agent: dict[str, Any] | None = None
    change: dict[str, Any] | None = None
    footnote: str | None = None
    quiet: bool = False
    # Due or run time (scheduled, proposed); the source's own time of this state (needs since, resolved at).
    when: str | None = None
    since: str | None = None
    created: str | None = None
    as_of: str | None = None
    # Internal: what an action binds to (entity, plan run, job, change, task, health key).
    source: dict[str, Any] = field(default_factory=dict)
    actor: str = "daemon"
    event: str | None = None
    urgent: bool = False
    pushes: bool = True


@dataclass(slots=True)
class Context:
    """What the derivation reads besides the store (from the daemon, or a test)."""

    window: str = "04:00"
    gate: str | None = None
    paused: bool = False
    health_at: str | None = None
    health_items: list[dict[str, Any]] = field(default_factory=list)
    redact: Callable[[str], str] = lambda text: text


def need(kind: str, text: str, primary: dict[str, Any], secondary: list[dict[str, Any]] | None = None, *,
         expires_at: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"kind": kind, "text": text, "primary": primary, "secondary": secondary or [], "expires_at": expires_at}


def soaking(item: Mapping[str, Any] | None) -> bool:
    """A candidate still soaking (`policy.classify`): Proposed, approvable early, never pushed."""
    return item is not None and item.get("tier") == Tier.APPROVE and waits_quietly(item)


def acked(store: Store, key: str, token: str) -> bool:
    """The admin acknowledged this failure (`token` names the exact record, so a new failure asks again)."""
    try:
        return store.kv_get(f"{ACK_KEY}{key}") == token
    except StoreError, sqlite3.Error:
        return False


# ---------------------------------------------------------------- derivation


class Deriver:
    """Builds every derived item's `Spec` from the store (pure reads)."""

    def __init__(self, store: Store, ctx: Context, now: datetime, tz: tzinfo) -> None:
        self.store = store
        self.ctx = ctx
        self.now = now
        self.tz = tz
        self.specs: dict[str, Spec] = {}
        # The latest plan's pending updates, by entity (for an update item that is no longer derived).
        self.plan: Mapping[str, Mapping[str, Any]] = {}
        # When that plan was made (None: no plan yet), and the update items only that plan speaks for (no job, no
        # live approval): what they need is the plan's classification, which an earlier rule may have made.
        self.plan_at: datetime | None = None
        self.plan_only: set[str] = set()

    def run(self) -> dict[str, Spec]:
        self.updates()
        self.changes()
        self.facts()
        self.tasks()
        self.health()
        self.follows()
        return self.specs

    def add(self, spec: Spec) -> None:
        self.specs[spec.key] = spec

    def _window_at(self) -> str:
        """The next update window, in UTC."""
        try:
            hour, minute = (int(x) for x in self.ctx.window.split(":"))
        except ValueError:
            hour, minute = 4, 0
        local = self.now.astimezone(self.tz)
        start = datetime.combine(local.date(), time(hour, minute), self.tz)
        if start <= local:
            start = datetime.combine(local.date() + timedelta(days=1), time(hour, minute), self.tz)
        return _iso(start)

    def _usual(self, kind: str, jobs: list[UpdateJob]) -> int:
        took = []
        for job in jobs:
            start, end = _parse(job.started_at), _parse(job.finished_at)
            if job.kind == kind and job.state == "succeeded" and start and end and end > start:
                took.append((end - start).total_seconds())
        return int(statistics.median(took)) if took else USUAL_S.get(kind, 600)

    # ---------------------------------------------------------------- updates

    def updates(self) -> None:
        store = self.store
        runs = store.recent_update_runs(limit=1)
        latest = runs[0] if runs else None
        plan = {str(i.get("entity_id")): i for i in (latest or {}).get("plan", []) if isinstance(i, dict)}
        self.plan = plan
        self.plan_at = _parse((latest or {}).get("created_at"))
        jobs = store.recent_update_jobs(since=self.now - JOBS_SINCE)
        by_subject: dict[str, UpdateJob] = {}
        for job in jobs:
            by_subject[f"{job.entity_id}@{job.to_version}"] = job
        approvals = [a for a in store.approvals(limit=200) if not is_change(a)]
        live: dict[str, dict[str, Any]] = {}
        decided: dict[str, dict[str, Any]] = {}
        for found in approvals:  # newest first
            target = live if found["state"] in LIVE else decided
            target.setdefault(str(found["subject"]), found)
        active = store.active_update_run()
        subjects: dict[str, tuple[str, str]] = {}
        for entity, planned in plan.items():
            if planned.get("latest") and (approvable(planned) or (planned.get("tier") == Tier.AUTO
                                                                  and not planned.get("held"))):  # fmt: skip
                subjects[f"{entity}@{planned['latest']}"] = (entity, str(planned["latest"]))
        for subject, job in by_subject.items():
            subjects.setdefault(subject, (job.entity_id, job.to_version))
        for subject in live:
            if subject.startswith(REBOOT_SUBJECT) or "@" not in subject:
                continue
            entity, version = subject.rsplit("@", 1)
            subjects.setdefault(subject, (entity, version))
        for subject, (entity, version) in subjects.items():
            offered = plan.get(entity)
            item = offered if offered is not None and same_version(offered.get("latest"), version) else None
            spec = self._update(entity, version, item, by_subject.get(subject), live, decided, latest, active, jobs)
            if spec is not None:
                self.add(spec)
                if subject not in by_subject and subject not in live:
                    self.plan_only.add(spec.key)
        for run in store.recent_update_runs(limit=30):
            spec = self._window(run)
            if spec is not None:
                self.add(spec)

    def _update(self, entity: str, version: str, item: Mapping[str, Any] | None, job: UpdateJob | None,  # noqa: PLR0917
                live: Mapping[str, dict[str, Any]], decided: Mapping[str, dict[str, Any]],
                latest: Mapping[str, Any] | None, active: Mapping[str, Any] | None,
                jobs: list[UpdateJob]) -> Spec | None:  # fmt: skip
        ctx = self.ctx
        subject = f"{entity}@{version}"
        kind = str((item or {}).get("kind") or (job.kind if job else "") or "")
        name = clean_text(str((item or {}).get("title") or (job.title if job else entity)), 80)
        frm = str((item or {}).get("installed") or (job.from_version if job else "?"))
        title = f"{name} {job.from_version if job else frm} → {version}"
        key = update_key(entity, version)
        base = Spec(key=key, kind="update", title=title, state="proposed", kicker=KINDS.get(kind, "Update"),
                    source={"entity": entity, "version": version, "kind": kind,
                            "plan_run": int(latest["id"]) if latest else None})  # fmt: skip
        if latest is not None:
            base.as_of = str(latest.get("created_at") or "")
        fix = (item or {}).get("fix")
        if isinstance(fix, Mapping) and fix.get("change"):
            # Its link to the config change it needs (opsd.updatefix): that proposal's own item is the one tap.
            base.facts.append({"label": "Config change it needs", "value": f"#{fix['change']}: {fix.get('state')}",
                               "at": (latest or {}).get("created_at")})  # fmt: skip
        review = (item or {}).get("review")
        if isinstance(review, Mapping) and review.get("reason"):
            base.agent = {"text": f"Release-note review: risk {review.get('risk')}. {review.get('reason')}",
                          "sources": ["release notes"], "not_read": [], "run_id": None}  # fmt: skip
        if job is not None:
            base.source["job"] = job.id
            words = job.state.replace("_", " ")
            value = f"{words}: {clean_text(job.detail, 200)}" if job.detail else words
            base.facts = [{"label": "Update job", "value": value, "at": job.updated_at}]
            base.as_of = max(base.as_of or "", job.updated_at)
            base.created = job.started_at or job.updated_at
        if job is not None and job.state not in JOB_FINAL:
            return self._running(base, job, jobs)
        if job is None and active is not None and item is not None:
            planned = next((i for i in active.get("plan", []) if isinstance(i, dict) and i.get("entity_id") == entity
                            and same_version(i.get("latest"), version) and i.get("execute")), None)  # fmt: skip
            if planned is not None:
                base.state, base.gist = "running", "taking a full backup before the install"
                names = ["Backup", "Install", "Reboot", "Verify"] if kind == "os" else ["Backup", "Install", "Verify"]
                base.phase = {"names": names, "current": 0, "started_at": active.get("created_at"),
                              "usual_s": self._usual(kind, jobs)}  # fmt: skip
                return base
        if job is not None:
            settled = self._settled(base, job, live)
            if settled is not None:
                return settled
        if item is None:
            # The latest plan no longer lists it (installed or superseded): a live approval for it is not enough.
            if job is not None and job.state == "skipped":
                base.state, base.gist = "cancelled", f"not installed: {clean_text(job.detail, 160)}"
                base.since = job.finished_at
                token = f"job:{job.id}"
                if needs_admin(job) and not acked(self.store, base.key, token):
                    # An approved job that did not run always tells the admin (`updates.needs_admin`).
                    base.state = "failed"
                    base.need = need("ack", f"Approved but not installed: {clean_text(job.detail, 300)}.",
                                     act("ack", "OK"))  # fmt: skip
                    base.source["ack"] = token
                return base
            return None
        skipped = f" (not installed last time: {clean_text(job.detail, 120)})" if job and job.state == "skipped" else ""
        if item.get("tier") == Tier.AUTO and not item.get("held"):
            base.when = self._window_at()
            if ctx.gate is None:
                base.state, base.gist = "scheduled", f"installs automatically tonight at {ctx.window}{skipped}"
                base.about = ("Runs in the update window through the normal pipeline: a full backup, the install, a "
                              "health check and a rollback if health gets worse."
                              + (" The host reboots once to activate it; the operating system is never rolled back by "
                                 "the daemon (a failed boot falls back to the other slot by itself)."
                                 if kind == "os" else ""))  # fmt: skip
            else:
                base.state, base.gist = "proposed", f"would install automatically, but {ctx.gate}{skipped}"
            return base
        if not approvable(item):
            return None
        approval = live.get(subject)
        reason = daemon_reason(item)
        if approval is not None and approval["state"] == "approved":
            when = "now" if approval.get("run_when") == "now" else f"tonight at {ctx.window}"
            base.state, base.gist = "scheduled", f"approved; installs {when}"
            if ctx.gate is not None:
                base.gist += f" (waits: {ctx.gate})"
            base.when = self._window_at() if approval.get("run_when") != "now" else approval.get("decided_at")
            base.about = (
                "Installs through the normal pipeline: a full backup, the install, a health check and a "
                "rollback if health gets worse." + (" The host reboots once to activate it." if kind == "os" else "")
            )
            base.actions = [act("withdraw", "Withdraw approval", "text")]
            base.actor, base.event = "you", f"Approved by you: installs {when}"
            base.source["approval"] = approval["id"]
            base.footnote = (
                f"Approval {str(approval['id'])[:4]} · bound to {frm} → {version}, plan #{base.source['plan_run']}"
            )
            return base
        rejected = decided.get(subject)
        if rejected is not None and rejected["state"] == "rejected" and approval is None:
            rejected_at = _parse(rejected.get("decided_at") or rejected.get("created_at"))
            if rejected_at is not None and self.now - rejected_at < RECENT_REJECT:
                base.state, base.gist, base.since = "cancelled", "rejected by you", rejected.get("decided_at")
                base.actor, base.event = "you", "Rejected by you"
                return base
        os_reboot = " The host reboots once to activate it." if kind == "os" else ""
        secondary = [] if kind == "os" else [act("approve_now", "Install now")]
        if soaking(item):
            # It waits quietly until it has soaked; the admin may approve it early (the same approval).
            base.state, base.gist = "proposed", f"waits: {reason}{skipped}"
            base.about = (
                f"It installs on its own once it has soaked, unless a repair or a breaking change touches this "
                f"house. Approve it early and it installs tonight at {ctx.window} through the normal pipeline."
            )
            base.actions = [act("approve", "Approve early"), *secondary, act("reject", "Reject", "text")]
            if approval is not None:
                base.source["approval"] = approval["id"]
            return base
        text = (f"{name} {frm} → {version} needs your approval ({reason}). Approve it and it installs tonight at "
                f"{ctx.window} through the normal pipeline: a full backup, the install, a health check and a rollback "
                f"if health gets worse.{os_reboot}")  # fmt: skip
        secondary.append(act("reject", "Reject", "text"))
        base.state, base.gist = "waiting", f"needs your approval{skipped}"
        base.need = need("approval", text, act("approve", "Approve for tonight"), secondary)
        base.since = (job.finished_at if skipped and job else None) or str((latest or {}).get("created_at") or "")
        if approval is not None:
            base.source["approval"] = approval["id"]
        return base

    def _running(self, base: Spec, job: UpdateJob, jobs: list[UpdateJob]) -> Spec:
        names = ["Backup", "Install", "Reboot", "Verify"] if job.kind == "os" else ["Backup", "Install", "Verify"]
        reboot = _dict(job.target.get("reboot"))
        match job.state:
            case "pending":
                current, gist = 1, "waiting its turn in this update run"
            case "installing":
                current, gist = 1, "installing"
            case "rebooting":
                current = 2
                gist = (
                    "rebooting the host to activate it"
                    if reboot.get("requested_at")
                    else "installed; the host reboots to activate it"
                )
            case "rolling_back":
                current, gist = len(names) - 1, f"health got worse; rolling back to {job.from_version}"
            case _:
                current, gist = len(names) - 1, "checking health against the check before the update"
        base.state, base.gist = "running", gist
        base.phase = {"names": names, "current": current, "started_at": job.started_at or job.updated_at,
                      "usual_s": self._usual(job.kind, jobs)}  # fmt: skip
        return base

    def _settled(self, base: Spec, job: UpdateJob, live: Mapping[str, dict[str, Any]]) -> Spec | None:
        """A finished job's item, or None when the plan decides (a skipped job, an acknowledged failure)."""
        store = self.store
        key = base.key
        reboot = _dict(job.target.get("reboot"))
        if job.state == "succeeded":
            base.state = "done"
            base.gist = "installed after a reboot" if reboot else "installed"
            base.since = job.finished_at
            base.phase = None
            return base
        if job.state == "reboot_pending":
            approval = live.get(f"{REBOOT_SUBJECT}{job.entity_id}@{job.to_version}")
            base.source.update({"job": job.id, "reboot": True})
            base.facts.append({"label": "Supervisor", "value": f"{job.from_version} running, {job.to_version} "
                               "installed and pending a reboot", "at": job.finished_at or job.updated_at})  # fmt: skip
            if approval is not None and approval["state"] == "approved":
                base.state = "scheduled"
                base.gist = f"installed; the daemon reboots the host tonight at {self.ctx.window} to activate it"
                base.when = self._window_at()
                base.actions = [act("withdraw", "Withdraw approval", "text")]
                base.source["approval"] = approval["id"]
                base.actor, base.event = "you", f"Reboot approved by you: tonight at {self.ctx.window}"
                return base
            base.state, base.gist = "waiting", "installed, the host needs a reboot to finish"
            text = (f"{job.to_version} is installed and waits for one reboot; the house still runs {job.from_version}. "
                    f"Approve and the daemon reboots the host in tonight's update window at {self.ctx.window} (a few "
                    f"minutes offline), then confirms {job.to_version} and compares health with the check before the "
                    f"update. Or reboot it yourself ({REBOOT_HELP}); the daemon confirms it afterwards.")  # fmt: skip
            base.need = need("approval", text, act("reboot_window", f"Reboot tonight at {self.ctx.window}"))
            base.since = job.finished_at or job.updated_at
            if approval is not None:
                base.source["approval"] = approval["id"]
            return base
        if job.state in ("failed", "rolled_back", "rollback_failed"):
            token = f"job:{job.id}"
            fell_back = job.state == "failed" and job.detail.startswith("after the reboot")
            # Rolled back, escalated or not activated: time-sensitive (never held or capped).
            urgent = job.state in ("rolled_back", "rollback_failed") or fell_back
            if acked(store, key, token):
                return None
            base.state, base.since = "failed", job.finished_at
            detail = clean_text(job.detail, 300)
            regressions = clean_text("; ".join(str(r) for r in job.regressions[:2]), 200)
            if job.state == "rolled_back":
                base.gist = f"made health worse; rolled back to {job.from_version}"
                text = (f"{job.to_version} made health worse ({regressions or detail}), so the daemon rolled it "
                        f"back to {job.from_version}. Health is back to normal. The version stays held until you "
                        "approve it again.")  # fmt: skip
            elif job.state == "rollback_failed":
                base.gist = "escalated: not rolled back"
                backup = _dict((store.update_run(job.run_id) or {}).get("backup"))
                named = f"{backup['name']} ({backup.get('slug')})" if backup.get("name") else "the pre-update backup"
                text = (f"{job.from_version} → {job.to_version} made health worse ({regressions or detail}) and the "
                        f"daemon did not roll it back: {detail}. Automatic updates are paused. Restore {named} under "
                        "Settings > System > Backups (it needs your backup encryption key) if the house is "
                        "affected.")  # fmt: skip
            elif fell_back:
                base.gist = "did not activate after the reboot"
                text = (f"{detail[:1].upper()}{detail[1:]}. Automatic updates are paused and {job.to_version} is held. "
                        "Check Settings > System > Repairs and the host log before you try it again.")  # fmt: skip
            else:
                base.gist = f"not installed: {clean_text(job.detail, 120)}"
                text = f"Not installed: {detail}. Nothing changed on the house."
            secondary = [act("resume_updates", "Resume automatic updates")] if self.ctx.paused else []
            base.need = need("ack", text, act("ack", "OK"), secondary)
            base.source["ack"] = token
            base.urgent = urgent
            return base
        return None

    def _window(self, run: Mapping[str, Any]) -> Spec | None:
        """An update night that backed up or installed (report-only plans are no item: the updates carry them)."""
        if run.get("mode") != "act":
            return None
        state = str(run.get("state"))
        day = str(run.get("window_day") or "")
        title = {"window": f"Update night {day}", "reboot": f"Approved reboot {day}"}.get(
            str(run.get("trigger")), f"Approved update run {day}"
        )
        key = window_key(int(run["id"]))
        spec = Spec(key=key, kind="window", title=title, state="running", kicker="Update window",
                    ref=f"#{run['id']}", source={"run": int(run["id"])}, created=str(run.get("created_at") or ""),
                    as_of=str(run.get("updated_at") or ""))  # fmt: skip
        summary = clean_text(str(run.get("summary") or ""), 300)
        backup = _dict(run.get("backup"))
        if backup.get("name"):
            named = f"{backup['name']} (encrypted, verified)" if backup.get("verified") else str(backup["name"])
            spec.facts.append({"label": "Backup", "value": named, "at": run.get("updated_at")})
        match state:
            case "planned" | "backing_up" | "installing":
                spec.gist = "taking a full backup" if state != "installing" else "installing"
                spec.phase = {"names": ["Backup", "Install", "Verify"], "current": 1 if state == "installing" else 0,
                              "started_at": run.get("created_at"), "usual_s": 1800}  # fmt: skip
            case "done":
                spec.state, spec.gist, spec.quiet = "done", summary or "nothing to install", True
                spec.since = run.get("finished_at") or run.get("updated_at")
            case "aborted":
                spec.state, spec.gist, spec.quiet = "cancelled", summary or "skipped", True
                spec.since = run.get("finished_at") or run.get("updated_at")
                why = self.store.kv_get(f"{ABORT_FAILED_KEY}{run['id']}")
                if why and not acked(self.store, key, f"run:{run['id']}"):
                    # A failure, not a busy night: nothing installs until it is fixed, so the admin hears of it.
                    spec.state, spec.quiet = "failed", False
                    text = (f"Nothing was installed: {clean_text(why, 200)}. Approved and automatic updates wait for "
                            "the next window and fail the same way until this is fixed. Check Settings > System > "
                            "Backups and Repairs.")  # fmt: skip
                    spec.need = need("ack", text, act("ack", "OK"))
                    spec.source["ack"] = f"run:{run['id']}"
            case _:
                spec.state, spec.gist = "failed", summary or "failed"
                spec.since = run.get("finished_at") or run.get("updated_at")
                jobs = self.store.update_jobs(int(run["id"]))
                # A failed job carries its own need; the night needs you only for a regression of the whole night.
                escalated = any(j.state == "rollback_failed" for j in jobs)
                if not escalated and not acked(self.store, key, f"run:{run['id']}"):
                    text = (f"Health is worse than before this night's updates. Automatic updates are paused. "
                            f"{summary}")[:600]  # fmt: skip
                    secondary = [act("resume_updates", "Resume automatic updates")] if self.ctx.paused else []
                    spec.need = need("ack", text, act("ack", "OK"), secondary)
                    spec.source["ack"] = f"run:{run['id']}"
                    spec.urgent = True
        return spec

    # ---------------------------------------------------------------- changes

    def changes(self) -> None:
        live = {a["subject"]: a for a in self.store.approvals(states=LIVE, limit=200) if is_change(a)}
        for row in self.store.changes(limit=30):
            spec = self._change(row, live)
            if spec is not None:
                self.add(spec)

    def _change(self, row: Mapping[str, Any], live: Mapping[str, dict[str, Any]]) -> Spec | None:
        cid = int(row["id"])
        state = str(row["state"])
        if state == "refused":
            return None
        key = change_key(cid)
        files = [f for f in row.get("files") or [] if isinstance(f, dict)]
        spec = Spec(key=key, kind="proposal", title=clean_text(str(row["subject"]), 120), state="waiting",
                    kicker="House fact" if row.get("kind") == "facts" else "Config change", ref=f"#{cid}",
                    created=str(row["created_at"]), as_of=str(row["updated_at"]),
                    source={"change": cid})  # fmt: skip
        spec.change = {"diff": str(row.get("diff") or ""), "files": [str(f.get("path")) for f in files],
                       "shown": [str(f.get("new_sha256")) for f in files]}  # fmt: skip
        why = clean_text(str(row.get("why") or ""), 300)
        effect = clean_text(str(row.get("effect") or ""), 200)
        detail = clean_text(str(row.get("detail") or ""), 200)
        if why or effect:
            # The proposer's (often the agent's) reasons are its words: data under `agent`, never the need.
            spec.agent = {"text": f"{why} {effect}".strip(), "sources": [], "not_read": [],
                          "run_id": row.get("run_id")}  # fmt: skip
        paths = ", ".join(spec.change["files"]) or "no files"
        # A push names the change by number, never by its proposed subject.
        spec.source["push_title"] = f"{spec.kicker} #{cid}"
        approval = live.get(f"change:{cid}:apply") or live.get(f"change:{cid}:revert")
        match state:
            case "open":
                if approval is not None and approval["state"] == "approved":
                    spec.state, spec.gist = "running", "approved; applying"
                    spec.actor, spec.event = "you", "Approved by you"
                    return spec
                text = (f"Change #{cid} to {paths}. Apply it and the daemon checks the config, reloads, commits "
                        "and pushes, and undoes it if anything fails.")  # fmt: skip
                spec.gist = "waiting for your approval"
                spec.need = need("approval", text, act("apply", "Apply and commit"), [act("reject", "Reject", "text")],
                                 expires_at=str(row.get("expires_at") or "") or None)  # fmt: skip
                spec.since = str(row["created_at"])
                if approval is not None:
                    spec.source["approval"] = approval["id"]
            case "applying" | "reverting":
                spec.state, spec.gist = "running", "applying" if state == "applying" else "reverting"
            case "applied":
                spec.state, spec.gist = "done", "applied and committed"
                spec.actions = [act("revert", "Revert")]
                spec.since = str(row.get("decided_at") or row["updated_at"])
            case "reverted":
                spec.state, spec.gist = "done", "reverted"
                spec.since = str(row["updated_at"])
            case "failed":
                token = f"change:{cid}:{row['updated_at']}"
                spec.state, spec.gist, spec.since = "failed", f"failed: {detail}", str(row["updated_at"])
                if not acked(self.store, key, token):
                    spec.need = need("ack", f"The change failed and was not applied: {detail}.", act("ack", "OK"))
                    spec.source["ack"] = token
            case "rejected":
                spec.state, spec.gist, spec.since = "cancelled", "rejected", str(row["updated_at"])
                spec.actor = "you"
            case "stale":
                spec.state, spec.gist = "cancelled", "the files changed since; it no longer applies"
                spec.since = str(row["updated_at"])
            case "expired":
                spec.state, spec.gist, spec.since = "expired", "not applied in time", str(row["updated_at"])
            case _:
                return None
        return spec

    def facts(self) -> None:
        for fact in self.store.list_facts(now=self.now):
            if fact.get("status") != "pending" or fact.get("change_id"):
                continue
            key = f"fact:{fact['id']}"
            text = clean_text(str(fact.get("text") or ""), 300)
            self.add(Spec(key=key, kind="proposal", title="House fact without a proposal", state="waiting",
                          kicker="House fact", gist=text[:120], created=str(fact.get("created_at") or ""),
                          since=str(fact.get("created_at") or ""), source={"fact": int(fact["id"])},
                          need=need("ack", f"Not proposed as a change: {text}", act("dismiss", "Reject"))))  # fmt: skip

    # ------------------------------------------------------------------ tasks

    def tasks(self) -> None:
        store = self.store
        rows = store.list_tasks("upcoming", now=self.now) + store.list_tasks("done", now=self.now)
        for task in rows:
            if task.kind == "retry":
                continue
            investigation = task.kind == "followup" and bool(task.payload.get("investigation"))
            kind = "investigation" if investigation else "reminder"
            kicker = {"reminder": "Reminder", "followup": "Follow-up"}.get(task.kind, task.kind.capitalize())
            if investigation:
                kicker = "Investigation"
            note = task.payload.get("note")
            spec = Spec(key=f"task:{task.id}", kind=kind, title=clean_text(task.title, 120), state="scheduled",
                        kicker=kicker, ref=f"#{task.id}", gist=clean_text(str(note), 120) if isinstance(note, str)
                        and note else ("investigation" if investigation else "reminder"),
                        source={"task": task.id, "thread": task.thread_id}, as_of=task.updated_at)  # fmt: skip
            match task.state:
                case "pending":
                    spec.when = task.next_fire_at
                    spec.actions = [act("stop", "Stop", "danger")] if investigation else [act("cancel", "Cancel",
                                                                                              "text")]  # fmt: skip
                    if investigation:
                        spec.state = "running"
                case "running" | "waiting_approval":
                    spec.state = "running"
                    if investigation:
                        spec.actions = [act("stop", "Stop", "danger")]
                case "done":
                    spec.state, spec.since = "done", task.updated_at
                case "cancelled":
                    spec.state, spec.since = "cancelled", task.updated_at
                case _:
                    spec.state, spec.since = "failed", task.updated_at
            self.add(spec)

    # ----------------------------------------------------------------- health

    def health(self) -> None:
        ctx = self.ctx
        for raw in ctx.health_items:
            key = f"health:{raw.get('key')}"
            severity = str(raw.get("severity") or "info")
            detail = _dict(raw.get("detail"))
            sample = detail.get("sample") if isinstance(detail.get("sample"), str) else ""
            entities = _list(detail.get("entities"))
            line = sample or ", ".join(str(e) for e in entities[:8])
            title = ctx.redact(clean_text(str(raw.get("summary") or raw.get("key")), 120))
            # The sample and the check time are observations (facts): they change between checks without the
            # item changing, so they never move its revision (an action asked for it stays valid).
            gist = severity
            observed = ctx.redact(clean_text(line, 300)) if line else severity
            critical = severity == "critical"
            text = (f"The health check reported this ({severity}). Snooze it for 7 days, or accept it as normal for "
                    "30 days; it is reported again if it changes.")  # fmt: skip
            spec = Spec(key=key, kind="check", title=title, state="waiting", kicker="Health check", gist=gist,
                        since=ctx.health_at, created=ctx.health_at, as_of=ctx.health_at,
                        need=need("ack", text, act("snooze", "Snooze 7 days"),
                                  [act("accept", "Accept as normal", "text")]),
                        source={"health": str(raw.get("key"))},
                        urgent=critical, pushes=False)  # fmt: skip
            spec.facts = [{"label": "Health check", "value": observed, "at": ctx.health_at}]
            after = _parse(detail.get("needs_after"))
            if after is not None and self.now < after:
                # Shown, but it asks only once it lasts (remote access drops for a while often).
                spec.state, spec.need = "proposed", None
                spec.gist = f"{severity}: watching; asks you if it lasts past {after.astimezone(self.tz):%H:%M}"
                spec.actions = [act("snooze", "Snooze 7 days"), act("accept", "Accept as normal", "text")]
            self.add(spec)

    def follows(self) -> None:
        """Health items that an OS update explains follow it, so they never linger as a separate card.

        The Supervisor's `reboot_required` issue follows the OS update that waits for its reboot (done when
        that update is); "host rebooted since last check" follows the OS update whose reboot that boot was.
        """
        updates = [s for s in self.specs.values() if s.kind == "update" and s.source.get("kind") == "os"]
        for spec in list(self.specs.values()):
            if not spec.key.startswith("health:host:reboot:"):
                continue
            boot = spec.key.rsplit(":", 1)[-1]
            for parent in updates:
                job = self.store.update_job(int(parent.source.get("job") or 0)) if parent.source.get("job") else None
                reboot = _dict(job.target.get("reboot")) if job is not None else {}
                if str(reboot.get("boot_after")) != boot:
                    continue
                spec.follows, spec.need = parent.key, None
                if parent.state == "done":
                    spec.state, spec.gist = "done", f"expected: the reboot that activated {parent.title}"
                    spec.since = parent.since
                else:
                    spec.state, spec.gist = "scheduled", f"the reboot for {parent.title}; checked with it"
                break
        self._reboot_required()

    def _reboot_required(self) -> None:
        parents = [s for s in self.specs.values() if s.kind == "update" and s.source.get("kind") == "os"
                   and (s.source.get("reboot") or (s.state == "running" and s.phase and s.phase.get("current") == 2)
                        or s.state == "done")]  # fmt: skip
        for spec in list(self.specs.values()):
            if not spec.key.startswith("health:supervisor:reboot_required"):
                continue
            parent = next((p for p in parents if p.state != "done"), None) or next(iter(parents), None)
            if parent is None:
                continue
            spec.follows = parent.key
            if parent.state == "done":
                spec.state, spec.need, spec.gist = "done", None, f"cleared: {parent.title} is active"
                spec.since = parent.since
            elif parent.state not in TERMINAL:
                spec.state, spec.need = "scheduled", None
                spec.gist = f"clears when {parent.title} is active"


# -------------------------------------------------------------------- the book


Send = Callable[[Message], Awaitable[str]]


class ItemBook:
    def __init__(self, store: Store, hub: Hub, *, tz: Callable[[], tzinfo], context: Callable[[], Context],
                 send: Send | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:  # fmt: skip
        self.store = store
        self.hub = hub
        self._tz = tz
        self._context = context
        self._send = send
        self._clock = clock
        # (item id, the need it was decided for, the push): delivered only if the item still waits for that need.
        self.outbox: list[tuple[str, str, Message]] = []
        # Decided and not yet answered by the channel: never decided twice.
        self._pending: set[tuple[str, str]] = set()
        self._syncing = False
        # A plan made before this start (by an earlier version, or under an earlier rule) may class an update
        # differently from what this version will: until a plan of this process exists, no update item enters or
        # leaves Needs you and none is pushed (an old plan could push a soaking minor as "needs your approval").
        self.started = clock().replace(microsecond=0)
        self.plan_stale = False
        self._held: set[str] = set()

    # ------------------------------------------------------------------- sync

    def sync(self) -> list[dict[str, Any]]:
        """Bring every derived item in line with the daemon's records; returns the items that changed."""
        if self._syncing:
            return []
        self._syncing = True
        try:
            return self._sync()
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("item sync failed", extra={"error": str(err)[:200]})
            return []
        except Exception:
            # A view must never take the daemon down: the records it reads stay the source of truth.
            _LOGGER.exception("item sync crashed")
            return []
        finally:
            self._syncing = False

    def _sync(self) -> list[dict[str, Any]]:
        now = self._clock()
        ctx = self._context()
        first = self.store.kv_get(BACKFILL_KEY) == ""
        deriver = Deriver(self.store, ctx, now, self._tz())
        specs = deriver.run()
        self.plan_stale = deriver.plan_at is not None and deriver.plan_at < self.started
        existing = {r["key"]: r for r in self.store.items_all(derived=True)}
        # Held until a fresh plan: plan-only update items, and update items the stale plan no longer derives.
        self._held = (
            deriver.plan_only | {k for k, r in existing.items() if r["kind"] == "update" and k not in specs}
            if self.plan_stale
            else set()
        )
        changed: list[dict[str, Any]] = []
        work = [(spec, existing.get(key)) for key, spec in specs.items()]
        work += [(self._vanished(old, deriver.plan), old) for key, old in existing.items()
                 if key not in specs and not (old["resolved_at"] and not old["needs"])]  # fmt: skip
        for spec, old in work:
            if spec.key in self._held and in_needs(spec.state, spec.need) != bool(old is not None and old["needs"]):
                continue
            try:
                record = self._apply(spec, old, now)
            except (StoreError, sqlite3.Error) as err:
                # One bad record (a CHECK it breaks) must not stop every other item from updating.
                _LOGGER.error("item not written", extra={"key": spec.key[:120], "error": str(err)[:200]})
                continue
            if record is not None:
                changed.append(record)
        if first:
            # The upgrade: what already waits was pushed by the version before; do not push it again now.
            self.store.kv_set(BACKFILL_KEY, now.isoformat(timespec="seconds"), now=now)
        self._decide_pushes(now, silent=first)
        if changed:
            count = self.store.needs_count()
            for record in changed:
                self.hub.publish("item", None, {"id": record["id"], "revision": record["revision"],
                                                "state": record["state"], "needs_count": count})  # fmt: skip
        return changed

    @staticmethod
    def _material(spec: Mapping[str, Any]) -> tuple[Any, ...]:
        """What an action binds to. Facts and `as_of` are observations: they update silently."""
        return (spec.get("kind"), spec.get("title"), spec.get("state"), spec.get("gist"), spec.get("kicker") or "",
                spec.get("ref") or "", spec.get("need"), spec.get("about"), spec.get("actions") or [],
                spec.get("phase"), spec.get("follows"), spec.get("agent"), spec.get("change"),
                spec.get("footnote"), bool(spec.get("quiet")))  # fmt: skip

    def _apply(self, spec: Spec, old: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
        fresh = {"kind": spec.kind, "title": spec.title, "state": spec.state, "gist": spec.gist,
                 "kicker": spec.kicker, "ref": spec.ref, "need": spec.need, "about": spec.about,
                 "actions": spec.actions, "phase": spec.phase, "follows": spec.follows, "facts": spec.facts,
                 "agent": spec.agent, "change": spec.change, "footnote": spec.footnote,
                 "quiet": spec.quiet}  # fmt: skip
        source = spec.source | {"urgent": spec.urgent, "pushes": spec.pushes}
        stamp = _iso(now)
        as_of = spec.as_of or stamp
        if old is not None and self._material(old) == self._material(fresh):
            silent: dict[str, Any] = {}
            if as_of != old["as_of"]:
                silent["as_of"] = as_of
            if source != old["source"] or spec.facts != old["facts"]:
                # Bindings (an approval id) and observations (facts) are not a change an action binds to.
                self.store.put_item(old | {"source": source, "facts": spec.facts})
            if silent:
                self.store.set_item_fields(old["id"], **silent)
            return None
        needs = in_needs(spec.state, spec.need)
        terminal = spec.state in TERMINAL
        revision = int(old["revision"]) + 1 if old else 1
        # A new record takes its source's own times (the backfill); a change in this process is stamped now.
        needs_since: str | None = None
        if needs:
            if old is not None and old["needs"] and old["needs_since"]:
                needs_since = old["needs_since"]
            else:
                needs_since = (spec.since or stamp) if old is None else stamp
        resolved_at: str | None = None
        if terminal:
            if old is not None and old["state"] == spec.state and old["resolved_at"]:
                resolved_at = old["resolved_at"]
            else:
                resolved_at = (spec.since or stamp) if old is None else stamp
        if needs:
            at = needs_since or stamp
        elif terminal:
            at = resolved_at or stamp
        elif spec.when:
            at = spec.when
        elif spec.phase and spec.phase.get("started_at"):
            at = str(spec.phase["started_at"])
        else:
            at = stamp
        ident = old["id"] if old else item_id(spec.key)
        record = fresh | {
            "id": ident, "key": spec.key, "revision": revision, "as_of": as_of, "at": at,
            "needs_since": needs_since, "resolved_at": resolved_at,
            "created_at": old["created_at"] if old else (spec.created or stamp), "updated_at": stamp,
            "source": source, "confirm": None, "derived": True,
            # Leaving Needs you forgets the push, so coming back (a skipped approved job) pushes again.
            "push": old["push"] if old and needs else {},
        }  # fmt: skip
        self.store.put_item(record)
        text = spec.event or (f"{WORD[spec.state]}: {spec.gist}" if spec.gist else WORD[spec.state])
        event_at = stamp if old else (spec.since or spec.created or stamp)
        self.store.add_item_event(ident, at=event_at, actor=spec.actor if spec.actor in ACTORS else "daemon",
                                  text=text, revision=revision)  # fmt: skip
        record["needs"] = needs
        return record

    @staticmethod
    def _vanished(old: Mapping[str, Any], plan: Mapping[str, Mapping[str, Any]] | None = None) -> Spec:
        """Its source no longer reports it: resolved, never left waiting. The words say the real reason; an update
        is never called installed here (that is the job's own item, from the version it confirmed)."""
        state, gist = "cancelled", "no longer pending"
        key = str(old["key"])
        prefix = key.split(":", 1)[0]
        if prefix == "health":
            state, gist = "done", "cleared: not reported by the latest health check"
        elif prefix == "updates":
            entity, _, version = key.removeprefix("updates:").rpartition("@")
            offered = (plan or {}).get(entity)
            if offered is not None and same_version(offered.get("latest"), version):
                gist = f"not offered now: {daemon_reason(offered)}"
            elif offered is not None:
                gist = f"superseded: Home Assistant now offers {clean_text(str(offered.get('latest')), 40)}"
            else:
                gist = "Home Assistant no longer lists it as pending"
        return Spec(key=str(old["key"]), kind=str(old["kind"]), title=str(old["title"]), state=state, gist=gist,
                    kicker=str(old.get("kicker") or ""), ref=str(old.get("ref") or ""), follows=old.get("follows"),
                    source=dict(old.get("source") or {}), pushes=False)  # fmt: skip

    # ------------------------------------------------------------------ pushes

    @staticmethod
    def _push_key(record: Mapping[str, Any]) -> str:
        """The need a push is for: a new need pushes again, the same one only re-surfaces."""
        current = record.get("need") or {}
        return f"{record['state']}:{current.get('kind')}:{(current.get('primary') or {}).get('id', '')}"

    def _decide_pushes(self, now: datetime, *, silent: bool) -> None:
        """Queue a push for each item that entered Needs you (or waited a day). It is recorded as pushed only
        once the channel delivered it (`deliver`), so a restart or a failure never loses it. An update
        item held for a fresh plan (`plan_stale`) waits for it."""
        for record in self.store.items_all(open_only=True):
            if not record["needs"] or record["key"] in self._held:
                continue
            source = record.get("source") or {}
            current = record.get("need") or {}
            key = self._push_key(record)
            ident = str(record["id"])
            sent = record.get("push") or {}
            last = _parse(sent.get("at"))
            failed = _parse(sent.get("failed_at")) if sent.get("key") == key else None
            fresh = sent.get("key") != key or last is None or now - last >= RESURFACE
            due = fresh and (failed is None or now - failed >= RETRY)
            expires = _parse(current.get("expires_at"))
            if not due or (expires is not None and expires <= now) or (ident, key) in self._pending:
                continue
            if silent or not source.get("pushes", True):
                # Nothing goes out for it (the upgrade backfill, the health check's own items): decided once.
                self.store.set_item_fields(ident, push={"key": key, "at": _iso(now)})
                continue
            self._pending.add((ident, key))
            self.outbox.append((ident, key, self.message(record)))

    @staticmethod
    def message(record: Mapping[str, Any]) -> Message:
        """The push for an item in Needs you: every word from the record, tag = item id, opens the item."""
        current = record.get("need") or {}
        urgent = bool((record.get("source") or {}).get("urgent"))
        word = WORD[str(record["state"])]
        label = (current.get("primary") or {}).get("label") or "Open"
        body = f"{word}: {record['gist']}." if record.get("gist") else f"{word}."
        if urgent and current.get("text"):
            body += f" {current['text']}"
        else:
            body += f" Open it to {label[:1].lower()}{label[1:]}." if label != "OK" else " Open it to acknowledge."
        if len(body) > PUSH_BODY:
            body = body[: PUSH_BODY - 1].rstrip() + "…"
        title = str((record.get("source") or {}).get("push_title") or record["title"])
        return Message(title[:70], body, urgent=urgent, tag=str(record["id"]),
                       budget_class=BudgetClass.SAFETY if urgent else BudgetClass.AGENT,
                       url=deep_link(str(record["id"])))  # fmt: skip

    async def deliver(self) -> list[str]:
        """Send what `sync` decided (quiet hours and caps apply in the daemon's send), then record the outcome."""
        outcomes = []
        queued, self.outbox = self.outbox, []
        latest = {ident: index for index, (ident, _, _) in enumerate(queued)}
        for index, (ident, key, message) in enumerate(queued):
            try:
                current = self.store.item(ident)
                # Superseded by a later decision, or resolved (or changed) before it went out: a stale push is
                # worse than none.
                if latest[ident] != index or self._send is None or current is None or not current["needs"]:
                    continue
                if self._push_key(current) != key:
                    continue
                try:
                    outcome = await self._send(message)
                except Exception:
                    _LOGGER.exception("item push failed", extra={"tag": message.tag})
                    outcome = "failed"
                outcomes.append(outcome)
                stamp = _iso(self._clock())
                record = {"key": key, "at": stamp} if outcome in DELIVERED else {"key": key, "failed_at": stamp}
                self.store.set_item_fields(ident, push=record)
            finally:
                self._pending.discard((ident, key))
        return outcomes

    async def sync_and_deliver(self) -> list[dict[str, Any]]:
        changed = self.sync()
        await self.deliver()
        return changed

    # ------------------------------------------------------------------- reads

    def get(self, ident: str) -> dict[str, Any] | None:
        if not ITEM_ID_RE.fullmatch(ident):
            return None
        return self.store.item(ident)

    def offered(self, record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Every action the item offers now: the need's primary and secondary, and its other actions."""
        found: dict[str, dict[str, Any]] = {}
        current = record.get("need") or {}
        for candidate in [current.get("primary"), *(current.get("secondary") or []), *(record.get("actions") or [])]:
            if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                found[candidate["id"]] = candidate
        return found

    def changed_since(self, record: Mapping[str, Any], revision: int) -> str:
        """What changed after `revision`, in the timeline's words (for a 409)."""
        newer = [e for e in self.store.item_events(str(record["id"])) if int(e["revision"]) > revision]
        return newer[-1]["text"] if newer else f"{WORD[str(record['state'])]}: {record.get('gist')}"

    def ack(self, record: Mapping[str, Any], *, now: datetime) -> bool:
        """The admin acknowledged a failure: a derived one by its source token, a standalone one directly."""
        source = record.get("source") or {}
        if record.get("derived"):
            token = source.get("ack")
            if not isinstance(token, str):
                return False
            self.store.kv_set(f"{ACK_KEY}{record['key']}", token, now=now)
            return True
        if record.get("need") is None or record.get("state") not in ("failed", "waiting"):
            return False
        state = "failed" if record["state"] == "failed" else "done"
        resolved = record.get("resolved_at") or _iso(now)
        updated = dict(record) | {"state": state, "need": None, "revision": int(record["revision"]) + 1,
                                  "updated_at": _iso(now), "resolved_at": resolved, "at": resolved,
                                  "needs_since": None}  # fmt: skip
        self.store.put_item(updated)
        self.store.add_item_event(str(record["id"]), at=now, actor="you", text="Acknowledged by you",
                                  revision=int(updated["revision"]))  # fmt: skip
        self.hub.publish("item", None, {"id": record["id"], "revision": updated["revision"], "state": state,
                                        "needs_count": self.store.needs_count()})  # fmt: skip
        return True

    def view(self, record: Mapping[str, Any], *, events: bool = False) -> dict[str, Any]:
        """The contract's Item (`docs/panel-api.md`)."""
        out: dict[str, Any] = {k: record.get(k) for k in (
            "id", "key", "kind", "title", "state", "gist", "at", "quiet", "revision", "as_of", "needs_since",
            "resolved_at", "created_at", "updated_at", "need", "about", "actions", "phase", "facts", "agent",
            "change", "footnote")}  # fmt: skip
        if record.get("kicker"):
            out["kicker"] = record["kicker"]
        if record.get("ref"):
            out["ref"] = record["ref"]
        out["quiet"] = bool(record.get("quiet"))
        out["actions"] = list(record.get("actions") or [])
        follows = record.get("follows")
        parent = self.store.item_by_key(str(follows)) if follows else None
        out["follows"] = {"id": parent["id"], "title": parent["title"], "state": parent["state"]} if parent else None
        if events:
            out["events"] = self.store.item_events(str(record["id"]))
        return out

    # -------------------------------------------------------------------- Home

    def home(self) -> dict[str, list[dict[str, Any]]]:
        """Needs you (oldest first), In progress and Coming up (by `at`), Today (resolved today, newest first)."""
        tz = self._tz()
        local = self._clock().astimezone(tz)
        midnight = datetime.combine(local.date(), time(0, 0), tz).astimezone(UTC)
        needs, running, coming, today = [], [], [], []
        for record in self.store.items_since(midnight):
            if record["needs"]:
                needs.append(record)
            elif record["state"] == "running":
                running.append(record)
            elif record["state"] in ("proposed", "scheduled"):
                coming.append(record)
            elif record["state"] in TERMINAL and not record["quiet"]:
                resolved = _parse(record.get("resolved_at"))
                if resolved is not None and resolved >= midnight:
                    today.append(record)
        needs.sort(key=lambda r: (str(r.get("needs_since") or r["at"]), r["id"]))
        running.sort(key=lambda r: (str(r["at"]), r["id"]))
        coming.sort(key=lambda r: (str(r["at"]), r["id"]))
        today.sort(key=lambda r: (str(r["at"]), r["id"]), reverse=True)
        return {"needs": needs, "running": running, "coming": coming, "today": today}
