"""Approvals for APPROVE-tier updates.

An approval is a record bound to one exact planned action: `plan_hash` is
the SHA-256 of the plan item as the admin was shown it (entity, from and to
versions, tier, reason, the release-note review, any hold), the plan (update
run) id and the approval's creation time. It is recomputed from the plan
item stored in that update run (a separate row) whenever the approval is
confirmed, consumed or executed, so editing the approval's own copy, its
plan id or its hash voids it; the live update entity must also still show
the same versions. It needs:

the admin's one request from the ingress panel (Supervisor ingress admin
header and the panel's CSRF token, for the item revision the admin saw); a
push only ever opens the panel. There is no phone confirmation: its action
id would cross Core's event bus, so it would add no second factor. The
request records the approval `pending` and approves it in the same step
(`confirm`), re-checked against the live update. Before it is used, the action is
recomputed from the live update entity: a different version (or the update
gone) voids it. It is used once (`approved` -> `used`, bound to the job that
consumed it), expires after 72 hours, and runs in the next update window or,
if the admin chose "now", at once; either way only through the normal
pipeline (backup, health diff, rollback or escalation), in `act` mode with
`updates.auto_install: on` and not paused. "Reject" rejects it; nothing
else changes. Rejecting is always safe, so it is one step.

Config change proposals reuse the same records, steps, single use and 72-hour expiry
for config changes (`kind: change` in the plan, subject `change:<id>:apply`
or `change:<id>:revert`). Their `plan_hash` binds the proposal id, its commit
and base commits, each file's old and new SHA-256 (and, for a revert, the
commit that applied it) and the proposal's creation time. The binding is
recomputed from git objects and the proposal ref when the tap arrives and
again right before the change is written, so editing the proposal row, the
ref or the approval breaks it. Update code paths never accept a change
approval, and the reverse.

The same binding covers the reboot of an installed HAOS update that waits for one
(`kind: reboot`, subject `reboot:<entity>@<version>`): the same request,
single use and expiry, bound to the job (its id, entity,
versions, state and finish time). The daemon uses it in the next update
window only, and only while the job still waits for that reboot; the
executor still requires the job's own update approval for the reboot call.

A dashboard migration plan (`kind: dashboard`, subject `dashboard:<finding>`, `opsd.dashboards`) uses
the same request, single use and expiry. Its `plan_hash` binds the finding, the dashboard's url_path,
the SHA-256 of its JSON before and after the edit, and the edit itself; it is recomputed from the
dashboard as Home Assistant serves it when the tap arrives and again right before the save, so a
dashboard that changed in between voids it. Update, change and reboot paths never accept it, and the
reverse.
"""

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from opsd.policy import Tier, same_version
from opsd.store import Store, StoreError, UpdateJob

_LOGGER = logging.getLogger(__name__)

APPROVAL_TTL = timedelta(hours=72)
# What the agent can install once approved. The Supervisor and the agent itself never.
APPROVABLE_KINDS = frozenset({"core", "addon", "hacs", "os", "firmware"})
WHEN = ("window", "now")
LIVE = ("pending", "approved")
MAX_PENDING = 20


class ApprovalError(ValueError):
    """The approval cannot be requested or confirmed; the message is safe to show the admin."""


# The parts of a plan item an approval is about: what is installed, why it needs approval,
# and what the release-note review said when the admin looked at it.
HASHED_FIELDS = ("entity_id", "kind", "title", "installed", "latest", "tier", "bump", "reason", "review", "held")


