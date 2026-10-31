"""A store that lived through every schema since the first, upgraded to this version's.

Real stores were not made at 18: they were made at schema 1 and migrated release by release, so they
carry rows written in older shapes (legacy task kinds and states, NULLs in columns added later, the
"declined" and "dismissed" names, the cost ledger, activity entries without an item, items marked
derived). This test replays that history with the frozen 0.18 migrations (`tests/fixtures/schema_v18.py`),
writing rows in each version's own shape on the way, and then upgrades the result. Nothing open or
actionable may be lost, and no row may make the upgrade fail: a value this version cannot use is kept or
dropped on purpose.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opsd.budget import Period
from opsd.store import SCHEMA_VERSION, Store
from tests.fixtures.schema_v18 import MIGRATIONS_V18

NOW = datetime.now(UTC).replace(microsecond=0)
DAY = NOW.date().isoformat()
MAIN, OLD = "c" * 16, "a" * 16
WEATHER = "update.acme_weather_update"


def at(minutes: int = 0) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class History:
    """A schema-0 file walked up the frozen migrations one version at a time."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.version = 0

    def to(self, version: int) -> sqlite3.Connection:
        while self.version < version:
            for statement in MIGRATIONS_V18[self.version].split(";"):
                if statement.strip():
                    self.conn.execute(statement)
            self.version += 1
            self.conn.execute(f"PRAGMA user_version = {self.version}")
        return self.conn


