"""Approvals: the admin's one panel action, bound to one exact action (spec approvals.md A1-A17).

An approval is granted already `approved` by the admin's panel request (ingress admin and CSRF checked by the
panel), used once (`approved` -> `used`, compare-and-set), expires after `APPROVAL_TTL` and is audited at every
decision. An update approval binds the plan item as stored in its update run (`plan_hash`); the other `KINDS`
bind a binding the daemon recomputes from git, disk or the job right before use (`binding_hash`).
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
# The approvals bound to a daemon-computed binding; an update approval's `plan.kind` is the update's own kind.
KINDS = frozenset({"change", "reboot", "fork_rebase"})
WHEN = ("window", "now")
LIVE = ("approved",)


class ApprovalError(ValueError):
    """The approval cannot be granted; the message is safe to show the admin."""


# The parts of a plan item an approval is about: what is installed, why it needs approval,
# and what the release-note review said when the admin looked at it.
HASHED_FIELDS = ("entity_id", "kind", "title", "installed", "latest", "tier", "bump", "reason", "review", "held")
# What identifies the thing being installed: tonight's plan item must match the approved one on each.
IDENTITY_FIELDS = ("kind", "title", "slug", "hacs_repo")


def _sha(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def plan_hash(item: Mapping[str, Any], plan_run: int, created_at: str) -> str:
    """SHA-256 of the plan item as shown, the plan (update run) id and the approval's creation time."""
    return _sha({"item": {k: item.get(k) for k in HASHED_FIELDS}, "plan": plan_run, "created_at": created_at})


def binding_hash(kind: str, binding: Mapping[str, Any], created_at: str) -> str:
    """SHA-256 of a `KINDS` approval's binding and its creation time."""
    return _sha({kind: dict(binding), "created_at": created_at})


def kind_of(found: Mapping[str, Any]) -> str:
    """`change`, `reboot`, `fork_rebase`, `update`, or "" for a record that is none of them."""
    kind = (found.get("plan") or {}).get("kind")
    if kind in KINDS:
        return str(kind)
    return "update" if kind in APPROVABLE_KINDS else ""


def is_change(found: Mapping[str, Any]) -> bool:
    return kind_of(found) == "change"


def is_reboot(found: Mapping[str, Any]) -> bool:
    return kind_of(found) == "reboot"


def is_fork(found: Mapping[str, Any]) -> bool:
    return kind_of(found) == "fork_rebase"


def is_update(found: Mapping[str, Any]) -> bool:
    return kind_of(found) == "update"


def fork_subject(proposal: str) -> str:
    return f"fork_rebase:{proposal}"


def reboot_subject(entity_id: str, version: str) -> str:
    return f"reboot:{entity_id}@{version}"


def reboot_binding(job: UpdateJob) -> dict[str, Any]:
    """What a reboot approval is about: this job, waiting for its reboot since it finished."""
    return {"job": job.id, "entity_id": job.entity_id, "from": job.from_version, "to": job.to_version,
            "state": job.state, "finished_at": job.finished_at}  # fmt: skip


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


