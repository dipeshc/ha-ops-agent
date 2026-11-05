"""Approvals: one admin action bound to the exact plan, single use, expiring; replay, drift and forgery refused."""

import dataclasses
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd.approvals import APPROVAL_TTL, ApprovalError, plan_hash
from opsd.policy import Bump, Decision, Tier
from opsd.updexec import ExecutorRefused
from tests.test_panel import Panel, h, local_ingress, p  # noqa: F401
from tests.test_updates import ACME_POOL, CARD, OS, START, WEATHER, Rig, entity, job, only_auto

ADMIN = "u-admin"


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    r = Rig(tmp_path)
    only_auto(r)  # no AUTO candidates: only what is approved runs
    yield r
    r.store.close()


async def latest_plan(rig: Rig) -> tuple[int, dict[str, dict[str, Any]]]:
    await rig.manager.run_window("manual")
    run = rig.store.recent_update_runs(limit=1)[0]
    return int(run["id"]), {i["entity_id"]: i for i in run["plan"]}


async def approve(rig: Rig, entity_id: str, *, when: str = "window", user: str = ADMIN) -> str:
    run, items = await latest_plan(rig)
    found = await rig.manager.approve(items[entity_id], plan_run=run, user=user, when=when)
    return str(found["id"])


def state(rig: Rig, approval_id: str) -> str:
    found = rig.store.approval(approval_id)
    assert found is not None
    return str(found["state"])


async def test_an_approved_update_runs_in_the_window_through_the_pipeline(rig: Rig) -> None:
    approval = await approve(rig, WEATHER)
    assert state(rig, approval) == "approved"
    assert [r["decision"] for r in rig.audit("approval.approved")] == ["approved"]
    rig.now += timedelta(days=1)
    result = await rig.manager.run_window()
    assert result["result"] == "executed"
    assert ("install_entity", (WEATHER, "v2.4.1")) in rig.ops.installs()
    assert rig.ops.calls[0][0] == "backup"  # the same backup-first pipeline
    job_ = rig.jobs()[WEATHER]
    assert job_.state == "succeeded"
    found = rig.store.approval(approval)
    assert found is not None and found["state"] == "used" and found["used_by_job"] == job_.id
    install = [r for r in rig.audit("update.install") if r["decision"] == "approved"]
    assert install and install[0]["tier"] == "approve"


async def test_replay_is_refused_and_an_approval_is_used_once(rig: Rig) -> None:
    approval = await approve(rig, WEATHER)
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert state(rig, approval) == "used"
    assert not rig.approvals.consume_update(approval, 999)
    assert not rig.approvals.proves_update(approval, job("hacs", WEATHER))  # another job cannot borrow it


async def test_an_expired_approval_does_nothing(rig: Rig) -> None:
    approval = await approve(rig, WEATHER)
    rig.now += APPROVAL_TTL + timedelta(minutes=1)
    await rig.manager.run_window()
    assert rig.ops.installs() == [] and state(rig, approval) == "expired"


async def test_a_version_change_voids_the_approval(rig: Rig) -> None:
    approval = await approve(rig, WEATHER)
    rig.ops.entities[WEATHER]["attributes"]["latest_version"] = "v2.4.2"
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert rig.ops.installs() == [] and state(rig, approval) == "void"
    # And a change between the plan the admin saw and the tap refuses the tap: nothing is approved.
    run, items = await latest_plan(rig)
    rig.ops.entities[WEATHER]["attributes"]["latest_version"] = "v2.4.3"
    with pytest.raises(ApprovalError, match=r"now offers .* instead"):
        await rig.manager.approve(items[WEATHER], plan_run=run, user=ADMIN, when="window")
    assert rig.store.approvals(states=("approved",)) == []
    assert rig.audit("approval.refused")


