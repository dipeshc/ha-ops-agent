"""An approved HAOS update is installed, then activated by the daemon's own reboot.

The scenario: an OS update is approved. The Supervisor logs "Install of Home Assistant Operating
System <version> success; reboot required", raises the `reboot_required` issue and reports the new
version as `version_pending`. Since Supervisor 2026.07.0 (supervisor#6982) `/os/update` no longer
reboots, so an agent that waits for the new version alone would record the install as failed, and a
chat without the evidence would have to guess why.
"""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import selfguard
from opsd.capabilities import CAPABILITIES, Args
from opsd.policy import Bump, Decision, Tier
from opsd.redact import Redactor
from opsd.runctx import ExecContext, ExecEnv
from opsd.runner import CHAT, INVESTIGATE, compose_system_prompt
from opsd.store import UpdateJob
from opsd.supervisor import ApiError
from opsd.updates import OS_NOTE, OsState, cmd_plan, os_phase
from opsd.updexec import ExecutorRefused, os_log_lines
from tests.conftest import WATCHED as WATCHED_ENTITIES
from tests.test_approvals import ADMIN, approve, latest_plan, state
from tests.test_updates import BROKER, OS, WEATHER, Rig, entity

PROMPTS = Path(__file__).resolve().parents[1] / "ha_ops_agent/rootfs/opt/ops/prompts"
APPROVE = Decision(Tier.APPROVE, Bump.MINOR, "operating system")


class Died(BaseException):
    """The add-on is stopped by the host reboot (the process ends mid-call)."""


@pytest.fixture
def os_rig(tmp_path: Path) -> Any:
    r = Rig(tmp_path)
    r.ops.entities[OS] = entity("17.3", "17.4", 27, "Home Assistant Operating System")
    r.ops.registry.append({"entity_id": OS, "platform": "hassio", "unique_id": "home_assistant_os_version_latest"})
    notes: list[tuple[str, str, bool]] = []
    r.manager.activity = lambda kind, title, detail="", needs_admin=False, **_: notes.append(
        (title, detail, needs_admin)
    )
    r.notes = notes
    yield r
    r.store.close()


async def approved_night(rig: Rig) -> str:
    approval, outcome = await approve(rig, OS)
    assert outcome == "approved"
    rig.now += timedelta(days=1)
    return approval


def outcome_of(rig: Rig, approval: str) -> str:
    """`<approval state>:<job state>:<detail>` (the job that used the approval, if any)."""
    found = rig.store.approval(approval) or {}
    job = rig.store.update_job(int(found["used_by_job"])) if found.get("used_by_job") else None
    return f"{found.get('state')}:{job.state if job else ''}:{job.detail if job else found.get('detail') or ''}"


# ------------------------------------------------------------------ the pipeline


async def test_install_success_reboot_required_is_installed_then_the_daemon_reboots_once(os_rig: Rig) -> None:
    rig = os_rig
    approval = await approved_night(rig)
    await rig.manager.run_window()
    calls = [c for c in rig.ops.calls if c[0] in ("update_os", "reboot_host")]
    assert calls == [("update_os", "17.4"), ("reboot_host", None)]
    job = rig.jobs()[OS]
    assert job.state == "succeeded" and "rebooted" in job.detail
    assert "all 3 watched entities available" in job.detail
    # The Supervisor's own lines are kept with the job (that boot's journal is gone after the reboot).
    evidence = job.target["os_install"]
    assert evidence["version"] == "17.3" and evidence["version_pending"] == "17.4" and evidence["reboot_required"]
    assert any("Install of Home Assistant Operating System 17.4 success; reboot required" in x for x in evidence["log"])
    assert job.target["reboot"]["boot_after"] != job.target["reboot"]["boot_before"]
    assert outcome_of(rig, approval).startswith("used:succeeded:")
    # A good night is silent: no push, the summary is in Activity and the notes.
    assert rig.sent == []
    [(_, summary, needs_admin)] = [n for n in rig.notes if n[0] == "Update night"]
    assert "Home Assistant Operating System 17.3 → 17.4" in summary and "Failed" not in summary and not needs_admin
    # The approved item that ran is not listed as still needing approval.
    assert "Operating System" not in summary.split("Needs your approval")[-1]
    reboot_audit = [r for r in rig.audit("update.install") if json.loads(r["args"]).get("step") == "reboot"]
    # Newest first: the approved, executed reboot (after the APPROVE tier was lifted by the verified approval).
    assert reboot_audit[0]["decision"] == "approved" and reboot_audit[0]["outcome"] == "ok"