def live_mismatch(current: Mapping[str, Any] | None, frm: Any, to: Any) -> str | None:
    """Why the live update entity is no longer exactly `frm` -> `to` on offer (plain words), or None."""
    if current is None:
        return "Home Assistant no longer lists this update"
    attrs = current.get("attributes") or {}
    installed, latest = attrs.get("installed_version"), attrs.get("latest_version")
    if current.get("state") != "on" or not isinstance(installed, str) or not isinstance(latest, str):
        return "Home Assistant no longer offers this update (installed, skipped or waiting for a reboot)"
    if not same_version(installed, frm) or not same_version(latest, to):
        return f"Home Assistant now offers {installed} → {latest} instead"
    return None


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

    def _create(self, plan: dict[str, Any], hash_of: Callable[[str], str], *, subject_: str, user: str, when: str,
                state: str, supersede: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:  # fmt: skip
        """A new record in `state`, decided by `user`; any earlier live one it supersedes is voided first."""
        if when not in WHEN:
            raise ApprovalError("when must be window or now")
        now = self._clock()
        for old in self._store.approvals(states=LIVE, limit=200):
            if supersede(old):
                self._store.decide_approval(old["id"], expect=LIVE, to="void", now=now, detail="superseded")
        approval_id = secrets.token_hex(16)
        created = now.astimezone(UTC).isoformat(timespec="seconds")
        self._store.create_approval(approval_id=approval_id, plan=plan, plan_hash=hash_of(created), subject=subject_,
                                    requested_by=user, run_when=when, now=now, expires_at=now + APPROVAL_TTL,
                                    state=state, decided_by=user)  # fmt: skip
        found = self._store.approval(approval_id)
        if found is None:
            raise ApprovalError("the approval could not be saved")
        self._audit(state, f"panel:{user[:64]}", found)
        return found

    # ------------------------------------------------------------ granting

    def grant(self, kind: str, binding: Mapping[str, Any], *, subject_: str, title: str, user: str,
              when: str = "now", **extra: Any) -> dict[str, Any]:  # fmt: skip
        """The admin's approval of one `KINDS` action, bound to `binding` as the daemon computed it just now."""
        if kind not in KINDS:
            raise ApprovalError(f"not an approval kind: {kind}")
        plan = {"kind": kind, "binding": dict(binding), "title": title[:120], **extra}
        return self._create(plan, lambda created: binding_hash(kind, binding, created), subject_=subject_,
                            user=user, when=when, state="approved",
                            supersede=lambda old: old["subject"] == subject_)  # fmt: skip

    def _update_record(self, item: Mapping[str, Any], *, plan_run: int, user: str, when: str,
                       state: str) -> dict[str, Any]:  # fmt: skip
        if not approvable(item):
            raise ApprovalError("this update cannot be approved")
        entity, frm, to = str(item["entity_id"]), str(item["installed"]), str(item["latest"])
        plan = {"entity_id": entity, "from": frm, "to": to, "plan": plan_run, "kind": item.get("kind"),
                "title": str(item.get("title") or entity)[:120], "tier": item.get("tier"),
                "reason": str(item.get("reason") or "")[:200]}  # fmt: skip
        # A new decision supersedes any earlier live approval for the same update.
        return self._create(
            plan,
            lambda created: plan_hash(item, plan_run, created),
            subject_=subject(entity, to),
            user=user,
            when=when,
            state=state,
            supersede=lambda old: is_update(old) and old["plan"].get("entity_id") == entity,
        )

    def grant_update(self, item: Mapping[str, Any], *, plan_run: int, user: str, when: str,
                     current: Mapping[str, Any] | None) -> dict[str, Any]:  # fmt: skip
        """The admin's approval of this exact item of this plan; `current` is the live update entity, which must
        still offer exactly these versions."""
        if not approvable(item):
            raise ApprovalError("this update cannot be approved")
        mismatch = live_mismatch(current, item.get("installed"), item.get("latest"))
        if mismatch is not None:
            self._audit(
                "refused",
                f"panel:{user[:64]}",
                {"subject": subject(str(item.get("entity_id")), str(item.get("latest")))},
                mismatch,
            )
            raise ApprovalError(mismatch)
        return self._update_record(item, plan_run=plan_run, user=user, when=when, state="approved")

    def reject_update(self, item: Mapping[str, Any], *, plan_run: int, user: str) -> dict[str, Any]:
        """Reject an update nobody approved yet: a rejected record, so the agent does not ask again this week."""
        return self._update_record(item, plan_run=plan_run, user=user, when="window", state="rejected")

    def withdraw(self, subject_: str, *, detail: str) -> None:
        """Void every live approval for this subject (always safe)."""
        for found in self._store.approvals(states=LIVE, limit=200):
            if found["subject"] == subject_:
                self._store.decide_approval(found["id"], expect=LIVE, to="void", now=self._clock(), detail=detail)

    def expire(self) -> None:
        now = self._clock()
        for found in self._store.approvals(states=LIVE, limit=200):
            if now >= datetime.fromisoformat(found["expires_at"]):
                self._store.decide_approval(found["id"], expect=LIVE, to="expired", now=now)

    # ------------------------------------------------------------ binding

    @staticmethod
    def bound(found: Mapping[str, Any], binding_now: Mapping[str, Any] | None) -> bool:
        """A `KINDS` approval's hash still matches the binding recomputed now."""
        kind = kind_of(found)
        if kind not in KINDS or binding_now is None:
            return False
        recomputed = binding_hash(kind, binding_now, str(found["created_at"]))
        return hmac.compare_digest(recomputed, str(found["plan_hash"]))

    def bound_update(self, found: Mapping[str, Any]) -> bool:
        """An update approval still describes the plan it was given for.

        The hash is recomputed from the plan item as stored in its update run (a separate
        row the approval cannot change) and the approval's creation time; editing the
        approval's own copy of the plan, its plan id or its hash breaks it.
        """
        if not is_update(found):
            return False
        plan = found.get("plan") or {}
        source = self._source(found)
        if source is None:
            return False
        if not same_version(source.get("installed"), plan.get("from")) or not same_version(
            source.get("latest"), plan.get("to")
        ):
            return False
        recomputed = plan_hash(source, int(plan["plan"]), str(found["created_at"]))
        return hmac.compare_digest(recomputed, str(found["plan_hash"]))

    def _source(self, found: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The plan item the approval was given for, as stored in its update run (not the approval's own copy)."""
        plan = found.get("plan") or {}
        run = self._store.update_run(int(plan.get("plan") or 0))
        return next((i for i in (run or {}).get("plan", []) if i.get("entity_id") == plan.get("entity_id")), None)

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
        marks: dict[str, dict[str, Any]] = {}
        for found in (a for a in self._store.approvals(states=LIVE, limit=200) if is_update(a)):
            plan_item = found["plan"]
            reason = self._plan_mismatch(found, by_entity.get(str(plan_item.get("entity_id"))))
            if reason is not None:
                if void:
                    self._store.decide_approval(found["id"], expect=LIVE, to="void", now=now, detail=reason)
                    self._audit("void", "daemon", found, reason)
                continue
            marks.setdefault(str(plan_item["entity_id"]), found)
        out = []
        for item in plan:
            mark_of = marks.get(str(item.get("entity_id")))
            if mark_of is None:
                out.append(item)
                continue
            mark = {"approval": mark_of["id"], "approval_state": mark_of["state"], "run_when": mark_of["run_when"]}
            ready = approvable(item) and (not now_only or mark_of["run_when"] == "now")
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
        if not self.bound_update(found):
            return "the update plan it was given for changed"
        source = self._source(found) or {}
        changed = [k for k in IDENTITY_FIELDS if (item.get(k) or None) != (source.get(k) or None)]
        if changed:
            # What is installed and how (an add-on slug, a HACS repository) is part of what was approved.
            return f"the update is no longer the one approved ({', '.join(changed)} changed)"
        return None

    # ------------------------------------------------------------ single use

    def _usable(self, found: Mapping[str, Any] | None, kind: str) -> bool:
        """Approved, by someone, of this kind, and unexpired (an expired one is marked so)."""
        if found is None or kind_of(found) != kind or found["state"] != "approved" or not found["decided_by"]:
            return False
        now = self._clock()
        if now >= datetime.fromisoformat(found["expires_at"]):
            self._store.decide_approval(str(found["id"]), expect=("approved",), to="expired", now=now)
            return False
        return True

    def consume(self, approval_id: str, kind: str, binding_now: Mapping[str, Any] | None, *,
                job: int | None = None) -> bool:  # fmt: skip
        """Single use, right before the action: approved, unexpired and still bound (else void)."""
        found = self._store.approval(approval_id)
        if found is None or not self._usable(found, kind):
            return False
        now = self._clock()
        if not self.bound(found, binding_now):
            self._store.decide_approval(approval_id, expect=("approved",), to="void", now=now,
                                        detail="what it approved changed since")  # fmt: skip
            return False
        return self._store.decide_approval(approval_id, expect=("approved",), to="used", now=now, job=job)

    def consume_update(self, approval_id: str, job_id: int) -> bool:
        """Single use, when the job is created: approved, unexpired, still bound and for exactly this job."""
        found = self._store.approval(approval_id)
        if found is None or not self._usable(found, "update"):
            return False
        job = self._store.update_job(job_id)
        plan = found["plan"]
        now = self._clock()
        if (
            job is None
            or not self.bound_update(found)
            or plan.get("entity_id") != job.entity_id
            or not same_version(plan.get("from"), job.from_version)
            or not same_version(plan.get("to"), job.to_version)
        ):
            self._store.decide_approval(approval_id, expect=("approved",), to="void", now=now,
                                        detail="the update plan it was given for changed")  # fmt: skip
            return False
        return self._store.decide_approval(approval_id, expect=("approved",), to="used", now=now, job=job_id)

    def _used_by(self, approval_id: str | None, kind: str, job: UpdateJob) -> Mapping[str, Any] | None:
        found = self._store.approval(approval_id) if approval_id else None
        if found is None or kind_of(found) != kind or found["state"] != "used" or found["used_by_job"] != job.id:
            return None
        return found if found["decided_by"] else None

    def proves(self, approval_id: str | None, kind: str, job: UpdateJob,
               binding_now: Mapping[str, Any] | None) -> bool:  # fmt: skip
        """The executor's check: this job consumed this `KINDS` approval, and it still binds."""
        found = self._used_by(approval_id, kind, job)
        return found is not None and self.bound(found, binding_now)

    def proves_update(self, approval_id: str | None, job: UpdateJob) -> bool:
        """The executor's check: this job consumed this update approval, for exactly this action."""
        found = self._used_by(approval_id, "update", job)
        if found is None or not self.bound_update(found):
            return False
        plan = found["plan"]
        return (
            plan.get("entity_id") == job.entity_id
            and same_version(plan.get("from"), job.from_version)
            and same_version(plan.get("to"), job.to_version)
        )