def lived(path: Path) -> dict[str, int]:
    """Rows written at the versions that wrote them; returns the ids the assertions need."""
    h = History(path)
    ids: dict[str, int] = {}
    # Schema 1: the first chat threads, tasks of every kind the CHECK allowed, approvals, the cost ledger.
    sql = h.to(1).execute
    for thread in (MAIN, OLD):
        sql("INSERT INTO threads (id, principal, channel, created_at, updated_at) VALUES (?, 'admin', 'panel', ?, ?)",
            (thread, at(-9000), at(-9000)))  # fmt: skip
    sql("INSERT INTO turns (thread_id, role, text, at) VALUES (?, 'admin', 'keep me', ?)", (MAIN, at(-9000)))
    sql("INSERT INTO turns (thread_id, role, text, at) VALUES (?, 'admin', 'an earlier conversation', ?)",
        (OLD, at(-9000)))  # fmt: skip
    task = ("INSERT INTO tasks (kind, title, owner, created_by, payload, state, next_fire_at, condition, expires_at,"
            " thread_id, created_at, updated_at) VALUES (?, ?, 'admin', 'chat:r1', '{}', ?, ?, ?, ?, ?, ?,"
            " ?)")  # fmt: skip
    for name, kind, title, state, fire, cond in (
        ("bins", "reminder", "put the bins out", "pending", at(60), None),
        ("watch", "watch", "watch the freezer", "pending", at(30), '{"entity": "sensor.freezer"}'),
        ("intent", "intent", "an old intent", "done", None, None),
        ("asked", "reminder", "call the plumber", "waiting_approval", at(90), None),
    ):
        ids[name] = int(sql(task, (kind, title, state, fire, cond, at(9000), MAIN, at(-9000), at(-9000))).lastrowid
                        or 0)  # fmt: skip
    sql(
        "INSERT INTO approvals (id, plan, plan_hash, requested_by, state, created_at, expires_at)"
        " VALUES ('d' || hex(randomblob(15)), '{}', 'h', 'u-admin', 'declined', ?, ?)",
        (at(-9000), at(-8000)),
    )
    sql("INSERT INTO spend VALUES (?, 'agent', 1.23, 4)", (DAY,))
    sql("INSERT INTO counters VALUES (?, 'claude_runs', 3), (?, 'chat_turns', 2)", (DAY, DAY))
    sql("INSERT INTO issued_actions VALUES (?, 'DONE', 'ha-ops-agent-task-1', NULL, ?, ?, NULL)",
        ("OPS_DONE_" + "e" * 32, at(-5), at(60 * 24)))  # fmt: skip
    sql("INSERT INTO audit (at, principal, channel, capability, decision, outcome) VALUES"
        " (?, 'agent-self', 'opsctl', 'task.create', 'execute', 'ok')", (at(-9000),))  # fmt: skip
    # Schema 4: the per-run ledger (with its cost columns), one run still running; the one chat recorded.
    sql = h.to(4).execute
    run = ("INSERT INTO runs (run_id, profile, class, day, week, month, started_at, ended_at, status, reserved_turns,"
           " reserved_tokens, reserved_usd, turns, tokens, cost_usd) VALUES (?, ?, 'agent', ?, 'w', 'm', ?, ?, ?, 15,"
           " 0, 0.5, ?, ?, ?)")  # fmt: skip
    sql(run, ("r-done", "triage", DAY, at(-30), at(-29), "ok", 4, 900, 0.07))
    sql(run, ("r-live", "chat", DAY, at(-1), None, "running", None, None, None))
    sql("INSERT INTO kv VALUES ('chat.main_thread', ?, ?)", (MAIN, at(-9000)))
    # Schema 5: chat UX columns; a retry task of the new kind.
    sql = h.to(5).execute
    sql("INSERT INTO turns (thread_id, role, text, at, status, client_id) VALUES (?, 'admin', 'queued long ago', ?,"
        " 'refused', 'client-0042')", (MAIN, at(-60)))  # fmt: skip
    # Schema 7: house facts (one dismissed, the old name for rejected).
    sql = h.to(7).execute
    ids["fact_pending"] = int(sql("INSERT INTO fact_proposals (text, created_at) VALUES ('the pump runs at night', ?)",
                                  (at(-50),)).lastrowid or 0)  # fmt: skip
    ids["fact_dismissed"] = int(sql("INSERT INTO fact_proposals (text, created_at, status, decided_at) VALUES"
                                    " ('noise', ?, 'dismissed', ?)", (at(-60), at(-55))).lastrowid or 0)  # fmt: skip
    # Schema 8: an update night installing right now.
    sql = h.to(8).execute
    plan = [{"entity_id": WEATHER, "kind": "hacs", "title": "Acme Weather", "installed": "v1", "latest": "v2",
             "tier": "auto", "execute": True}]  # fmt: skip
    sql("INSERT INTO update_runs (id, window_day, trigger, mode, state, plan, created_at, updated_at) VALUES"
        " (3, ?, 'window', 'act', 'installing', ?, ?, ?)", (DAY, json.dumps(plan), at(-20), at(-2)))  # fmt: skip
    sql("INSERT INTO update_jobs (id, run_id, seq, entity_id, kind, title, from_version, to_version, state,"
        " install_attempts, idempotency_key, started_at, updated_at) VALUES (5, 3, 0, ?, 'hacs', 'Acme Weather', 'v1',"
        " 'v2', 'installing', 1, '3:w:v1:v2', ?, ?)", (WEATHER, at(-3), at(-2)))  # fmt: skip
    # Schema 10: a chat's open request for an approval.
    sql = h.to(10).execute
    sql("INSERT INTO approval_proposals (entity_id, from_version, to_version, plan_run, title, thread_id, run_id,"
        " requested_by, created_at, expires_at) VALUES (?, 'v1', 'v2', 3, 'Acme Weather', ?, 'r-chat',"
        " 'admin-chat:u-admin', ?, ?)", (WEATHER, MAIN, at(-10), at(60 * 70)))  # fmt: skip
    # Schema 11: config changes, one declined (the old name) and one open, the pending fact's change.
    sql = h.to(11).execute
    change = ("INSERT INTO change_proposals (kind, subject, files, state, detail, requested_by, created_at,"
              " expires_at, updated_at) VALUES (?, ?, ?, ?, ?, 'chat', ?, ?, ?)")  # fmt: skip
    ids["declined"] = int(sql(change, ("change", "fix(alerts): old", "[]", "declined", "declined by u-admin",
                                       at(-90), at(-80), at(-85))).lastrowid or 0)  # fmt: skip
    files = json.dumps([{"path": "automations/porch.yaml", "new_sha256": "2" * 64}])
    ids["open"] = int(sql(change, ("change", "fix(automations): porch", files, "open", "", at(-15), at(60 * 70),
                                   at(-15))).lastrowid or 0)  # fmt: skip
    # Schema 12: a push held for after quiet hours.
    h.to(12).execute("INSERT INTO held_pushes (held_at, message, title) VALUES (?, '{}', 'Reminder: bins')",
                     (at(-120),))  # fmt: skip
    # Schema 13 turns the threads into "earlier conversation" activity; a routine check line after it.
    h.to(13).execute("INSERT INTO activity (at, kind, title, detail) VALUES (?, 'check', 'Health check: nothing new',"
                     " '')", (at(-40),))  # fmt: skip
    # Schema 17 makes every activity entry an item. Then, in 0.18's shapes: an entry whose item was never
    # written (an activity-only row), the items 0.18 derived for the open change and the legacy watch task.
    sql = h.to(17).execute
    ids["orphan"] = int(sql("INSERT INTO activity (at, kind, title, detail, needs_admin, ref) VALUES (?, 'backup',"
                            " 'Backup failed', 'no space', 1, '{}')", (at(-25),)).lastrowid or 0)  # fmt: skip
    need = {"kind": "approval", "text": "Change", "primary": {"id": "apply", "label": "Apply and commit"},
            "secondary": [], "expires_at": at(60 * 70)}  # fmt: skip
    item = ("INSERT INTO items (id, key, kind, title, state, need, actions, source, as_of, at, needs_since,"
            " created_at, updated_at, derived) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)")  # fmt: skip
    sql(item, ("p-open", f"change:{ids['open']}", "proposal", "fix(automations): porch", "waiting", json.dumps(need),
               "[]", json.dumps({"change": ids["open"]}), at(-15), at(-15), at(-15), at(-15), at(-15)))  # fmt: skip
    sql(item, ("r-watch", f"task:{ids['watch']}", "reminder", "watch the freezer", "scheduled", None,
               json.dumps([{"id": "cancel", "label": "Cancel"}]), json.dumps({"task": ids["watch"]}), at(-1), at(30),
               None, at(-9000), at(-1)))  # fmt: skip
    # Schema 18: a maintenance finding's item.
    h.to(18).execute(item, ("m-find", "maint:" + "f" * 16, "maintenance", "acme_sprinkler: deprecated", "proposed",
                            None, "[]", "{}", at(-5), at(-5), None, at(-5), at(-5)))  # fmt: skip
    h.conn.close()
    return ids