async def test_the_run_survives_the_reboot_and_the_next_start_finishes_it(os_rig: Rig) -> None:
    rig = os_rig
    approval = await approved_night(rig)
    real_reboot = rig.ops.reboot_host

    async def reboot_and_die() -> None:
        await real_reboot()
        raise Died

    rig.ops.reboot_host = reboot_and_die  # type: ignore[method-assign]
    with pytest.raises(Died):
        await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "rebooting" and job.target["reboot"]["requested_at"]
    assert not [m for m in rig.sent if m.title == "Ops Agent: updates"]  # no summary before it is confirmed
    # The add-on starts again after the reboot: a new process, the same store.
    rig.ops.reboot_host = real_reboot  # type: ignore[method-assign]
    rig.manager = rig.build()
    assert await rig.manager.resume()
    job = rig.jobs()[OS]
    assert job.state == "succeeded" and "health matches" in job.detail
    assert rig.ops.reboots == 1  # never a second reboot
    assert rig.run()["state"] == "done"
    assert rig.sent == []  # silent: it worked
    assert outcome_of(rig, approval).startswith("used:succeeded:")
    # The plan is re-made from what is installed: nothing pending any more.
    latest = rig.store.recent_update_runs(limit=1)[0]
    assert latest["trigger"] == "refresh" and OS not in {i["entity_id"] for i in latest["plan"]}


async def test_coming_back_on_the_old_version_is_a_real_failure_and_escalates(os_rig: Rig) -> None:
    rig = os_rig
    rig.ops.os_fallback = True
    await approved_night(rig)
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "failed" and "fell back to the old slot" in job.detail
    item = rig.item(OS, "17.4")
    [alert] = [m for m in await rig.flush() if m.tag == item["id"]]
    assert item["state"] == "failed" and item["needs"]
    assert alert.urgent and "17.3" in alert.body and "did not activate" in alert.body
    assert rig.manager.paused() and rig.manager.failed_block() is not None
    assert rig.manager.holds()[f"{OS}@17.4"]["state"] == "failed"
    assert rig.ops.reboots == 1


async def test_a_host_that_does_not_reboot_leaves_it_installed_and_pending(os_rig: Rig) -> None:
    rig = os_rig
    rig.ops.reboot_ignored = True
    approval = await approved_night(rig)
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "reboot_pending" and "did not reboot within 15 minutes" in job.detail
    assert outcome_of(rig, approval).startswith("used:reboot_pending:")
    assert any(t.endswith("installed: reboot needed") for t, _, _ in rig.notes)
    # Installed, reboot pending: Needs you (never failed), one push tagged with the item; the night is silent.
    item = rig.item(OS, "17.4")
    [push] = [m for m in await rig.flush() if m.tag == item["id"]]
    assert item["state"] == "waiting" and item["need"]["primary"]["id"] == "reboot_window"
    assert "the host needs a reboot" in push.body and "Failed" not in push.body
    # Still pending: nothing to confirm.
    assert await rig.manager.reconcile() == []
    # The admin reboots; the next check (or start) confirms it and compares health.
    rig.ops.reboot_after()
    rig.now += timedelta(hours=3)
    assert await rig.manager.reconcile() == ["Home Assistant Operating System 17.3 → 17.4: installed after reboot"]
    job = rig.jobs()[OS]
    assert job.state == "succeeded" and job.detail.startswith("installed after reboot")
    assert "health matches the check before the update" in job.detail
    assert outcome_of(rig, approval).startswith("used:succeeded:")
    # The reboot-needed push went out; the confirmation is silent (on its item).
    assert [m.tag for m in await rig.flush() if m.tag == item["id"]] == [item["id"]]
    assert rig.item(OS, "17.4")["state"] == "done" and not rig.item(OS, "17.4")["needs"]
    assert any("is active after the reboot" in d and not admin for _, d, admin in rig.notes)
    assert rig.ops.reboots == 1  # the agent never reboots outside its approved run