async def test_the_hash_binds_entity_versions_and_plan(rig: Rig) -> None:
    item = {"entity_id": WEATHER, "kind": "hacs", "installed": "v2.4.0", "latest": "v2.4.1", "tier": "approve",
            "reason": "fragile", "review": {"risk": "low", "reason": "fine"}}  # fmt: skip
    base = plan_hash(item, 7, "2026-01-28T04:00:00+00:00")
    changes = ({"entity_id": CARD}, {"latest": "v2.4.2"}, {"installed": "v2.3.9"}, {"tier": "auto"},
               {"reason": "other"}, {"review": {"risk": "high", "reason": "breaking"}}, {"held": True})  # fmt: skip
    for change in changes:
        assert plan_hash(item | change, 7, "2026-01-28T04:00:00+00:00") != base, change
    assert plan_hash(item, 8, "2026-01-28T04:00:00+00:00") != base
    assert plan_hash(item, 7, "2026-01-28T04:00:01+00:00") != base
    # A tampered stored hash voids it, even with the right versions.
    found = await approve(rig, WEATHER)
    with rig.store._tx() as conn:
        conn.execute("UPDATE approvals SET plan_hash = ? WHERE id = ?", ("0" * 64, found))
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert rig.ops.installs() == [] and state(rig, found) == "void"


async def test_approving_while_paused_waits_for_the_resume(rig: Rig) -> None:
    rig.manager.set_paused(True, by="panel:u-admin")
    approval = await approve(rig, WEATHER, when="now")
    result = await rig.manager.run_window("approved")
    assert result["result"] == "reported" and rig.ops.installs() == []
    # No push; the approved item stays Scheduled and says why it waits.
    item = rig.item(WEATHER, "v2.4.1")
    assert item["state"] == "scheduled" and "waits: automatic updates are paused" in item["gist"]
    assert not [m for m in await rig.flush() if "waits" in m.body]
    assert state(rig, approval) == "approved"
    rig.manager.set_paused(False, by="panel:u-admin")
    await rig.manager.run_window("approved")
    assert rig.jobs()[WEATHER].state == "succeeded" and state(rig, approval) == "used"


async def test_now_only_runs_approved_items_not_the_auto_ones(rig: Rig) -> None:
    rig.ops._apply(CARD, "v1.16.12")  # an AUTO candidate is pending too
    await approve(rig, WEATHER, when="now")
    await rig.manager.run_window("approved")
    assert set(rig.jobs()) == {WEATHER}


async def test_executors_need_a_consumed_approval_for_the_approve_tier(rig: Rig) -> None:
    approve_tier = Decision(Tier.APPROVE, Bump.PATCH, "fragile")
    forged = dataclasses.replace(job("hacs", WEATHER), target={"approval": "f" * 32})
    with pytest.raises(ExecutorRefused, match="needs_approval"):
        await rig.manager.executors.install(forged, approve_tier)
    # Nothing lifts NEVER, approval or not.
    with pytest.raises(ExecutorRefused, match="denied"):
        await rig.manager.executors.install(forged, Decision(Tier.NEVER, Bump.PATCH, ".0"))
    assert rig.ops.installs() == []


async def test_reject(rig: Rig) -> None:
    run, items = await latest_plan(rig)
    rejected = rig.approvals.reject_update(items[WEATHER], plan_run=run, user=ADMIN)
    assert state(rig, rejected["id"]) == "rejected"
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    await rig.flush()
    assert rig.item(WEATHER, "v2.4.1")["state"] == "cancelled"  # rejected: not asked again this week
    assert not [m for m in rig.sent if m.tag == rig.item(WEATHER, "v2.4.1")["id"]]
    _, items = await latest_plan(rig)
    with pytest.raises(ApprovalError):
        await rig.manager.approve(items[ACME_POOL], plan_run=run, user=ADMIN, when="window")  # pinned


async def test_the_operating_system_can_be_approved_and_is_never_rolled_back(rig: Rig) -> None:
    rig.ops.entities[OS] = entity("17.2", "17.3", 27, "Home Assistant Operating System")
    rig.ops.registry.append({"entity_id": OS, "platform": "hassio", "unique_id": "home_assistant_os_version_latest"})
    approval = await approve(rig, OS)
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert ("update_os", "17.3") in rig.ops.installs() and rig.jobs()[OS].state == "succeeded"
    assert state(rig, approval) == "used"
    # A regression after an OS update is escalated, never "rolled back".
    rig.ops.entities[OS] = entity("17.3", "17.4", 27, "Home Assistant Operating System")
    rig.ops.bad.add((OS, "17.4"))
    await approve(rig, OS)
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert rig.jobs()[OS].state == "rollback_failed" and "operating system" in rig.jobs()[OS].detail