def plan_hash(item: Mapping[str, Any], plan_run: int, created_at: str) -> str:
    """SHA-256 of the plan item as shown, the plan (update run) id and the approval's creation time."""
    canonical = json.dumps(
        {"item": {k: item.get(k) for k in HASHED_FIELDS}, "plan": plan_run, "created_at": created_at},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def change_hash(binding: Mapping[str, Any], created_at: str) -> str:
    """SHA-256 of a config change's binding (see the module docstring) and the approval's creation time."""
    canonical = json.dumps({"change": dict(binding), "created_at": created_at}, sort_keys=True,
                           separators=(",", ":"), default=str)  # fmt: skip
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_change(found: Mapping[str, Any]) -> bool:
    return (found.get("plan") or {}).get("kind") == "change"


def is_reboot(found: Mapping[str, Any]) -> bool:
    return (found.get("plan") or {}).get("kind") == "reboot"


def is_dashboard(found: Mapping[str, Any]) -> bool:
    return (found.get("plan") or {}).get("kind") == "dashboard"


def is_update(found: Mapping[str, Any]) -> bool:
    """An update approval: none of the other kinds that share the records."""
    return not (is_change(found) or is_reboot(found) or is_dashboard(found))


def dashboard_subject(finding: str) -> str:
    return f"dashboard:{finding}"


def dashboard_hash(binding: Mapping[str, Any], created_at: str) -> str:
    canonical = json.dumps({"dashboard": dict(binding), "created_at": created_at}, sort_keys=True,
                           separators=(",", ":"), default=str)  # fmt: skip
    return hashlib.sha256(canonical.encode()).hexdigest()


def reboot_subject(entity_id: str, version: str) -> str:
    return f"reboot:{entity_id}@{version}"


def reboot_binding(job: UpdateJob) -> dict[str, Any]:
    """What a reboot approval is about: this job, waiting for its reboot since it finished."""
    return {"job": job.id, "entity_id": job.entity_id, "from": job.from_version, "to": job.to_version,
            "state": job.state, "finished_at": job.finished_at}  # fmt: skip


def reboot_hash(binding: Mapping[str, Any], created_at: str) -> str:
    canonical = json.dumps({"reboot": dict(binding), "created_at": created_at}, sort_keys=True,
                           separators=(",", ":"), default=str)  # fmt: skip
    return hashlib.sha256(canonical.encode()).hexdigest()


def approvable(item: Mapping[str, Any]) -> bool:
    """APPROVE-tier, a kind the agent can install, with both versions known."""
    return (
        item.get("tier") == Tier.APPROVE
        and item.get("kind") in APPROVABLE_KINDS
        and bool(item.get("installed"))
        and bool(item.get("latest"))
    )


def subject(entity_id: str, version: str) -> str:
    return f"{entity_id}@{version}"


class Approvals:
    def __init__(self, store: Store, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._store = store
        self._clock = clock

    def _audit(self, what: str, by: str, approval: Mapping[str, Any], detail: str = "") -> None:
        try:
            self._store.audit(at=self._clock(), run_id=None, principal=by, channel="approvals",
                              capability=f"approval.{what}", args={"id": approval.get("id"),
                                                                   "subject": approval.get("subject")},
                              tier="approve", decision=what, outcome=what, detail=detail[:300])  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    # ------------------------------------------------------------- request

    def request(self, item: Mapping[str, Any], *, plan_run: int, user: str, when: str) -> dict[str, Any]:
        """A pending approval for this exact item of this plan (`confirm` approves it)."""
        if when not in WHEN:
            raise ApprovalError("when must be window or now")
        if not approvable(item):
            raise ApprovalError("this update cannot be approved")
        if len(self._store.approvals(states=("pending",), limit=MAX_PENDING + 1)) >= MAX_PENDING:
            raise ApprovalError("too many approvals waiting for confirmation")
        now = self._clock()
        entity, frm, to = str(item["entity_id"]), str(item["installed"]), str(item["latest"])
        # A new request supersedes any earlier live one for the same update.
        for old in self._store.approvals(states=LIVE, limit=200):
            if old["plan"].get("entity_id") == entity:
                self._store.decide_approval(old["id"], expect=LIVE, to="void", now=now, detail="superseded")
        plan = {"entity_id": entity, "from": frm, "to": to, "plan": plan_run, "kind": item.get("kind"),
                "title": str(item.get("title") or entity)[:120], "tier": item.get("tier"),
                "reason": str(item.get("reason") or "")[:200]}  # fmt: skip
        approval_id = secrets.token_hex(16)
        created = now.astimezone(UTC).isoformat(timespec="seconds")
        self._store.create_approval(approval_id=approval_id, plan=plan, plan_hash=plan_hash(item, plan_run, created),
                                    subject=subject(entity, to), requested_by=user, run_when=when, now=now,
                                    expires_at=now + APPROVAL_TTL)  # fmt: skip
        found = self._store.approval(approval_id)
        if found is None:
            raise ApprovalError("the approval could not be saved")
        self._audit("requested", f"panel:{user[:64]}", found)
        return found

    def confirm(self, approval_id: str, *, user: str, current: Mapping[str, Any] | None) -> str:
        """The admin's approval from the panel. `current`: the live update entity. Returns the new state."""
        now = self._clock()
        found = self._store.approval(approval_id)
        if found is None:
            return "unknown"
        by = f"user:{user[:64]}"
        if not is_update(found):
            self._audit("refused", by, found, "not an update approval")
            return "refused"
        if found["state"] != "pending":
            self._audit("refused", by, found, f"already {found['state']}")
            return "refused"
        if not user or not hmac.compare_digest(user.encode(), str(found["requested_by"]).encode()):
            self._audit("refused", by, found, "not the admin who asked")
            return "refused"
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("pending",), to="expired", now=now)
            return "expired"
        mismatch = self._mismatch(found, current)
        if mismatch is not None:
            self._store.decide_approval(approval_id, expect=("pending",), to="void", now=now, detail=mismatch)
            self._audit("void", by, found, mismatch)
            return "void"
        if not self._store.decide_approval(approval_id, expect=("pending",), to="approved", now=now, by=user):
            return "refused"
        self._audit("approved", by, found)
        return "approved"

    def reject_update(self, item: Mapping[str, Any], *, plan_run: int, user: str) -> dict[str, Any]:
        """Reject an update nobody approved yet: a rejected record, so the agent does not ask again this week."""
        found = self.request(item, plan_run=plan_run, user=user, when="window")
        self._store.decide_approval(found["id"], expect=("pending",), to="rejected", now=self._clock(), by=user,
                                    detail="rejected")  # fmt: skip
        self._audit("rejected", f"panel:{user[:64]}", found)
        return found

    # ------------------------------------------------------------ the plan

    def bound(self, found: Mapping[str, Any]) -> bool:
        """The approval still describes the plan it was given for.

        The hash is recomputed from the plan item as stored in its update run (a separate
        row the approval cannot change) and the approval's creation time; editing the
        approval's own copy of the plan, its plan id or its hash breaks it.
        """
        plan = found.get("plan") or {}
        if not is_update(found):
            return False
        run = self._store.update_run(int(plan.get("plan") or 0))
        source = next((i for i in (run or {}).get("plan", []) if i.get("entity_id") == plan.get("entity_id")), None)
        if source is None:
            return False
        if not same_version(source.get("installed"), plan.get("from")) or not same_version(
            source.get("latest"), plan.get("to")
        ):
            return False
        recomputed = plan_hash(source, int(plan["plan"]), str(found["created_at"]))
        return hmac.compare_digest(recomputed, str(found["plan_hash"]))

    def _mismatch(self, found: Mapping[str, Any], current: Mapping[str, Any] | None) -> str | None:
        """None when it is bound to its plan and the live update is still exactly that action; else why not."""
        if current is None:
            return "Home Assistant no longer lists this update"
        if not self.bound(found):
            return "the update plan it was given for changed"
        plan = found["plan"]
        attrs = current.get("attributes") or {}
        installed, latest = attrs.get("installed_version"), attrs.get("latest_version")
        if current.get("state") != "on" or not isinstance(installed, str) or not isinstance(latest, str):
            return "Home Assistant no longer offers this update (installed, skipped or waiting for a reboot)"
        if not same_version(installed, plan.get("from")) or not same_version(latest, plan.get("to")):
            return f"Home Assistant now offers {installed} → {latest} instead"
        return None

    def expire(self) -> None:
        now = self._clock()
        for found in self._store.approvals(states=LIVE, limit=200):
            if now >= datetime.fromisoformat(found["expires_at"]):
                self._store.decide_approval(found["id"], expect=LIVE, to="expired", now=now)

    def annotate(
        self, plan: list[dict[str, Any]], *, now_only: bool = False, void: bool = False
    ) -> list[dict[str, Any]]:
        """Mark plan items with their approval.

        `void`: only the nightly plan passes it. Then an approval whose update changed or is no longer
        offered is voided, with the reason in plain words. Otherwise approvals are only read: one that
        does not match this plan is simply not marked (and so not run) and stays as it is.
        """
        self.expire()
        now = self._clock()
        by_entity = {str(i.get("entity_id")): i for i in plan}
        live = [a for a in self._store.approvals(states=LIVE, limit=200) if is_update(a)]
        result_marks: dict[str, dict[str, Any]] = {}
        for found in live:
            plan_item = found["plan"]
            item = by_entity.get(str(plan_item.get("entity_id")))
            reason = self._plan_mismatch(found, item)
            if reason is not None:
                if void:
                    self._store.decide_approval(found["id"], expect=LIVE, to="void", now=now, detail=reason)
                    self._audit("void", "daemon", found, reason)
                continue
            result_marks.setdefault(str(plan_item["entity_id"]), found)
        out = []
        for item in plan:
            mark_of = result_marks.get(str(item.get("entity_id")))
            if mark_of is None:
                out.append(item)
                continue
            mark = {"approval": mark_of["id"], "approval_state": mark_of["state"], "run_when": mark_of["run_when"]}
            ready = (
                mark_of["state"] == "approved" and approvable(item) and (not now_only or mark_of["run_when"] == "now")
            )
            out.append(item | mark | {"approved": ready})
        return out

    def _plan_mismatch(self, found: Mapping[str, Any], item: Mapping[str, Any] | None) -> str | None:
        """Why this approval does not fit the plan item for its update (plain words), or None."""
        plan_item = found["plan"]
        if item is None:
            return "Home Assistant did not offer this update at the nightly plan (installed, skipped or gone)"
        if not same_version(item.get("installed"), plan_item.get("from")) or not same_version(
            item.get("latest"), plan_item.get("to")
        ):
            return f"Home Assistant now offers {item.get('installed')} → {item.get('latest')} instead"
        if not self.bound(found):
            return "the update plan it was given for changed"
        return None

    def consume(self, approval_id: str, job_id: int) -> bool:
        return self._store.decide_approval(approval_id, expect=("approved",), to="used", now=self._clock(), job=job_id)

    def proves(self, approval_id: str | None, job: UpdateJob) -> bool:
        """The executor's check: this job consumed this approval, for exactly this action."""
        if not approval_id:
            return False
        found = self._store.approval(approval_id)
        if found is None or found["state"] != "used" or found["used_by_job"] != job.id or not found["decided_by"]:
            return False
        if not is_update(found):
            return False
        if not self.bound(found):
            return False
        plan = found["plan"]
        return (
            plan.get("entity_id") == job.entity_id
            and same_version(plan.get("from"), job.from_version)
            and same_version(plan.get("to"), job.to_version)
        )

    # ------------------------------------------------------ config changes

    def request_change(self, binding: Mapping[str, Any], *, action: str, title: str, user: str) -> dict[str, Any]:
        """A pending approval for applying or reverting one change proposal (`confirm_change` approves it)."""
        if action not in ("apply", "revert"):
            raise ApprovalError("action must be apply or revert")
        if len(self._store.approvals(states=("pending",), limit=MAX_PENDING + 1)) >= MAX_PENDING:
            raise ApprovalError("too many approvals waiting for confirmation")
        now = self._clock()
        target = f"change:{binding['proposal']}:{action}"
        for old in self._store.approvals(states=LIVE, limit=200):
            if old["subject"] == target:
                self._store.decide_approval(old["id"], expect=LIVE, to="void", now=now, detail="superseded")
        approval_id = secrets.token_hex(16)
        created = now.astimezone(UTC).isoformat(timespec="seconds")
        plan = {"kind": "change", "action": action, "binding": dict(binding), "title": title[:120]}
        self._store.create_approval(approval_id=approval_id, plan=plan, plan_hash=change_hash(binding, created),
                                    subject=target, requested_by=user, run_when="now", now=now,
                                    expires_at=now + APPROVAL_TTL)  # fmt: skip
        found = self._store.approval(approval_id)
        if found is None:
            raise ApprovalError("the approval could not be saved")
        self._audit("requested", f"panel:{user[:64]}", found)
        return found

    @staticmethod
    def change_bound(found: Mapping[str, Any], binding_now: Mapping[str, Any] | None) -> bool:
        """The approval's hash still matches the binding recomputed from git and the proposal now."""
        if not is_change(found) or binding_now is None:
            return False
        recomputed = change_hash(binding_now, str(found["created_at"]))
        return hmac.compare_digest(recomputed, str(found["plan_hash"]))

    def confirm_change(self, approval_id: str, *, user: str, binding_now: Mapping[str, Any] | None) -> str:
        """The admin's approval of a change: `approved`, `refused`, `expired`, `void` or `unknown`."""
        now = self._clock()
        found = self._store.approval(approval_id)
        if found is None or not is_change(found):
            return "unknown"
        by = f"user:{user[:64]}"
        if found["state"] != "pending":
            self._audit("refused", by, found, f"already {found['state']}")
            return "refused"
        if not user or not hmac.compare_digest(user.encode(), str(found["requested_by"]).encode()):
            self._audit("refused", by, found, "not the admin who asked")
            return "refused"
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("pending",), to="expired", now=now)
            return "expired"
        if not self.change_bound(found, binding_now):
            self._store.decide_approval(approval_id, expect=("pending",), to="void", now=now,
                                        detail="the change or its base moved before it was confirmed")  # fmt: skip
            self._audit("void", by, found, "changed before confirmation")
            return "void"
        if not self._store.decide_approval(approval_id, expect=("pending",), to="approved", now=now, by=user):
            return "refused"
        self._audit("approved", by, found)
        return "approved"

    def consume_change(self, approval_id: str, proposal_id: int, binding_now: Mapping[str, Any] | None) -> bool:
        """Single use, right before the change is written: approved, unexpired, still bound."""
        found = self._store.approval(approval_id)
        now = self._clock()
        if found is None or not is_change(found) or found["state"] != "approved" or not found["decided_by"]:
            return False
        if int((found["plan"].get("binding") or {}).get("proposal") or 0) != proposal_id:
            return False
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("approved",), to="expired", now=now)
            return False
        if not self.change_bound(found, binding_now):
            self._store.decide_approval(approval_id, expect=("approved",), to="void", now=now,
                                        detail="the change or its base moved")  # fmt: skip
            return False
        return self._store.decide_approval(approval_id, expect=("approved",), to="used", now=now, job=proposal_id)

    # ------------------------------------------------- reboot of an OS update

    def request_reboot(self, job: UpdateJob, *, user: str) -> dict[str, Any]:
        """A pending approval to reboot the host for this installed OS job in the next update window."""
        if len(self._store.approvals(states=("pending",), limit=MAX_PENDING + 1)) >= MAX_PENDING:
            raise ApprovalError("too many approvals waiting for confirmation")
        now = self._clock()
        target = reboot_subject(job.entity_id, job.to_version)
        for old in self._store.approvals(states=LIVE, limit=200):
            if old["subject"] == target:
                self._store.decide_approval(old["id"], expect=LIVE, to="void", now=now, detail="superseded")
        binding = reboot_binding(job)
        approval_id = secrets.token_hex(16)
        created = now.astimezone(UTC).isoformat(timespec="seconds")
        plan = {"kind": "reboot", "binding": binding,
                "title": f"Reboot to activate {job.title} {job.to_version}"[:120]}  # fmt: skip
        self._store.create_approval(approval_id=approval_id, plan=plan, plan_hash=reboot_hash(binding, created),
                                    subject=target, requested_by=user, run_when="window", now=now,
                                    expires_at=now + APPROVAL_TTL)  # fmt: skip
        found = self._store.approval(approval_id)
        if found is None:
            raise ApprovalError("the approval could not be saved")
        self._audit("requested", f"panel:{user[:64]}", found)
        return found

    @staticmethod
    def reboot_bound(found: Mapping[str, Any], job: UpdateJob | None) -> bool:
        """The approval's hash still matches the job as it is now (same job, versions, state and finish time)."""
        if not is_reboot(found) or job is None:
            return False
        recomputed = reboot_hash(reboot_binding(job), str(found["created_at"]))
        return hmac.compare_digest(recomputed, str(found["plan_hash"]))

    def confirm_reboot(self, approval_id: str, *, user: str, job: UpdateJob | None) -> str:
        """The admin's approval of a reboot: `approved`, `refused`, `expired`, `void` or `unknown`."""
        now = self._clock()
        found = self._store.approval(approval_id)
        if found is None or not is_reboot(found):
            return "unknown"
        by = f"user:{user[:64]}"
        if found["state"] != "pending":
            self._audit("refused", by, found, f"already {found['state']}")
            return "refused"
        if not user or not hmac.compare_digest(user.encode(), str(found["requested_by"]).encode()):
            self._audit("refused", by, found, "not the admin who asked")
            return "refused"
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("pending",), to="expired", now=now)
            return "expired"
        if not self.reboot_bound(found, job):
            self._store.decide_approval(approval_id, expect=("pending",), to="void", now=now,
                                        detail="the update changed before it was confirmed")  # fmt: skip
            self._audit("void", by, found, "changed before confirmation")
            return "void"
        if not self._store.decide_approval(approval_id, expect=("pending",), to="approved", now=now, by=user):
            return "refused"
        self._audit("approved", by, found)
        return "approved"

    def consume_reboot(self, approval_id: str, job: UpdateJob) -> bool:
        """Single use, right before the reboot step starts: approved, unexpired, still bound to this job."""
        found = self._store.approval(approval_id)
        now = self._clock()
        if found is None or not is_reboot(found) or found["state"] != "approved" or not found["decided_by"]:
            return False
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("approved",), to="expired", now=now)
            return False
        if not self.reboot_bound(found, job):
            self._store.decide_approval(approval_id, expect=("approved",), to="void", now=now,
                                        detail="the update changed since the reboot was approved")  # fmt: skip
            return False
        return self._store.decide_approval(approval_id, expect=("approved",), to="used", now=now, job=job.id)

    # ------------------------------------------------ dashboard migrations

    def request_dashboard(self, binding: Mapping[str, Any], *, title: str, user: str) -> dict[str, Any]:
        """A pending approval for one dashboard migration plan (`confirm_dashboard` approves it)."""
        if len(self._store.approvals(states=("pending",), limit=MAX_PENDING + 1)) >= MAX_PENDING:
            raise ApprovalError("too many approvals waiting for confirmation")
        now = self._clock()
        target = dashboard_subject(str(binding["finding"]))
        for old in self._store.approvals(states=LIVE, limit=200):
            if old["subject"] == target:
                self._store.decide_approval(old["id"], expect=LIVE, to="void", now=now, detail="superseded")
        approval_id = secrets.token_hex(16)
        created = now.astimezone(UTC).isoformat(timespec="seconds")
        plan = {"kind": "dashboard", "binding": dict(binding), "title": title[:120]}
        self._store.create_approval(approval_id=approval_id, plan=plan, plan_hash=dashboard_hash(binding, created),
                                    subject=target, requested_by=user, run_when="now", now=now,
                                    expires_at=now + APPROVAL_TTL)  # fmt: skip
        found = self._store.approval(approval_id)
        if found is None:
            raise ApprovalError("the approval could not be saved")
        self._audit("requested", f"panel:{user[:64]}", found)
        return found

    @staticmethod
    def dashboard_bound(found: Mapping[str, Any], binding_now: Mapping[str, Any] | None) -> bool:
        """The approval's hash still matches the binding recomputed from the dashboard as it is now."""
        if not is_dashboard(found) or binding_now is None:
            return False
        recomputed = dashboard_hash(binding_now, str(found["created_at"]))
        return hmac.compare_digest(recomputed, str(found["plan_hash"]))

    def confirm_dashboard(self, approval_id: str, *, user: str, binding_now: Mapping[str, Any] | None) -> str:
        """The admin's approval of a dashboard plan: `approved`, `refused`, `expired`, `void` or `unknown`."""
        now = self._clock()
        found = self._store.approval(approval_id)
        if found is None or not is_dashboard(found):
            return "unknown"
        by = f"user:{user[:64]}"
        if found["state"] != "pending":
            self._audit("refused", by, found, f"already {found['state']}")
            return "refused"
        if not user or not hmac.compare_digest(user.encode(), str(found["requested_by"]).encode()):
            self._audit("refused", by, found, "not the admin who asked")
            return "refused"
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("pending",), to="expired", now=now)
            return "expired"
        if not self.dashboard_bound(found, binding_now):
            self._store.decide_approval(approval_id, expect=("pending",), to="void", now=now,
                                        detail="the dashboard changed before it was confirmed")  # fmt: skip
            self._audit("void", by, found, "changed before confirmation")
            return "void"
        if not self._store.decide_approval(approval_id, expect=("pending",), to="approved", now=now, by=user):
            return "refused"
        self._audit("approved", by, found)
        return "approved"

    def consume_dashboard(self, approval_id: str, binding_now: Mapping[str, Any] | None) -> bool:
        """Single use, right before the dashboard is saved: approved, unexpired, still bound."""
        found = self._store.approval(approval_id)
        now = self._clock()
        if found is None or not is_dashboard(found) or found["state"] != "approved" or not found["decided_by"]:
            return False
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(approval_id, expect=("approved",), to="expired", now=now)
            return False
        if not self.dashboard_bound(found, binding_now):
            self._store.decide_approval(approval_id, expect=("approved",), to="void", now=now,
                                        detail="the dashboard changed since the plan was approved")  # fmt: skip
            return False
        return self._store.decide_approval(approval_id, expect=("approved",), to="used", now=now)

    def void_subject(self, subject: str, *, detail: str) -> None:
        for found in self._store.approvals(states=LIVE, limit=200):
            if found["subject"] == subject:
                self._store.decide_approval(found["id"], expect=LIVE, to="void", now=self._clock(), detail=detail)