async def test_no_reboot_after_the_window(os_rig: Rig) -> None:
    rig = os_rig
    await approved_night(rig)
    real = rig.ops.update_os

    async def slow_install(version: str) -> None:
        await real(version)
        rig.now += timedelta(minutes=80)  # the install used up the window

    rig.ops.update_os = slow_install  # type: ignore[method-assign]
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "reboot_pending" and "the update window is over" in job.detail
    assert rig.ops.reboots == 0


async def test_no_reboot_when_updates_were_paused_meanwhile(os_rig: Rig) -> None:
    rig = os_rig
    await approved_night(rig)
    real = rig.ops.update_os

    async def install_then_pause(version: str) -> None:
        await real(version)
        rig.manager.set_paused(True, by="panel:u-admin")

    rig.ops.update_os = install_then_pause  # type: ignore[method-assign]
    await rig.manager.run_window()
    assert rig.jobs()[OS].state == "reboot_pending" and rig.ops.reboots == 0


# --------------------------------------------------- a stale failed record and its cleanup


def failed_os_job(rig: Rig) -> tuple[int, str]:
    """An approved OS job recorded as "failed" while its version was only pending (an older agent's record)."""
    store = rig.store
    run = store.start_update_run(window_day="2026-01-30", trigger="window", mode="act",
                                 plan=[{"entity_id": OS, "kind": "os", "installed": "17.3", "latest": "17.4",
                                        "tier": "approve", "title": "Home Assistant Operating System"}],
                                 now=rig.now)  # fmt: skip
    assert run is not None
    store.set_update_run(run, now=rig.now, state="installing", before=rig_point().to_json())
    [job] = store.add_update_jobs(
        run,
        [
            {
                "entity_id": OS,
                "kind": "os",
                "title": "Home Assistant Operating System",
                "from_version": "17.3",
                "to_version": "17.4",
                "target": {},
            }
        ],
        now=rig.now,
    )
    store.transition_job(job.id, expect=("pending",), to="installing", now=rig.now, count_install=True)
    store.transition_job(job.id, expect=("installing",), to="failed", now=rig.now,
                         detail="install did not complete; still on 17.3")  # fmt: skip
    store.set_update_run(run, now=rig.now, state="done", summary="Updated 0 of 1. Failed: Home Assistant Operating"
                         " System (install did not complete; still on 17.3).")  # fmt: skip
    found = store.approvals(limit=1)
    approval = found[0]["id"] if found else ""
    return job.id, approval


def rig_point() -> Any:
    from opsd.updiff import HealthPoint

    return HealthPoint(at="t", core_state="running", core_version="2026.10.3", api_up=True,
                       custom={"acme_solar": {"entities": 3, "unavailable": 0, "entries_failed": 0}},
                       watched={WATCHED_ENTITIES[0]: "locked", WATCHED_ENTITIES[1]: "locked",
                                WATCHED_ENTITIES[2]: "disarmed"})  # fmt: skip


async def test_a_real_failure_is_not_reconciled(os_rig: Rig) -> None:
    rig = os_rig
    job_id, _ = failed_os_job(rig)
    rig.store.transition_job(
        job_id,
        expect=("failed",),
        to="failed",
        now=rig.now,
        detail="after the reboot the host came back on 17.3: the boot fell back to the old slot",
    )
    rig.ops.reboot_after()
    rig.ops._apply(OS, "17.4")
    assert await rig.manager.reconcile() == []


# ---------------------------------------------------------- the executor and its limits


def os_job(rig: Rig, approval: str | None) -> UpdateJob:
    return UpdateJob(id=99, run_id=1, seq=0, entity_id=OS, kind="os", title="Home Assistant Operating System",
                     from_version="17.3", to_version="17.4", target={"approval": approval} if approval else {},
                     state="rebooting", install_attempts=1, rollback_attempts=0, detail="", regressions=[],
                     started_at=None, finished_at=None, updated_at="t")  # fmt: skip


