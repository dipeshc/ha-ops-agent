"""The upgrade from 0.18's store (schema 18) to this version's, on a realistic fixture database.

The schema-18 store is built with the frozen 0.18 migrations (`tests/fixtures/schema_v18.py`), exactly
as installs made it, and holds a row of every kind the agent keeps: an open approval bound to its plan, a
pending request, change and house fact, a running update job, held pushes, push action ids, reminders, the
chat, budget runs (one still running), counters, kv state, items with their events, the audit log and the
reload rate file. After the upgrade every one of them is still there, and every open one still works.
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import reload as reload_mod
from opsd import store as store_mod
from opsd.approvals import Approvals, plan_hash
from opsd.budget import Period
from opsd.store import SCHEMA_VERSION, Store, StoreUnavailable
from tests.fixtures.schema_v18 import MIGRATIONS_V18

NOW = datetime.now(UTC).replace(microsecond=0)
DAY = NOW.date().isoformat()
WEATHER = "update.acme_weather_update"
CHAT = "0123456789abcdef"
ADMIN = "u-admin"
PLAN_ITEM = {"entity_id": WEATHER, "kind": "hacs", "title": "Acme Weather", "installed": "v2.4.0", "latest": "v2.4.1",
             "tier": "approve", "bump": "patch", "reason": "a HACS integration", "review": None,
             "held": False}  # fmt: skip


def at(minutes: int = 0) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def v18_store(path: Path) -> None:
    """A store as 0.18 left it, with a row of every kind."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    for script in MIGRATIONS_V18:
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)
    conn.execute("PRAGMA user_version = 18")
    sql = conn.execute
    # The one chat: its thread, read mark and turns (an admin turn with its client id, a reply, a summary).
    sql("INSERT INTO kv VALUES ('chat.main_thread', ?, ?)", (CHAT, at(-600)))
    sql("INSERT INTO threads (id, principal, channel, created_at, updated_at, turns, last_read_turn_id)"
        " VALUES (?, 'admin', 'panel', ?, ?, 3, 1)", (CHAT, at(-600), at(-5)))  # fmt: skip
    sql("INSERT INTO turns (id, thread_id, role, text, at, status, meta, client_id) VALUES"
        " (1, ?, 'admin', 'is the weather update safe?', ?, 'ok', '{\"channel\": \"panel\"}', 'client-0001')",
        (CHAT, at(-10)))  # fmt: skip
    sql("INSERT INTO turns (id, thread_id, role, text, at, run_id, reply_to) VALUES"
        " (2, ?, 'agent', 'It is a patch; I asked you to approve it.', ?, 'run-chat-1', 1)",
        (CHAT, at(-9)))  # fmt: skip
    sql("INSERT INTO turns (id, thread_id, role, text, at, status) VALUES (3, ?, 'system', 'summary', ?, 'summary')",
        (CHAT, at(-5)))  # fmt: skip
    # The plan the approval is bound to, and a night that is installing right now.
    sql("INSERT INTO update_runs (id, window_day, trigger, mode, state, plan, created_at, updated_at) VALUES"
        " (4, ?, 'window', 'act', 'installing', ?, ?, ?)", (DAY, json.dumps([PLAN_ITEM]), at(-60), at(-2)))  # fmt: skip
    sql("INSERT INTO update_jobs (id, run_id, seq, entity_id, kind, title, from_version, to_version, target, state,"
        " install_attempts, idempotency_key, started_at, updated_at) VALUES (9, 4, 0, 'update.acme_solar_update',"
        " 'hacs', 'Acme Solar', '1.0.0', '1.0.1', '{\"approval\": null}', 'installing', 1, '4:solar:1.0.0:1.0.1',"
        " ?, ?)", (at(-3), at(-2)))  # fmt: skip
    # An open approval bound to that plan (pending, so the admin can still approve it).
    created = at(-30)
    approval = {"entity_id": WEATHER, "from": "v2.4.0", "to": "v2.4.1", "plan": 4, "kind": "hacs",
                "title": "Acme Weather", "tier": "approve", "reason": "a HACS integration"}  # fmt: skip
    sql("INSERT INTO approvals (id, plan, plan_hash, requested_by, state, created_at, expires_at, subject, run_when)"
        " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, 'window')",
        ("a" * 32, json.dumps(approval, sort_keys=True), plan_hash(PLAN_ITEM, 4, created), ADMIN, created,
         at(60 * 70), f"{WEATHER}@v2.4.1"))  # fmt: skip
    sql("INSERT INTO approvals (id, plan, plan_hash, requested_by, state, created_at, expires_at, subject,"
        " used_by_job, decided_at, decided_by) VALUES (?, '{}', 'h', ?, 'used', ?, ?, 'update.old@1', 3, ?, ?)",
        ("b" * 32, ADMIN, at(-9000), at(-5000), at(-8000), ADMIN))  # fmt: skip
    # A pending request (the chat asked), a change proposal and a house fact.
    sql("INSERT INTO approval_proposals (id, entity_id, from_version, to_version, plan_run, title, thread_id, run_id,"
        " requested_by, state, created_at, expires_at) VALUES (5, ?, 'v2.4.0', 'v2.4.1', 4, 'Acme Weather', ?,"
        " 'run-chat-1', 'admin-chat:u-admin', 'open', ?, ?)", (WEATHER, CHAT, at(-9), at(60 * 71)))  # fmt: skip
    files = [{"path": "automations/porch.yaml", "old_sha256": "1" * 64, "new_sha256": "2" * 64}]
    sql("INSERT INTO change_proposals (id, kind, subject, why, effect, files, summary, diff, base_sha, commit_sha,"
        " ref, state, requested_by, thread_id, run_id, fact_id, created_at, expires_at, updated_at) VALUES (7,"
        " 'change', 'fix(automations): porch light off at dawn', 'it stays on', 'turns off at sunrise', ?, '[]',"
        " '-a\n+b', ?, ?, 'refs/ops/proposals/7', 'open', 'admin-chat:u-admin', ?, 'run-chat-1', NULL, ?, ?, ?)",
        (json.dumps(files), "c" * 40, "d" * 40, CHAT, at(-20), at(60 * 70), at(-20)))  # fmt: skip
    sql("INSERT INTO fact_proposals (id, text, thread_id, turn_id, created_at, status) VALUES"
        " (2, 'the cellar pump runs at night', ?, 1, ?, 'pending')", (CHAT, at(-8)))  # fmt: skip
    # Reminders, a system task and a chat retry.
    task = ("INSERT INTO tasks (id, kind, title, owner, created_by, system_key, payload, state, next_fire_at,"
            " thread_id, idempotency_key, created_at, updated_at) VALUES (?, ?, ?, 'admin', ?, ?, ?, 'pending', ?, ?,"
            " ?, ?, ?)")  # fmt: skip
    sql(task, (11, "reminder", "put the bins out", "chat:run-chat-1", None, '{"note": "blue bin"}', at(60), CHAT,
               "k-bins", at(-50), at(-50)))  # fmt: skip
    sql(task, (12, "recurring", "Daily health check", "daemon", "health.check", "{}", at(300), None, None, at(-600),
               at(-600)))  # fmt: skip
    sql(task, (13, "retry", "Retry message #1", "daemon", "retry:1", '{"turn_id": 1, "attempt": 1}', at(15), CHAT,
               "retry:1:1", at(-1), at(-1)))  # fmt: skip
    # Pushes held for after quiet hours, push action ids (one unused, one used).
    sql("INSERT INTO held_pushes (id, held_at, message, title) VALUES (3, ?, ?, 'Reminder: bins')",
        (at(-120), json.dumps({"title": "Reminder: bins", "body": "blue bin"})))  # fmt: skip
    sql("INSERT INTO held_pushes (id, held_at, message, title, state, outcome, settled_at) VALUES"
        " (2, ?, '{}', 'Old', 'undelivered', 'rate_limited', ?)", (at(-300), at(-200)))  # fmt: skip
    sql("INSERT INTO issued_actions VALUES (?, 'REPLY', 'ha-ops-agent', ?, ?, ?, NULL)",
        ("OPS_REPLY_" + "e" * 32, CHAT, at(-10), at(60 * 24)))  # fmt: skip
    sql("INSERT INTO issued_actions VALUES (?, 'DONE', 't', NULL, ?, ?, ?)",
        ("OPS_DONE_" + "f" * 32, at(-10), at(60 * 24), at(-5)))  # fmt: skip
    # The budget ledger: a finished run and one still running (its reservation counts).
    run = ("INSERT INTO runs (run_id, profile, class, day, week, month, started_at, ended_at, status, reserved_turns,"
           " reserved_tokens, reserved_usd, turns, tokens, cost_usd, reserved_seconds, seconds, reserved_share,"
           " share_5h, share_7d) VALUES (?, ?, 'agent', ?, 'w', 'm', ?, ?, ?, 15, 0, 0.5, ?, ?, ?, 180, ?, 0.01, ?,"
           " ?)")  # fmt: skip
    sql(run, ("run-chat-1", "chat", DAY, at(-9), at(-8), "ok", 3, 1200, 0.02, 40.0, 0.002, 0.0004))
    sql(run, ("run-triage-2", "triage", DAY, at(-1), None, "running", None, None, None, None, None, None))
    # Daily counters, kv state (an ack, a pause flag, a stale backfill marker, a panel confirmation).
    sql("INSERT INTO counters VALUES (?, 'chat_turns', 4), (?, 'cap:integration.reload', 1)", (DAY, DAY))
    for key, value in (("item.ack:change:6", "change:6:x"), ("updates.paused", "0"), ("items.backfill", at(-600)),
                       ("confirm:abc", "{}")):  # fmt: skip
        sql("INSERT INTO kv VALUES (?, ?, ?)", (key, value, at(-60)))
    # An activity entry, its standalone item and event; a waiting change item with its push record.
    sql("INSERT INTO activity (id, at, kind, title, detail, needs_admin, ref, item) VALUES"
        " (41, ?, 'reload', 'Reloaded acme_vacuum', 'ok', 0, '{}', 'a-41')", (at(-30),))  # fmt: skip
    item = ("INSERT INTO items (id, key, kind, title, state, gist, need, actions, source, quiet, revision, as_of, at,"
            " needs_since, resolved_at, created_at, updated_at, derived, push) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")  # fmt: skip
    sql(item, ("a-41", "activity:41", "reload", "Reloaded acme_vacuum", "done", "ok", None, '{"activity": 41}', 1, 1,
               at(-30), at(-30), None, at(-30), at(-30), at(-30), 0, "{}"))  # fmt: skip
    need = {"kind": "approval", "text": "Change #7 ...", "primary": {"id": "apply", "label": "Apply and commit"},
            "secondary": [], "expires_at": at(60 * 70)}  # fmt: skip
    sql(item, ("p-000000000007", "change:7", "proposal", "fix(automations): porch light off at dawn", "waiting",
               "waiting for your approval", json.dumps(need), '{"change": 7}', 0, 2, at(-20), at(-20), at(-20), None,
               at(-20), at(-20), 1, json.dumps({"key": "waiting:approval:apply", "at": at(-19)})))  # fmt: skip
    for text, revision in (("Needs you: waiting for your approval", 1), ("Approved by you", 2)):
        sql("INSERT INTO item_events (item_id, at, actor, text, revision) VALUES ('p-000000000007', ?, 'daemon', ?, ?)",
            (at(-20), text, revision))  # fmt: skip
    sql("INSERT INTO audit (at, principal, channel, capability, args, decision, outcome) VALUES"
        " (?, 'agent-self', 'opsctl', 'task.create', '{}', 'execute', 'ok')", (at(-50),))  # fmt: skip
    conn.close()
    # 0.18's reload rate file: acme_vacuum was reloaded five minutes ago.
    (path.parent / "reloads.json").write_text(json.dumps({"acme_vacuum": at(-5)}))


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}  # noqa: S608
    finally:
        conn.close()