@pytest.fixture
def replayed(tmp_path: Path) -> Iterator[tuple[Store, dict[str, int]]]:
    path = tmp_path / "ops.db"
    ids = lived(path)
    store = Store.open(path)
    yield store, ids
    store.close()


def test_a_store_from_schema_1_upgrades_with_everything_open_still_working(
    replayed: tuple[Store, dict[str, int]],
) -> None:
    store, ids = replayed
    assert store.schema_version() == SCHEMA_VERSION
    # Reminders: the open one fires; a state this version does not have is pending again.
    bins, asked = store.get_task(ids["bins"]), store.get_task(ids["asked"])
    assert bins is not None and bins.state == "pending" and bins.thread_id == MAIN
    assert asked is not None and asked.state == "pending"
    assert {t.id for t in store.claim_due(NOW + timedelta(hours=2))} == {ids["bins"], ids["asked"]}
    # The update night carries on; the chat's request, the open change and the pending fact stay actionable.
    job = store.update_job(5)
    assert job is not None and job.state == "installing" and store.active_update_run() is not None
    assert [r["entity_id"] for r in store.proposals()] == [WEATHER]
    change = store.change(ids["open"])
    assert change is not None and change["state"] == "open" and change["files"][0]["path"] == "automations/porch.yaml"
    assert store.move_change(ids["open"], expect=("open",), to="applying", now=NOW)
    declined = store.change(ids["declined"])
    assert declined is not None and declined["state"] == "rejected" and declined["detail"] == "rejected by u-admin"
    facts = {f["id"]: f for f in store.list_facts(now=NOW)}
    assert facts[ids["fact_pending"]]["status"] == "pending" and facts[ids["fact_dismissed"]]["status"] == "rejected"
    assert [a["state"] for a in store.approvals()] == ["rejected"]
    # Pushes: the held one, the issued action (once).
    assert [h["title"] for h in store.held_pushes()] == ["Reminder: bins"]
    assert store.consume_action("OPS_DONE_" + "e" * 32, now=NOW) is not None
    assert store.consume_action("OPS_DONE_" + "e" * 32, now=NOW) is None
    # The chat: only the one chat's turns, NULL-era columns read with their defaults.
    turns = store.turns_page(MAIN)
    assert [t.text for t in turns] == ["keep me", "queued long ago"] and turns[0].status == "ok"
    assert store.turn_by_client(MAIN, "client-0042") is not None and store.get_thread(OLD) is None
    # The ledger: a run with cost columns and NULLs in the later ones, and one still running.
    usage = store.usage(Period.at(NOW, UTC), at=NOW)
    assert usage["day"]["runs"] == 2 and usage["profiles"]["triage"]["tokens"] == 900
    assert store.close_stale_runs(at=NOW) == 1
    assert store.count(DAY, "claude_runs") == 3 and store.count(DAY, "chat_turns") == 2
    assert store.kv_get("chat.main_thread") == MAIN
    assert [r["capability"] for r in store.audit_rows()] == ["task.create"]


def test_legacy_values_are_kept_or_dropped_on_purpose(replayed: tuple[Store, dict[str, int]]) -> None:
    store, ids = replayed
    # Task kinds no version ever ran are dropped, and the item 0.18 showed for one resolves instead of waiting.
    assert store.get_task(ids["watch"]) is None and store.get_task(ids["intent"]) is None
    watch = store.item_by_key(f"task:{ids['watch']}")
    assert watch is not None and watch["state"] == "cancelled" and watch["actions"] == []
    assert "not run by this version" in watch["gist"] and not watch["needs"]
    assert store.item_events(watch["id"])[-1]["text"].startswith("Cancelled: not run by this version")
    # An activity entry 0.18 never made an item for becomes a resolved one: History keeps it, nothing asks.
    orphan = store.item(f"a-{ids['orphan']}")
    assert orphan is not None and orphan["state"] == "failed" and orphan["title"] == "Backup failed"
    assert not orphan["needs"] and store.item_events(orphan["id"])
    assert store.next_activity() > ids["orphan"]
    # The derived marker goes; the derived items stay as they were.
    open_change = store.item("p-open")
    assert open_change is not None and open_change["needs"] and "derived" not in open_change
    assert store.item("m-find") is not None
    assert "spend" not in store.table_counts() and "activity" not in store.table_counts()


def test_a_store_from_schema_1_ends_with_exactly_a_fresh_installs_schema(
    replayed: tuple[Store, dict[str, int]], tmp_path: Path
) -> None:
    from tests.test_store_upgrade import shape

    store, _ = replayed
    fresh = Store.open(tmp_path / "fresh.db")
    try:
        assert shape(store) == shape(fresh)
    finally:
        fresh.close()