async def test_the_reboot_executor_needs_the_approval_and_the_pending_version(os_rig: Rig) -> None:
    rig = os_rig
    ex = rig.manager.executors
    await rig.ops.update_os("17.4")
    with pytest.raises(ExecutorRefused, match="needs the approval"):
        await ex.reboot_for_os(os_job(rig, None), APPROVE)
    with pytest.raises(ExecutorRefused, match="needs the approval"):
        await ex.reboot_for_os(os_job(rig, "forged-approval-id"), APPROVE)
    not_os = dataclasses.replace(os_job(rig, None), kind="core", entity_id="update.home_assistant_core_update")
    with pytest.raises(ExecutorRefused):
        await ex.reboot_for_os(not_os, APPROVE)
    assert rig.ops.reboots == 0


async def test_the_reboot_executor_refuses_when_nothing_is_pending(os_rig: Rig) -> None:
    rig = os_rig
    await approved_night(rig)
    rig.ops.reboot_ignored = True
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "reboot_pending"
    # A forged second attempt for the same, consumed approval: the version is not pending any more.
    rig.ops.os_pending = None
    with pytest.raises(ExecutorRefused, match="not installed and pending"):
        await rig.manager.executors.reboot_for_os(job, APPROVE)
    assert rig.ops.reboots == 1


def test_os_phase_reads_the_supervisor_as_it_reports() -> None:
    job = UpdateJob(id=1, run_id=1, seq=0, entity_id=OS, kind="os", title="HAOS", from_version="17.3",
                    to_version="17.4", target={}, state="installing", install_attempts=1, rollback_attempts=0,
                    detail="", regressions=[], started_at=None, finished_at=None, updated_at="t")  # fmt: skip

    def st(**kw: Any) -> OsState:
        base: dict[str, Any] = {"version": "17.3", "pending": None, "boot": "B", "boot_timestamp": 1,
                                "reboot_required": False}  # fmt: skip
        return OsState(**(base | kw))

    # Running the old version, the new one installed and pending.
    assert os_phase(st(pending="17.4", reboot_required=True), job) == "pending"
    # A Core older than core#177155: the Supervisor reports the pending version as `version` too.
    assert os_phase(st(version="17.4", pending="17.4"), job) == "pending"
    # Supervisor 2026.07.0 only (no version_pending yet): the issue and the other slot show it.
    assert os_phase(st(reboot_required=True, staged=("17.4",)), job) == "pending"
    assert os_phase(st(staged=("17.4",)), job) == "previous"
    assert os_phase(st(version="17.4"), job) == "active"
    assert os_phase(st(), job) == "previous"
    assert os_phase(st(version="17.5"), job) == "other"
    info = {"version": "17.3", "version_pending": "17.4", "boot": "B",
            "boot_slots": {"A": {"version": "17.4"}, "B": {"version": "17.3"}}}  # fmt: skip
    read = OsState.read(info, {"boot_timestamp": 5}, {"issues": [{"type": "reboot_required"}]})
    assert read.pending == "17.4" and read.staged == ("17.4",) and read.reboot_required and read.boot_timestamp == 5


def test_os_log_lines_keep_only_the_os_host_and_reboot_lines() -> None:
    text = (
        "\x1b[32m2026-01-02 02:17:08.120 INFO (MainThread) [supervisor.os.manager] Install of Home Assistant"
        " Operating System 17.4 success; reboot required\x1b[0m\n"
        "2026-01-30 02:17:08.121 INFO (MainThread) [supervisor.resolution.module] Create new issue reboot_required"
        " - system / None\n"
        "2026-01-30 02:17:08.122 INFO (MainThread) [supervisor.resolution.module] Create new issue no_current_backup\n"
        "2026-01-30 02:17:09.000 INFO (MainThread) [supervisor.api.middleware.security] /os/info access\n"
        "2026-01-30 06:40:00.000 INFO (MainThread) [supervisor.host.control] Initialize host reboot using logind\n"
    )
    lines = os_log_lines(text, 10)
    assert len(lines) == 3 and "\x1b" not in lines[0] and "reboot required" in lines[0]
    assert "reboot_required" in lines[1] and "host reboot" in lines[2]
    assert os_log_lines(text, 1) == [lines[-1]] and os_log_lines(text, 0) == []