def _sql(text: str) -> str:
    """A schema statement without what only its formatting decides (spacing, quotes, `IF NOT EXISTS`)."""
    text = " ".join(text.split()).replace("IF NOT EXISTS ", "").replace('"', "")
    return re.sub(r" \)", ")", re.sub(r"\( ", "(", text))


def shape(store: Store) -> dict[str, Any]:
    """Everything the schema says: each statement (CHECKs, defaults, generated columns), columns, foreign keys
    and index columns, per table and index."""
    conn = store._db()
    names = [str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {
        "sql": {
            (str(r[0]), str(r[1])): _sql(str(r[2]))
            for r in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        },
        "columns": {n: [tuple(c) for c in conn.execute(f"PRAGMA table_xinfo({n})")] for n in names},
        "foreign_keys": {n: [tuple(k) for k in conn.execute(f"PRAGMA foreign_key_list({n})")] for n in names},
        "indexes": {
            n: sorted(
                (
                    str(i[1]),
                    int(i[2]),
                    int(i[4]),
                    tuple(str(c[2]) for c in conn.execute(f"PRAGMA index_xinfo({i[1]})") if c[5]),
                )
                for i in conn.execute(f"PRAGMA index_list({n})")
            )
            for n in names
        },
    }


@pytest.fixture
def upgraded(tmp_path: Path) -> Iterator[Store]:
    path = tmp_path / "ops.db"
    v18_store(path)
    store = Store.open(path)
    yield store
    store.close()


def test_the_upgrade_makes_the_same_schema_as_a_fresh_install(upgraded: Store, tmp_path: Path) -> None:
    fresh = Store.open(tmp_path / "fresh.db")
    try:
        assert upgraded.schema_version() == fresh.schema_version() == SCHEMA_VERSION
        assert shape(upgraded) == shape(fresh)
        assert sorted(shape(fresh)["columns"]) == ["approvals", "audit", "item_events", "items", "kv", "runs", "tasks",
                                           "turns", "update_jobs", "update_runs"]  # fmt: skip
    finally:
        fresh.close()


def test_an_open_approval_is_still_bound_and_usable_once(upgraded: Store) -> None:
    approvals = Approvals(upgraded, clock=lambda: NOW)
    found = upgraded.approval("a" * 32)
    assert found is not None and found["state"] == "pending" and found["subject"] == f"{WEATHER}@v2.4.1"
    assert found["plan"]["plan"] == 4 and found["requested_by"] == ADMIN
    # The binding is recomputed from the plan row exactly as before the upgrade.
    assert approvals.bound(found)
    assert upgraded.decide_approval("a" * 32, expect=("pending",), to="approved", now=NOW, by=ADMIN)
    assert approvals.consume("a" * 32, 9) and not approvals.consume("a" * 32, 9)  # single use survives
    used = upgraded.approval("b" * 32)
    assert used is not None and used["state"] == "used" and used["used_by_job"] == 3
    # Nothing reads a request, a change or a fact as an approval.
    assert {a["id"] for a in upgraded.approvals(limit=50)} == {"a" * 32, "b" * 32}


def test_every_pending_proposal_kind_is_kept_and_still_actionable(upgraded: Store) -> None:
    [request] = upgraded.proposals()
    assert (request["id"], request["entity_id"], request["to_version"], request["plan_run"]) == (
        5,
        WEATHER,
        "v2.4.1",
        4,
    )
    assert request["thread_id"] == CHAT and request["run_id"] == "run-chat-1"
    assert upgraded.settle_proposal(5, to="done", now=NOW) and not upgraded.proposals()
    change = upgraded.change(7)
    assert change is not None and change["state"] == "open" and change["kind"] == "change"
    assert change["files"][0]["new_sha256"] == "2" * 64 and change["diff"] == "-a\n+b"
    assert (change["base_sha"], change["commit_sha"], change["ref"]) == ("c" * 40, "d" * 40, "refs/ops/proposals/7")
    assert change["why"] == "it stays on" and change["subject"].startswith("fix(automations)")
    assert [c["id"] for c in upgraded.changes(states=("open",))] == [7]
    assert upgraded.move_change(7, expect=("open",), to="applying", now=NOW)
    assert upgraded.create_change(kind="facts", subject="s", why="", effect="", state="open", requested_by="x",
                                  now=NOW, expires_at=NOW)["id"] == 8  # fmt: skip
    [fact] = upgraded.list_facts(now=NOW)
    assert (fact["id"], fact["status"], fact["turn_id"]) == (2, "pending", 1) and "cellar" in fact["text"]
    assert upgraded.decide_fact(2, "rejected", now=NOW)


def test_the_running_update_job_and_its_night_carry_on(upgraded: Store) -> None:
    job = upgraded.update_job(9)
    assert job is not None and job.state == "installing" and job.install_attempts == 1
    active = upgraded.active_update_run()
    assert active is not None and active["id"] == 4 and active["plan"] == [PLAN_ITEM]
    assert upgraded.transition_job(9, expect=("installing",), to="verifying", now=NOW)
    assert upgraded.start_update_run(window_day=DAY, trigger="manual", mode="report", plan=[], now=NOW) is None


def test_held_pushes_actions_reminders_and_retries_survive(upgraded: Store) -> None:
    [held] = upgraded.held_pushes()
    assert held["id"] == 3 and json.loads(held["message"])["body"] == "blue bin"
    [missed] = upgraded.held_pushes(states=("undelivered",))
    assert missed["outcome"] == "rate_limited"
    upgraded.settle_held([3], to="delivered", outcome="sent", now=NOW)
    assert upgraded.held_pushes() == [] and upgraded.hold_push("{}", title="next", now=NOW) == 4
    reply = upgraded.consume_action("OPS_REPLY_" + "e" * 32, now=NOW)
    assert reply is not None and reply["thread_id"] == CHAT and reply["kind"] == "REPLY"
    assert upgraded.consume_action("OPS_REPLY_" + "e" * 32, now=NOW) is None
    assert upgraded.consume_action("OPS_DONE_" + "f" * 32, now=NOW) is None  # it was used before the upgrade
    [bins] = [t for t in upgraded.open_tasks() if t.kind == "reminder"]
    assert bins.id == 11 and bins.payload == {"note": "blue bin"} and bins.thread_id == CHAT
    assert upgraded.system_task("health.check") is not None
    retry = upgraded.system_task("retry:1")
    assert retry is not None and retry.state == "pending" and retry.payload["attempt"] == 1
    claimed = upgraded.claim_due(NOW + timedelta(hours=2))
    assert {t.id for t in claimed} == {11, 13}


def test_the_chat_its_read_mark_and_its_turns_survive(upgraded: Store) -> None:
    assert upgraded.chat_id() == CHAT
    thread = upgraded.get_thread(CHAT)
    assert thread is not None and thread["turns"] == 3 and thread["unread"] and not thread["in_flight"]
    turns = upgraded.turns_page(CHAT)
    assert [(t.id, t.role) for t in turns] == [(1, "admin"), (2, "agent"), (3, "system")]
    assert turns[0].meta == {"channel": "panel"} and turns[1].reply_to == 1 and turns[1].thread_id == CHAT
    found = upgraded.turn_by_client(CHAT, "client-0001")
    assert found is not None and found.id == 1
    latest = upgraded.latest_summary(CHAT)
    assert latest is not None and latest.id == 3
    assert upgraded.mark_read(CHAT) and not upgraded.get_thread(CHAT)["unread"]  # type: ignore[index]
    assert upgraded.append_turn(CHAT, role="admin", text="next", now=NOW).id == 4


def test_the_budget_ledger_counters_and_kv_survive(upgraded: Store) -> None:
    usage = upgraded.usage(Period.at(NOW, UTC), at=NOW)
    # The finished run counts what it used, the running one its reservation.
    assert usage["day"]["runs"] == 2 and usage["day"]["turns"] == 3 + 15 and usage["day"]["seconds"] == 40 + 180
    assert usage["profiles"]["chat"]["tokens"] == 1200 and "usd" not in usage["day"]
    assert upgraded.close_stale_runs(at=NOW) == 1
    assert upgraded.count(DAY, "chat_turns") == 4 and upgraded.count(DAY, "cap:integration.reload") == 1
    assert upgraded.bump(DAY, "chat_turns", limit=5) and not upgraded.bump(DAY, "chat_turns", limit=5)
    assert upgraded.kv_get("item.ack:change:6") == "change:6:x" and upgraded.kv_get("updates.paused") == "0"
    assert upgraded.kv_get("items.backfill") == "" and upgraded.kv_get("confirm:abc") == ""


def test_items_events_and_the_audit_survive(upgraded: Store) -> None:
    change = upgraded.item("p-000000000007")
    assert change is not None and change["state"] == "waiting" and change["needs"] and change["revision"] == 2
    assert change["need"]["primary"]["id"] == "apply" and change["push"]["key"] == "waiting:approval:apply"
    assert "derived" not in change and "confirm" not in change
    assert [e["text"] for e in upgraded.item_events("p-000000000007")][-1] == "Approved by you"
    assert upgraded.needs_count() == 1
    entry = upgraded.item("a-41")
    assert entry is not None and entry["quiet"] and entry["state"] == "done"
    # New standalone entries never reuse an old entry's number.
    assert upgraded.next_activity() == 42
    assert [r["capability"] for r in upgraded.audit_rows()] == ["task.create"]


def test_the_reload_rate_moves_into_the_store(upgraded: Store, tmp_path: Path) -> None:
    assert not (tmp_path / "reloads.json").exists()
    refusal = reload_mod.rate_refusal(upgraded, "acme_vacuum", NOW)
    assert refusal is not None and "less than 10 minutes" in refusal
    assert reload_mod.rate_refusal(upgraded, "kasa", NOW) is None


def test_a_corrupt_reload_file_still_refuses_after_the_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    v18_store(path)
    (tmp_path / "reloads.json").write_text("{not json")
    store = Store.open(path)
    try:
        with pytest.raises(reload_mod.CapabilityError, match="reload state"):
            reload_mod.rate_refusal(store, "acme_vacuum", NOW)
    finally:
        store.close()


@pytest.mark.parametrize("where", ["copy", "indexes", "import"])
def test_a_failure_midway_leaves_the_store_at_18_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           where: str) -> None:  # fmt: skip
    path = tmp_path / "ops.db"
    v18_store(path)
    before = counts(path)
    if where == "copy":
        broken = store_mod.UPGRADE_18 + "; INSERT INTO no_such_table VALUES (1)"  # noqa: S608 - a constant
        monkeypatch.setattr(store_mod, "UPGRADE_18", broken)
    elif where == "indexes":
        # After every copy and every drop: the whole upgrade still rolls back.
        monkeypatch.setattr(store_mod, "INDEXES", store_mod.INDEXES + "; CREATE INDEX broken ON no_such_table(x)")
    else:

        def fail(*_: Any) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(Store, "_import_reloads", staticmethod(fail))
    with pytest.raises(sqlite3.OperationalError):
        Store.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    finally:
        conn.close()
    assert counts(path) == before and (tmp_path / "reloads.json").exists()
    # Not quarantined either: the daemon runs degraded and the file stays for the next attempt.
    with pytest.raises(StoreUnavailable):
        Store.open_or_recover(path, retry_delays=())
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))