async def test_approvals_ask_once_with_a_link_to_the_panel(rig: Rig) -> None:
    await rig.manager.run_window()
    [push] = await rig.flush()
    item = rig.item(WEATHER, "v2.4.1")
    assert push.tag == item["id"] and push.url is not None and push.url.endswith(f"/t/{item['id']}")
    assert "Needs you: needs your approval" in push.body
    # The push opens the item; nothing executes from it. Its one button is Reply: a message on the item.
    assert [str(a.kind) for a in push.actions] == ["REPLY"] and push.thread_id == f"item:{item['id']}"
    rig.now += timedelta(hours=20)
    await rig.manager.run_window()
    assert len(await rig.flush()) == 1


# ------------------------------------------------------------------ panel


def seed_plan(p: Panel) -> int:  # noqa: F811
    assert p.d.updates is not None
    item = {"entity_id": WEATHER, "title": "Acme Weather", "kind": "hacs", "installed": "v2.4.0", "latest": "v2.4.1",
            "tier": "approve", "reason": "fragile"}  # fmt: skip
    run = p.d.updates.store.start_update_run(window_day="d", trigger="manual", mode="report", plan=[item], now=START)
    assert run is not None

    async def current(entity_id: str) -> dict[str, Any]:
        return {"entity_id": entity_id, **entity("v2.4.0", "v2.4.1", 23)}

    p.d.updates.ops.entity_state = current  # type: ignore[method-assign]
    return run


async def test_the_approval_is_checked_against_its_plan_row(rig: Rig) -> None:
    import json

    run, items = await latest_plan(rig)
    # Editing the approval's own copy (another plan id, or another version) breaks the binding.
    for edit in ({"plan": run + 50}, {"to": "v9.9.9"}):
        found = await rig.manager.approve(items[WEATHER], plan_run=run, user=ADMIN, when="window")
        with rig.store._tx() as conn:
            edited = json.dumps(found["plan"] | edit)
            conn.execute("UPDATE approvals SET payload = ? WHERE kind = 'approval' AND id = ?", (edited, found["id"]))
        assert not rig.approvals.bound_update(rig.store.approval(found["id"]) or {})
        assert "approved" not in str(rig.approvals.annotate(list(items.values()), void=True))
        assert state(rig, found["id"]) == "void"
    # So does a change to the plan row it came from (what the admin was shown).
    approval = await approve(rig, WEATHER)
    source_run = int((rig.store.approval(approval) or {})["plan"]["plan"])
    source = rig.store.update_run(source_run)
    assert source is not None
    plan = [i | {"reason": "rewritten"} if i["entity_id"] == WEATHER else i for i in source["plan"]]
    rig.store.set_update_run(source_run, now=rig.now, plan=plan)
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert rig.ops.installs() == [] and state(rig, approval) == "void"


async def test_using_an_approval_checks_its_expiry_its_plan_and_the_job(rig: Rig) -> None:
    """Single use when the job is created: an expired approval or one for another action is never used."""
    from opsd.updates import job_spec

    approval = await approve(rig, WEATHER)
    run, items = await latest_plan(rig)
    [wrong] = rig.store.add_update_jobs(run, [job_spec(items[WEATHER] | {"latest": "v2.4.9"})], now=rig.now)
    assert not rig.approvals.consume_update(approval, wrong.id) and state(rig, approval) == "void"
    assert not rig.approvals.consume_update(approval, 999_999)

    late = await approve(rig, WEATHER)
    run, items = await latest_plan(rig)
    [right] = rig.store.add_update_jobs(run, [job_spec(items[WEATHER])], now=rig.now)
    rig.now += APPROVAL_TTL + timedelta(hours=5)
    assert not rig.approvals.consume_update(late, right.id) and state(rig, late) == "expired"


async def test_an_approval_is_void_when_what_it_installs_changed(rig: Rig) -> None:
    """Same entity and versions, but another HACS repository behind it: the approval does not carry over."""
    approval = await approve(rig, WEATHER)
    rig.ops.hacs["612978245"]["full_name"] = "someone-else/ha-acme-weather"
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert rig.ops.installs() == [] and state(rig, approval) == "void"
    found = rig.store.approval(approval)
    assert found is not None and "hacs_repo changed" in found["detail"]