# ------------------------------------------------------------- approvals in plain words


async def test_an_approval_for_an_installed_pending_os_update_says_why_it_is_not_needed(os_rig: Rig) -> None:
    rig = os_rig
    run, items = await latest_plan(rig)
    found = rig.approvals.request(items[OS], plan_run=run, user=ADMIN, when="window")
    # Meanwhile 17.4 was installed; Core's update entity no longer offers it.
    await rig.ops.update_os("17.4")
    rig.ops.entities[OS]["state"] = "off"
    assert await rig.manager.confirm_approval(found["id"], ADMIN) == "void"
    title, detail, needs_admin = rig.notes[-1]
    assert title == "Approval not needed: Home Assistant Operating System 17.3 → 17.4"
    # Said in the entry; whether it needs the admin is the item's business, not the note's.
    assert "already installed and waits for a reboot" in detail and not needs_admin
    assert "void" not in title and "@" not in title
    assert state(rig, found["id"]) == "void"
    assert "no longer offers this update" in str(rig.store.approval(found["id"])["detail"])  # type: ignore[index]


async def test_other_void_and_approved_notes_are_plain(os_rig: Rig) -> None:
    rig = os_rig
    run, items = await latest_plan(rig)
    found = rig.approvals.request(items[OS], plan_run=run, user=ADMIN, when="window")
    rig.ops.entities[OS] = entity("17.3", "17.5", 27, "Home Assistant Operating System")
    assert await rig.manager.confirm_approval(found["id"], ADMIN) == "void"
    title, detail, _ = rig.notes[-1]
    assert title.startswith("Approval not used:") and detail == "Home Assistant now offers 17.3 → 17.5 instead."
    rig.ops.entities[OS] = entity("17.3", "17.4", 27, "Home Assistant Operating System")
    _, outcome = await approve(rig, OS)
    assert outcome == "approved"
    title, detail, needs_admin = rig.notes[-1]
    assert title == "Update approved: Home Assistant Operating System 17.3 → 17.4"
    assert detail == "It installs at 04:00." and not needs_admin


# ---------------------------------------------------------------- panel and plan state


async def test_an_approved_item_is_not_shown_as_needing_approval(os_rig: Rig) -> None:
    rig = os_rig
    await rig.manager.run_window("manual")
    await rig.flush()
    assert rig.item(OS, "17.4")["state"] == "waiting"
    await approve(rig, OS)
    await rig.flush()
    assert rig.item(OS, "17.4")["state"] == "scheduled"


async def test_the_plan_is_re_made_when_installed_versions_change(os_rig: Rig) -> None:
    rig = os_rig
    await rig.manager.run_window("manual")
    first = rig.store.recent_update_runs(limit=1)[0]
    snap = [{"entity_id": i["entity_id"], "installed": i["installed"], "latest": i["latest"]} for i in first["plan"]]
    assert not rig.manager.plan_stale(snap)
    # The agent's own add-on was updated outside the agent (a stale self-update card).
    rig.ops._apply("update.ha_ops_agent_update", "0.5.1")
    newer = [i for i in snap if i["entity_id"] != "update.ha_ops_agent_update"]
    assert rig.manager.plan_stale(newer)
    assert (await rig.manager.refresh())["result"] == "reported"
    latest = rig.store.recent_update_runs(limit=1)[0]
    assert latest["trigger"] == "refresh" and latest["mode"] == "report"
    assert "update.ha_ops_agent_update" not in {i["entity_id"] for i in latest["plan"]}
    assert not rig.ops.installs() and not rig.sent  # silent and report-only


async def test_a_refresh_never_resumes_or_overlaps_a_running_night(os_rig: Rig) -> None:
    rig = os_rig
    async with rig.manager._lock:
        assert (await rig.manager.refresh())["result"] == "busy"


