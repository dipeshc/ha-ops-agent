"""Complete backups of any type, unencrypted-backup guard, "Back up now" and the act-mode self-heal."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd.backups import LAST_KEY, SELF_HEAL_KEY, Backups
from opsd.baseline import Baseline, diff
from opsd.config import Options
from opsd.health import Snapshot, derive_backups
from opsd.store import Store
from opsd.updexec import Executors
from tests.conftest import make_options
from tests.test_daemon import Harness, h  # noqa: F401
from tests.test_panel import Panel, local_ingress, p  # noqa: F401

NOW = datetime(2026, 1, 28, 12, 0, tzinfo=UTC)
ADDONS = ["core_mosquitto", "local_ha_ops_agent"]


def core_record(backup_id: str, hours: float, *, addons: list[str] = ADDONS, database: bool = True,
                protected: bool = True,
                agents: tuple[str, ...] = ("hassio.local", "cloud.cloud")) -> dict[str, Any]:  # fmt: skip
    when = (NOW - timedelta(hours=hours)).isoformat()
    return {"backup_id": backup_id, "name": f"Automatic backup {backup_id}", "date": when,
            "with_automatic_settings": True, "homeassistant_included": True, "homeassistant_version": "2026.10.4",
            "database_included": database, "addons": [{"slug": a} for a in addons], "folders": [],
            "agents": {a: {"protected": protected, "size": 1} for a in agents}, "failed_agent_ids": []}  # fmt: skip


def supervisor_backup(slug: str, hours: float, *, protected: bool = True, addons: list[str] = ADDONS,
                      kind: str = "partial") -> dict[str, Any]:  # fmt: skip
    content = {"homeassistant": True, "addons": addons, "folders": ["share"]}
    return {"slug": slug, "name": f"backup {slug}", "date": (NOW - timedelta(hours=hours)).isoformat(), "type": kind,
            "protected": protected, "content": content}  # fmt: skip


# ---------------------------------------------------------------- 1. what counts


def test_an_encrypted_automatic_partial_backup_is_complete() -> None:
    manager = {"backups": [core_record("auto1", 5)], "next_automatic_backup": "2026-01-29T04:45:00+00:00"}
    out = derive_backups([supervisor_backup("auto1", 5)], NOW, addons=ADDONS, manager=manager)
    assert out["newest_complete_name"] == "backup auto1" and out["newest_complete_age_h"] == 5.0
    assert out["newest_complete_encrypted"] is True and out["unprotected"] == []
    assert out["automatic"]["configured"] is True


@pytest.mark.parametrize(
    "record",
    [
        core_record("x", 5, addons=["core_mosquitto"]),  # an add-on missing
        core_record("x", 5, database=False),  # no database
        {**core_record("x", 5), "homeassistant_included": False},
    ],
)
def test_a_backup_missing_anything_is_not_complete(record: dict[str, Any]) -> None:
    out = derive_backups([supervisor_backup("x", 5)], NOW, addons=ADDONS, manager={"backups": [record]})
    assert out["newest_complete_name"] is None and out["newest_any_age_h"] == 5.0


def test_without_core_the_supervisor_content_decides() -> None:
    complete = derive_backups([supervisor_backup("a", 30)], NOW, addons=ADDONS)
    assert complete["newest_complete_age_h"] == 30.0
    partial = derive_backups([supervisor_backup("b", 3, addons=[])], NOW, addons=ADDONS)
    assert partial["newest_complete_name"] is None
    no_db = derive_backups([{**supervisor_backup("c", 3), "homeassistant_exclude_database": True}], NOW,
                           addons=ADDONS)  # fmt: skip
    assert no_db["newest_complete_name"] is None


def test_a_cloud_only_backup_counts_and_unencrypted_local_ones_are_named() -> None:
    manager = {"backups": [core_record("cloud1", 2, agents=("cloud.cloud",))]}
    local = [supervisor_backup("old", 100, protected=False, kind="full")]
    out = derive_backups(local, NOW, addons=ADDONS, manager=manager)
    assert out["newest_complete_name"] == "Automatic backup cloud1" and out["newest_complete_age_h"] == 2.0
    assert out["unprotected"] == ["backup old"] and out["unprotected_count"] == 1


def test_backup_items_name_what_is_wrong_and_never_contents() -> None:
    snapshot = Snapshot(taken_at=NOW.isoformat(), backups=derive_backups(
        [supervisor_backup("plain", 200, protected=False, kind="full")], NOW, addons=ADDONS))  # fmt: skip
    items = {i.key: i for i in diff(snapshot, Baseline(created_at=NOW.isoformat()), NOW,
                                    backup_failure="the backup did not complete")}  # fmt: skip
    assert "is 200.0 h old (more than 7 days)" in items["backup:complete_stale"].summary
    assert items["backup:stale"].summary == "newest backup of any kind is 200.0 h old"
    assert items["backup:unprotected"].summary == "1 unencrypted backup(s) on /backup: backup plain"
    assert items["backup:unprotected"].detail == {"names": ["backup plain"]}
    assert "did not complete" in items["backup:run_failed"].summary
    fresh = Snapshot(taken_at=NOW.isoformat(), backups=derive_backups([supervisor_backup("a", 2)], NOW, addons=ADDONS))
    assert not [i for i in diff(fresh, Baseline(created_at=NOW.isoformat()), NOW) if i.kind == "backup"]


# ---------------------------------------------------------------- 2. the run itself


class FakeOps:
    def __init__(self, *, finishes: bool = True, protected: bool = True) -> None:
        self.records = [core_record("old", 50)]
        self.finishes = finishes
        self.protected = protected
        self.created = 0

    async def create_backup(self) -> str | None:
        self.created += 1
        if self.finishes:
            self.records.append(core_record(f"new{self.created}", 0, protected=self.protected))
        return "job"

    async def backup_manager(self) -> dict[str, Any]:
        return {"state": "idle"}

    async def core_backups(self) -> list[dict[str, Any]]:
        return list(self.records)

    async def installed_addons(self) -> list[str]:
        return list(ADDONS)


def service(tmp_path: Path, options: Options, ops: FakeOps | None = None) -> tuple[Backups, Store, list[Any]]:
    store = Store.open(tmp_path / "ops.db")
    clock = [NOW]
    ops = ops or FakeOps()

    async def sleep(_: float) -> None:
        clock[0] += timedelta(seconds=30)

    executors = Executors(ops=ops, store=store, options=options, clock=lambda: clock[0])  # type: ignore[arg-type]
    backups = Backups(ops=ops, executors=executors, store=store, clock=lambda: clock[0], sleep=sleep)  # type: ignore[arg-type]
    notes: list[Any] = []
    backups.activity = lambda *a, **kw: notes.append((a, kw))
    return backups, store, notes


async def test_back_up_now_works_in_observe_mode_and_is_verified(tmp_path: Path) -> None:
    backups, store, notes = service(tmp_path, Options())
    record = await backups.run(by="panel:u-admin", admin=True)
    assert record["state"] == "ok" and record["name"] == "Automatic backup new1"
    assert backups.last()["state"] == "ok" and notes[0][0][1] == "Backup (Back up now): done"
    audit = [r for r in store.audit_rows(limit=10) if r["capability"] == "backup.create"]
    assert audit and audit[0]["outcome"] == "ok" and json.loads(audit[0]["args"])["by"] == "admin"
    # One start an hour.
    assert backups.next_manual_at() is not None
    store.close()


async def test_an_unencrypted_or_missing_result_fails_and_is_reported(tmp_path: Path) -> None:
    backups, store, notes = service(tmp_path, Options(), FakeOps(protected=False))
    record = await backups.run(by="panel:u-admin", admin=True)
    assert record["state"] == "failed" and "not encrypted" in record["detail"]
    assert notes[0][1]["needs_admin"] is True and backups.recent_failure() is not None
    store.close()


async def test_the_agent_cannot_back_up_in_observe_mode(tmp_path: Path) -> None:
    backups, store, _ = service(tmp_path, Options())
    record = await backups.run(by="agent", admin=False)
    assert record["state"] == "failed" and "observe mode" in record["detail"]
    store.close()


def test_self_heal_is_due_only_when_everything_says_so(tmp_path: Path) -> None:
    backups, store, _ = service(tmp_path, make_options(agent__mode="act"))
    stale = {"newest_complete_age_h": 30.0, "automatic": {"configured": True}}
    assert backups.self_heal_due(stale, acting=True)
    assert not backups.self_heal_due(stale, acting=False)  # observe: report only
    assert not backups.self_heal_due({**stale, "automatic": {"configured": False}}, acting=True)
    assert not backups.self_heal_due({**stale, "newest_complete_age_h": 20.0}, acting=True)
    assert backups.self_heal_due({**stale, "newest_complete_age_h": None}, acting=True)
    store.kv_set(SELF_HEAL_KEY, (NOW - timedelta(hours=3)).isoformat(), now=NOW)
    assert not backups.self_heal_due(stale, acting=True)  # once a day
    store.close()


# ---------------------------------------------------------------- 2. the panel


def install_ops(d: Any, ops: FakeOps) -> None:
    assert d.updates is not None and d.backups is not None
    for name in ("create_backup", "backup_manager", "core_backups", "installed_addons"):
        setattr(d.updates.ops, name, getattr(ops, name))

    clock = [datetime.now(UTC)]

    async def no_wait(_: float) -> None:
        clock[0] += timedelta(seconds=30)

    d.backups._sleep = no_wait
    d.backups._clock = lambda: clock[0]


async def test_back_up_now_in_the_panel_runs_directly_and_is_rate_limited(p: Panel, h: Harness) -> None:  # noqa: F811
    ops = FakeOps()
    install_ops(p.d, ops)
    before = len(h.pushes())
    status, _, body = await p.request("POST", "/api/backups/now", body={})
    assert status == 200 and body["ok"]
    await p.d._backup_task
    assert ops.created == 1 and len(h.pushes()) == before  # the admin's own backup: no push
    status, _, view = await p.request("GET", "/api/updates")
    assert view["backup"]["last"]["state"] == "ok" and view["backup"]["next_at"]
    status, _, body = await p.request("POST", "/api/backups/now", body={})
    assert status == 429 and body["message"] == "Latest backup 0 min ago."
    assert view["backup"]["blocked"] == "Latest backup 0 min ago."
    assert (await p.request("POST", "/api/backups/now", body={}, csrf=False))[0] == 403


# ---------------------------------------------------------------- 3. self-heal


async def test_act_mode_heals_a_missing_backup_once_and_tells_the_admin(h: Harness) -> None:  # noqa: F811
    h.fake.backup_info = {"backups": [], "next_automatic_backup": "2026-01-29T04:45:00+00:00", "state": "idle"}
    d = h.daemon(agent__mode="act")
    ops = FakeOps()
    install_ops(d, ops)
    await d.health_cycle("catch_up")
    assert d._backup_task is not None
    await d._backup_task
    assert ops.created == 1
    # A backup made is recorded silently (an item in History), never pushed.
    await d.items_flush()
    assert not [x for x in h.pushes() if "backup" in x["title"].lower()]
    assert d.store is not None
    assert any(i["kind"] == "backup" and i["state"] == "done" for i in d.store.items_all(prefix="activity:"))
    assert d.store is not None and json.loads(d.store.kv_get(LAST_KEY))["by"] == "agent"
    # Not again within 24 hours.
    await d.health_cycle("daily")
    assert ops.created == 1


async def test_observe_mode_never_heals(h: Harness) -> None:  # noqa: F811
    h.fake.backup_info = {"backups": [], "next_automatic_backup": "2026-01-29T04:45:00+00:00", "state": "idle"}
    d = h.daemon()
    ops = FakeOps()
    install_ops(d, ops)
    await d.health_cycle("catch_up")
    assert d._backup_task is None and ops.created == 0


async def test_a_failed_self_heal_pushes_and_raises_an_item(h: Harness) -> None:  # noqa: F811
    h.fake.backup_info = {"backups": [], "next_automatic_backup": "2026-01-29T04:45:00+00:00", "state": "idle"}
    d = h.daemon(agent__mode="act")
    install_ops(d, FakeOps(finishes=False))
    await d.health_cycle("catch_up")
    assert d._backup_task is not None
    await d._backup_task
    # A failed one is an item in Needs you, pushed once with the item as its tag.
    await d.items_flush()
    assert d.store is not None
    [item] = [i for i in d.store.items_all(prefix="activity:") if i["kind"] == "backup"]
    assert item["state"] == "failed" and item["needs"]
    [push] = [x for x in h.pushes() if x["title"] == "Ops Agent: backup"]
    assert push["data"]["tag"] == item["id"]
    outcome = await d.health_cycle("daily")
    assert outcome is not None
    assert "backup:run_failed" in {i.key for i in d.open_items()}


# ---------------------------------------------------------------- 2. chat proposal


async def test_chat_can_ask_for_a_backup_but_never_runs_one(h: Harness) -> None:  # noqa: F811
    from tests.test_full_chat import ask, daemon, turns

    d = daemon(h)
    ops = FakeOps()
    install_ops(d, ops)
    h.in_run_requests = [{"verb": "request-approval", "args": {"entity": "backup"}}]
    h.claude_reply = {"reply": "I asked you to back up now.", "summary": "asked"}
    thread = await ask(d, "can you back up now?")
    [asked] = h.in_run_responses
    assert asked["ok"] and "Nothing runs until the admin taps Back up now" in asked["output"]
    assert ops.created == 0 and d.store is not None
    [proposal] = d.store.proposals()
    assert proposal["entity_id"] == "backup.now"
    card = turns(d, thread)[-1]
    assert card.status == "proposal" and card.meta["actions"] == ["backup"] and card.text.startswith("Back up now?")
    # A finished backup closes the request.
    assert d.start_backup(by="panel:u-admin", admin=True)
    assert d._backup_task is not None
    await d._backup_task
    assert d.store.proposals() == []


async def test_at_most_three_standalone_backups_a_day(tmp_path: Path) -> None:
    from opsd.backups import DAILY_CAP

    backups, store, _ = service(tmp_path, make_options(agent__mode="act"))
    for _ in range(DAILY_CAP):
        assert (await backups.run(by="panel:u-admin", admin=True))["state"] == "ok"
    record = await backups.run(by="agent", admin=False)
    assert record["state"] == "capped" and backups.capped()
    assert "At most 3 backups a day" in str(backups.manual_refusal())
    stale = {"newest_complete_age_h": 30.0, "automatic": {"configured": True}}
    assert not backups.self_heal_due(stale, acting=True)  # the self-heal counts too
    store.close()


def test_a_fresh_complete_backup_blocks_back_up_now(tmp_path: Path) -> None:
    backups, store, _ = service(tmp_path, Options())
    assert backups.manual_refusal() is None
    backups.observe({"newest_complete_age_h": 0.5}, NOW)
    assert backups.fresh_minutes() == 30 and backups.manual_refusal() == "Latest backup 30 min ago."
    assert backups.view()["blocked"] == "Latest backup 30 min ago."
    backups.observe({"newest_complete_age_h": 4.0}, NOW)  # older news never moves it back
    assert backups.fresh_minutes() == 30
    store.close()
    backups, store, _ = service(tmp_path / "b", Options())
    backups.observe({"newest_complete_age_h": 3.5}, NOW)
    assert backups.fresh_minutes() is None and backups.manual_refusal() is None
    store.close()