@pytest.mark.parametrize("version", [1, 16, 17])
def test_a_store_older_than_18_refuses_to_open(tmp_path: Path, version: int) -> None:
    path = tmp_path / "ops.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for script in MIGRATIONS_V18[:version]:
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    with pytest.raises(StoreUnavailable, match="older than this agent upgrades"):
        Store.open_or_recover(path, retry_delays=())
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    finally:
        conn.close()


def test_tables_without_a_schema_version_refuse_to_open(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE stray (x INTEGER)")
    conn.close()
    with pytest.raises(StoreUnavailable, match="older than this agent upgrades"):
        Store.open_or_recover(path, retry_delays=())


def test_old_entries_and_odd_rows_never_break_the_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    v18_store(path)
    conn = sqlite3.connect(path, isolation_level=None)
    # An entry still in Needs you whose activity row was pruned long ago, and a change row with broken JSON.
    conn.execute("INSERT INTO items (id, key, kind, title, state, need, as_of, at, needs_since, created_at,"
                 " updated_at) VALUES ('a-77', 'activity:77', 'backup', 'Backup failed', 'waiting', ?, ?, ?, ?, ?, ?)",
                 (json.dumps({"kind": "ack", "text": "t", "primary": {"id": "ack", "label": "OK"}, "secondary": [],
                              "expires_at": None}), at(-9000), at(-9000), at(-9000), at(-9000), at(-9000)))  # fmt: skip
    conn.execute("UPDATE change_proposals SET files = 'not json', summary = '' WHERE id = 7")
    conn.close()
    store = Store.open(path)
    try:
        assert store.next_activity() == 78  # a-77 is never overwritten by a new entry
        change = store.change(7)
        assert change is not None and change["files"] == [] and change["summary"] == []
    finally:
        store.close()


def test_the_store_is_copied_aside_before_the_upgrade(tmp_path: Path) -> None:
    import os
    import stat

    from opsd.store import upgrade_copy

    path = tmp_path / "ops.db"
    v18_store(path)
    before = counts(path)
    store = Store.open(path)
    store.close()
    copy = upgrade_copy(path)
    assert copy.name == "ops.db.v18.bak" and stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert copy.stat().st_uid == os.stat(path).st_uid and not list(tmp_path.glob("*.tmp"))
    # A consistent 0.18 store: what the admin puts back to roll back by hand.
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert counts(copy) == before


def test_a_copy_that_cannot_be_made_refuses_the_upgrade(tmp_path: Path) -> None:
    from opsd.store import upgrade_copy

    path = tmp_path / "ops.db"
    v18_store(path)
    before = counts(path)
    # Something in the way of the copy (here a directory where its temporary file goes).
    blocker = upgrade_copy(path).with_name("ops.db.v18.bak.tmp")
    blocker.mkdir()
    (blocker / "x").write_text("x")
    with pytest.raises(StoreUnavailable, match="could not copy the store aside"):
        Store.open_or_recover(path, retry_delays=())
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    finally:
        conn.close()
    assert counts(path) == before and not upgrade_copy(path).exists()


def test_the_copy_is_deleted_after_14_days(tmp_path: Path) -> None:
    import os

    from opsd.store import upgrade_copy

    path = tmp_path / "ops.db"
    v18_store(path)
    store = Store.open(path)
    copy = upgrade_copy(path)
    made = copy.stat().st_mtime
    assert not store.expire_upgrade_copy(NOW + timedelta(days=13)) and copy.exists()
    store.prune(now=datetime.fromtimestamp(made, UTC) + timedelta(days=14, minutes=1))
    assert not copy.exists()
    store.close()
    # At a start too.
    copy.write_bytes(b"")
    old = (NOW - timedelta(days=15)).timestamp()
    os.utime(copy, (old, old))
    Store.open(path).close()
    assert not copy.exists()


def test_the_documented_rollback_gives_a_healthy_0_18_store(tmp_path: Path) -> None:
    """storage.md S12: stop, delete the WAL and SHM, copy (not move) the copy back, start 0.18."""
    import shutil

    from opsd.store import upgrade_copy

    path = tmp_path / "ops.db"
    v18_store(path)
    before = counts(path)
    store = Store.open(path)
    for n in range(50):
        store.append_turn(CHAT, role="admin", text=f"written by 0.19.0, {n}", now=NOW)
    # The add-on stopped hard: what its WAL held then is left next to the file.
    wal = Path(f"{path}-wal")
    left = wal.read_bytes()
    assert left
    store.close()

    def restore(*, delete_wal: bool) -> str:
        wal.write_bytes(left)
        if delete_wal:
            # Step 2: a 0.19.0 WAL applied to the 0.18 file can leave it malformed.
            for leftover in (wal, Path(f"{path}-shm")):
                leftover.unlink(missing_ok=True)
        shutil.copy2(upgrade_copy(path), path)
        conn = sqlite3.connect(path)
        try:
            return str(conn.execute("PRAGMA quick_check").fetchone()[0])
        except sqlite3.DatabaseError as err:
            return str(err)
        finally:
            conn.close()

    assert restore(delete_wal=True) == "ok"
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    finally:
        conn.close()
    assert counts(path) == before and upgrade_copy(path).exists()


def test_the_copy_and_its_rename_reach_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import stat

    path = tmp_path / "ops.db"
    v18_store(path)
    synced: list[bool] = []
    real = os.fsync

    def fsync(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    Store.open(path).close()
    assert False in synced and True in synced  # the copy's data, then the directory that holds its new name