# --------------------------------------------------------------- honest diagnosis (chat)


class FactsClient:
    """The Supervisor as it answers between the install and the reboot."""

    def supervisor(self, path: str, method: str = "GET", body: Any = None, *, timeout: float | None = None) -> Any:
        assert method == "GET"
        return {
            "/os/info": {"version": "17.3", "version_latest": "17.4", "version_pending": "17.4",
                         "update_available": False, "boot": "B",
                         "boot_slots": {"A": {"state": "inactive", "status": "good", "version": "17.4"},
                                        "B": {"state": "booted", "status": "good", "version": "17.3"}}},
            "/host/info": {"boot_timestamp": 1700000000000000, "hostname": "example-host"},
            "/supervisor/info": {"version": "2026.1.0", "channel": "stable"},
            "/resolution/info": {"issues": [{"type": "reboot_required", "context": "system", "reference": None,
                                             "uuid": "u1"}],
                                 "suggestions": [{"type": "execute_reboot", "context": "system", "uuid": "u2",
                                                  "auto": False}],
                                 "unhealthy": [], "unsupported": []},
        }[path]  # fmt: skip

    def logs(self, path: str, lines: int = 200) -> str:
        assert path == "/supervisor/logs"
        return (
            "2026-01-30 02:17:08.120 INFO (MainThread) [supervisor.os.manager] Install of Home Assistant Operating"
            " System 17.4 success; reboot required\n"
            "2026-01-30 02:17:08.121 INFO (MainThread) [supervisor.resolution.module] Create new issue"
            " reboot_required - system / None\n"
        )


async def test_update_plan_gives_the_chat_the_os_evidence_as_data(os_rig: Rig) -> None:
    rig = os_rig
    job_id, _ = failed_os_job(rig)
    record = {"version": "17.3", "version_pending": "17.4", "log": ["Install of 17.4 success; reboot required"]}
    job = rig.store.update_job(job_id)
    assert job is not None
    rig.store.set_job_target(job_id, job.target | {"os_install": record})
    env = ExecEnv(paths=None, options=rig.options, client=FactsClient(), redactor=lambda: Redactor([]),  # type: ignore[arg-type]
                  store=rig.store)  # fmt: skip
    out = cmd_plan(ExecContext(env=env, capability=CAPABILITIES["update.plan"], run=None), Args({}))
    assert out["note"] == OS_NOTE and "no creation time" in out["note"] and "supervisor#6982" in out["note"]
    [found] = [j for j in out["last_update_night"]["jobs"] if j["entity_id"] == OS]
    assert found["state"] == "failed" and found["started_at"] and found["finished_at"]
    assert found["os_install"] == record


@pytest.mark.parametrize("profile", [CHAT, INVESTIGATE])
def test_the_prompt_asks_for_a_source_or_an_explicit_guess(profile: Any) -> None:
    prompt = " ".join(compose_system_prompt(PROMPTS, profile).split())
    assert "## Causes need a source" in prompt
    assert 'write "Guess:" first' in prompt
    assert "Never invent a date, a time or a sequence of events." in prompt
    assert "carry no creation time" in prompt
    assert "opsctl update-plan" in prompt and "update-plan" in prompt.split("## Capabilities in this run")[1]


def test_the_chat_prompt_explains_version_pending() -> None:
    prompt = (PROMPTS / "profiles/chat.md").read_text()
    assert "`version_pending` is installed and waits for a reboot" in " ".join(prompt.split())
    assert "It needs no new approval" in " ".join(prompt.split())


# ----------------------------------------------------------- the selfguard delta, end to end


async def test_only_the_executor_scope_reaches_the_reboot(os_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live ops' calls pass the guard only inside the executor's approved scope."""
    import urllib.request

    from opsd.supervisor import SupervisorClient
    from opsd.updexec import LiveUpdateOps

    reached: list[str] = []

    def urlopen(req: Any, timeout: float | None = None) -> Any:
        reached.append(req.full_url.removeprefix("http://supervisor"))
        raise AssertionError("stop")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    ops = LiveUpdateOps(SupervisorClient("token"))
    with pytest.raises(ApiError, match="refused"):
        await ops.reboot_host()
    with pytest.raises(ApiError, match="refused"):
        await ops.update_os("17.4")
    assert reached == []
    with selfguard.approved_os_flow(selfguard.HOST_REBOOT_PATH), pytest.raises(AssertionError):
        await ops.reboot_host()
    assert reached == ["/host/reboot"]


# ------------------------------------------------- adversarial checks of the OS update flow


async def test_review_1_a_silent_night_does_not_swallow_a_later_needs_you_push(os_rig: Rig) -> None:
    """The summary never pushes; each job that needs the admin is an item, and each one pushes."""
    rig = os_rig
    # Two runs that each leave the admin something to do push twice: a later one is never swallowed.
    rig.ops.reboot_ignored = True
    await approved_night(rig)
    await rig.manager.run_window()
    item = rig.item(OS, "17.4")
    assert item["state"] == "waiting" and [x.tag for x in await rig.flush() if x.tag == item["id"]] == [item["id"]]
    _, outcome = await approve(rig, WEATHER, when="now")
    assert outcome == "now"
    before = len(rig.sent)
    rig.ops.fail.add(WEATHER)
    await rig.manager.run_window("approved")
    acme_weather = rig.item(WEATHER, "v2.4.1")
    later = (await rig.flush())[before:]
    assert (
        acme_weather["state"] == "failed" and acme_weather["needs"] and [x.tag for x in later] == [acme_weather["id"]]
    )


async def test_review_2_an_approved_update_skipped_by_the_precheck_tells_the_admin(os_rig: Rig) -> None:
    rig = os_rig
    approval = await approved_night(rig)
    real_backup = rig.ops.create_backup

    async def backup_then_pause() -> str | None:
        found = await real_backup()
        rig.manager.set_paused(True, by="panel:u-admin")
        return found

    rig.ops.create_backup = backup_then_pause  # type: ignore[method-assign]
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "skipped" and "approve it again in the panel" in job.detail
    assert outcome_of(rig, approval).startswith("used:skipped:")
    assert "approve it again" in outcome_of(rig, approval)
    # Back in Needs you (approve it again), pushed with the item's tag.
    item = rig.item(OS, "17.4")
    assert item["state"] == "waiting" and "approve it again" in item["gist"]
    [push] = [x for x in await rig.flush() if x.tag == item["id"]]
    assert "Operating System" in push.title and "Needs you" in push.body
    assert rig.ops.installs() == []


async def test_review_4_the_window_is_checked_again_right_before_the_reboot(os_rig: Rig) -> None:
    rig = os_rig
    await approved_night(rig)
    real_install, real_log, real_jobs = rig.ops.update_os, rig.ops.os_log, rig.ops.jobs
    busy = {"polls": 0}

    async def install_late(version: str) -> None:
        await real_install(version)
        rig.now = datetime(2026, 1, 29, 5, 10, tzinfo=UTC)  # 20 minutes of the 04:00 window left

    async def log_then_busy(limit: int) -> list[str]:
        busy["polls"] = 10  # a Supervisor job starts as the reboot step begins
        return await real_log(limit)

    async def jobs() -> Any:
        if busy["polls"] > 0:
            busy["polls"] -= 1
            return {"jobs": [{"name": "backup_manager_full_backup", "done": False}]}
        return await real_jobs()

    rig.ops.update_os = install_late  # type: ignore[method-assign]
    rig.ops.os_log = log_then_busy  # type: ignore[method-assign]
    rig.ops.jobs = jobs  # type: ignore[method-assign]
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "reboot_pending" and "the update window is over" in job.detail
    assert rig.ops.reboots == 0


async def test_review_5_no_reboot_without_a_readable_boot_time(os_rig: Rig) -> None:
    rig = os_rig
    await approved_night(rig)

    async def no_host_info() -> dict[str, Any]:
        raise ApiError("GET", "/host/info", 500, "boom")

    rig.ops.host_info = no_host_info  # type: ignore[method-assign]
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "reboot_pending" and "boot time could not be read" in job.detail
    assert rig.ops.reboots == 0
    item = rig.item(OS, "17.4")
    [push] = [x for x in await rig.flush() if x.tag == item["id"]]
    assert "the host needs a reboot" in push.body


async def test_review_6_already_on_the_target_verifies_without_a_reboot_or_a_push(os_rig: Rig) -> None:
    rig = os_rig
    approval = await approved_night(rig)
    real_log = rig.ops.os_log

    async def log_then_someone_reboots(limit: int) -> list[str]:
        lines = await real_log(limit)
        rig.ops.reboot_after()  # the host was rebooted by someone else meanwhile
        return lines

    rig.ops.os_log = log_then_someone_reboots  # type: ignore[method-assign]
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    assert job.state == "succeeded" and rig.ops.reboots == 0
    item = rig.item(OS, "17.4")
    assert item["state"] == "done" and not [x for x in await rig.flush() if x.tag == item["id"]]  # no false "reboot"
    assert outcome_of(rig, approval).startswith("used:succeeded:")


async def test_review_7_only_the_nightly_plan_may_void_an_approval(os_rig: Rig) -> None:
    rig = os_rig
    approval, outcome = await approve(rig, OS)
    assert outcome == "approved"
    # After a reboot HACS and the Supervisor's update entities may not be loaded yet.
    gone = rig.ops.entities.pop(OS)
    assert (await rig.manager.refresh())["result"] == "reported"
    await rig.manager.run_window("manual")
    assert state(rig, approval) == "approved"
    # The nightly plan voids it, in plain words.
    rig.ops.entities[OS] = entity("17.3", "17.5", 27, "Home Assistant Operating System")
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    assert state(rig, approval) == "void"
    found = rig.store.approval(approval)
    assert found is not None and found["detail"] == "Home Assistant now offers 17.3 → 17.5 instead"
    assert outcome_of(rig, approval) == "void::Home Assistant now offers 17.3 → 17.5 instead"
    assert gone["attributes"]["latest_version"] == "17.4"


async def test_review_7_a_missing_update_at_the_nightly_plan_is_voided_with_the_reason(os_rig: Rig) -> None:
    rig = os_rig
    approval, _ = await approve(rig, OS)
    rig.ops.entities.pop(OS)
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    found = rig.store.approval(approval)
    assert found is not None and found["state"] == "void"
    assert (
        found["detail"] == "Home Assistant did not offer this update at the nightly plan (installed, skipped or gone)"
    )


async def test_an_approved_reboot_runs_last_in_a_run_of_its_own(os_rig: Rig) -> None:
    """The approved reboot runs after the night's plan and installs, in a one-job run; the night it
    was installed in stays finished (never set back to installing, never re-run)."""
    rig = os_rig
    rig.ops.reboot_ignored = True
    await approved_night(rig)
    await rig.manager.run_window()
    job = rig.jobs()[OS]
    night = int(rig.run()["id"])
    assert job.state == "reboot_pending" and rig.store.update_run(night)["state"] == "done"  # type: ignore[index]
    found = rig.manager.request_reboot(job.id, user=ADMIN)
    assert await rig.manager.confirm_reboot(found["id"], ADMIN) == "approved"
    # The next night a patch is due too: it installs first, then the reboot.
    rig.ops.reboot_ignored = False
    rig.ops.entities[BROKER] = entity("4.2.1", "4.2.2", 29, "Example Broker")
    rig.now += timedelta(days=1)
    await rig.manager.run_window()
    calls = [c[0] for c in rig.ops.calls if c[0] in ("update_addon", "reboot_host")]
    assert calls[-2:] == ["update_addon", "reboot_host"]
    runs = rig.store.recent_update_runs(limit=10)
    reboot = next(r for r in runs if r["trigger"] == "reboot")
    assert [j.entity_id for j in rig.store.update_jobs(int(reboot["id"]))] == [OS]
    assert reboot["state"] == "done" and rig.store.update_job(job.id).state == "succeeded"  # type: ignore[union-attr]
    assert rig.store.update_run(night)["state"] == "done"  # type: ignore[index]
    assert OS not in [j.entity_id for j in rig.store.update_jobs(night)]
