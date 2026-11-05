"""The upgrade from schema 19 to 20 (spec storage.md S2, S11).

`tests/fixtures/schema_v19.sql` is a store add-on 0.23.0's own code wrote (every table, every approval state
and kind, every kv family, the rows 0.24.0 drops), dumped once and frozen. Each test loads it, opens it with
this version's store and checks that only what S11 names changed.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import maintenance
from opsd import store as store_mod
from opsd.approvals import Approvals
from opsd.budget import Period
from opsd.store import SCHEMA_VERSION, Store, StoreUnavailable

FIXTURE = Path(__file__).parent / "fixtures" / "schema_v19.sql"
# The fixture's clock (when 0.23.0 wrote it).
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
EARLIER_CHAT = "e" * 16
EARLIER_ITEM = "q-fff5a7d0491d"
QUESTION = "q-42024584b652"
HELD_ITEM = "u-a4cf8519d7e0"
# The finding 0.23.0 hid and planned under 0.22's evidence hash, and the suggestion 0.24.0 has no variant for.
LEGACY_FINDING = "0123456789abcdef"
SUGGESTION = "aabbccddeeff0011"
DEAD_COUNTERS = ("chat_turns", "chat_cap_notice", "followup_runs", "cap:plan_fix", "cap:approval.request",
                 "cap:dashboard.save", "cap:baseline.ack")  # fmt: skip
DROPPED_RUN_COLUMNS = ("week", "month", "reserved_tokens", "reserved_share", "share_5h", "share_7d", "share_estimated")
PRIMARY = {"approvals": ("kind", "id"), "runs": ("run_id",), "kv": ("key",), "items": ("id",)}


def v19_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(FIXTURE.read_text())
    finally:
        conn.close()


def rows(path: Path) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    """Every row of every table, by primary key."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        names = [str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        found = {}
        for name in names:
            key = PRIMARY.get(name, ("id",))
            found[name] = {tuple(r[k] for k in key): dict(r) for r in conn.execute(f'SELECT * FROM "{name}"')}  # noqa: S608
        return found
    finally:
        conn.close()


def dump(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [*conn.iterdump(), str(conn.execute("PRAGMA user_version").fetchone()[0])]
    finally:
        conn.close()


def _sql(text: str) -> str:
    """A schema statement without what only its formatting decides (spacing, quotes, `IF NOT EXISTS`)."""
    text = " ".join(text.split()).replace("IF NOT EXISTS ", "").replace('"', "")
    return text.replace("( ", "(").replace(" )", ")")


def shape(store: Store) -> dict[str, Any]:
    """Everything the schema says: each statement (CHECKs, defaults, generated columns), columns, foreign keys
    and index columns, per table and index."""
    conn = store._db()
    names = [str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {
        "sql": {(str(r[0]), str(r[1])): _sql(str(r[2]))
                for r in conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")},
        "columns": {n: [tuple(c) for c in conn.execute(f"PRAGMA table_xinfo({n})")] for n in names},
        "foreign_keys": {n: [tuple(k) for k in conn.execute(f"PRAGMA foreign_key_list({n})")] for n in names},
        "indexes": {n: sorted((str(i[1]), int(i[2]), int(i[4]),
                               tuple(str(c[2]) for c in conn.execute(f"PRAGMA index_xinfo({i[1]})") if c[5]))
                              for i in conn.execute(f"PRAGMA index_list({n})")) for n in names},
    }  # fmt: skip


@pytest.fixture
def before(tmp_path: Path) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    v19_store(tmp_path / "ops.db")
    return rows(tmp_path / "ops.db")


@pytest.fixture
def upgraded(tmp_path: Path, before: dict[str, Any]) -> Iterator[Store]:
    del before
    store = Store.open(tmp_path / "ops.db")
    yield store
    store.close()


def test_the_fixture_is_a_0_23_0_store() -> None:
    text = FIXTURE.read_text()
    assert "PRAGMA user_version = 19;" in text
    for table in ("audit", "tasks", "approvals", "turns", "runs", "kv", "update_runs", "update_jobs", "items",
                  "item_events"):  # fmt: skip
        assert f'INSERT INTO "{table}"' in text


def test_the_upgrade_makes_the_same_schema_as_a_fresh_install(upgraded: Store, tmp_path: Path) -> None:
    fresh = Store.open(tmp_path / "fresh.db")
    try:
        assert upgraded.schema_version() == fresh.schema_version() == SCHEMA_VERSION == 20
        assert shape(upgraded) == shape(fresh)
        assert sorted(shape(fresh)["columns"]) == ["approvals", "audit", "item_events", "items", "kv", "runs", "tasks",
                                           "turns", "update_jobs", "update_runs"]  # fmt: skip
    finally:
        fresh.close()


def _expected(old: dict[str, dict[tuple[Any, ...], dict[str, Any]]]) -> dict[str, dict[tuple[Any, ...], Any]]:
    """The fixture's rows as S11 says the upgrade leaves them, except the values it computes (checked apart)."""
    want = json.loads(json.dumps({t: {json.dumps(k): v for k, v in r.items()} for t, r in old.items()}))
    approvals = want["approvals"]
    for key, row in list(approvals.items()):
        payload = json.loads(row["payload"])
        if row["kind"] == "request" or (row["kind"] == "approval" and payload.get("kind") == "dashboard"):
            del approvals[key]
        elif row["kind"] == "approval" and row["state"] == "pending":
            row.update(
                state="void", detail="never confirmed (0.24.0 has no confirmation step)", decided_at="*", updated_at="*"
            )
    for row in want["runs"].values():
        for column in DROPPED_RUN_COLUMNS:
            del row[column]
    items = want["items"]
    del items[json.dumps([EARLIER_ITEM])]
    items[json.dumps([QUESTION])]["kind"] = "question"
    items[json.dumps([HELD_ITEM])]["push"] = "{}"
    want["item_events"] = {k: r for k, r in want["item_events"].items() if r["item_id"] != EARLIER_ITEM}
    want["turns"] = {k: r for k, r in want["turns"].items() if "thread" in json.loads(r["meta"])}
    tasks = want["tasks"]
    tasks = {k: r for k, r in tasks.items() if r["system_key"] != "status.refresh"}
    for row in tasks.values():
        if row["kind"] == "retry" and row["thread_id"] == EARLIER_CHAT:
            row.update(state="cancelled", next_fire_at=None, updated_at="*")
    want["tasks"] = tasks
    kv = want["kv"]
    for key in list(kv):
        name = json.loads(key)[0]
        held = name.startswith("push.held:") and json.loads(json.loads(kv[key]["value"])["message"])["tag"] == HELD_ITEM
        dead = name in ("chat.main_thread", "chat.created_at", "chat.read") or name.startswith(
            ("maintenance:dashboard:", "maintenance:result:", "maintenance:migrated:")) or (
            name.startswith("count:") and name[17:] in DEAD_COUNTERS) or name in (
            f"{maintenance.PLAN_KEY}{SUGGESTION}", f"{maintenance.HIDDEN_KEY}{SUGGESTION}")  # fmt: skip
        if held or dead:
            del kv[key]
    for name in (maintenance.META_KEY, maintenance.FINDINGS_KEY, f"{maintenance.HIDDEN_KEY}{LEGACY_FINDING}",
                 f"{maintenance.PLAN_KEY}{LEGACY_FINDING}"):  # fmt: skip
        kv[json.dumps([name])]["value"] = "*"
    return want


def _as_found(new: dict[str, dict[tuple[Any, ...], dict[str, Any]]],
              want: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:  # fmt: skip
    found = json.loads(json.dumps({t: {json.dumps(k): v for k, v in r.items()} for t, r in new.items()}))
    for table, expected in want.items():
        for key, row in expected.items():
            for column, value in row.items():
                if value == "*" and key in found[table]:
                    found[table][key][column] = "*"
    return found


def test_every_kept_row_is_unchanged_and_only_what_s11_names_changed(before: dict[str, Any], upgraded: Store,
                                                                     tmp_path: Path) -> None:  # fmt: skip
    del upgraded
    want = _expected(before)
    assert _as_found(rows(tmp_path / "ops.db"), want) == want


def test_the_dropped_rows_columns_and_keys_are_gone(upgraded: Store) -> None:
    conn = upgraded._db()
    assert not conn.execute("SELECT 1 FROM approvals WHERE kind = 'request'"
                            " OR (kind = 'approval' AND state = 'pending')").fetchall()  # fmt: skip
    assert not conn.execute("SELECT 1 FROM approvals WHERE json_extract(payload, '$.kind') = 'dashboard'").fetchall()
    columns = {str(c[1]) for c in conn.execute("PRAGMA table_info(runs)")}
    assert not columns & set(DROPPED_RUN_COLUMNS)
    assert upgraded.item(EARLIER_ITEM) is None and upgraded.item_events(EARLIER_ITEM) == []
    assert not conn.execute("SELECT 1 FROM turns WHERE json_extract(meta, '$.thread') IS NULL").fetchall()
    for key in ("chat.main_thread", "chat.created_at", "chat.read"):
        assert upgraded.kv_get(key, "gone") == "gone"
    for prefix in ("maintenance:dashboard:", "maintenance:result:", "maintenance:migrated:"):
        assert upgraded.kv_prefix(prefix) == {}
    for name in DEAD_COUNTERS:
        assert upgraded.count("2026-11-05", name) == 0
    assert upgraded.count("2026-11-05", "cap:integration.reload") == 1
    assert not conn.execute("SELECT 1 FROM tasks WHERE system_key = 'status.refresh'").fetchall()
    assert set(maintenance.meta(upgraded)) == {"at", "full_at", "installed", "newest", "configured"}
    assert [f.key for f in maintenance.load(upgraded)] == [LEGACY_FINDING, "fedcba9876543210", "00112233445566aa"]
    assert upgraded.kv_get(maintenance.HIDDEN_KEY + SUGGESTION, "gone") == "gone"
    # Audit rows of removed capabilities stay as history.
    assert any(r["capability"] == "baseline.ack" for r in upgraded.audit_rows())


def test_a_finding_hidden_or_planned_under_the_0_22_hash_stays_hidden_and_planned(upgraded: Store) -> None:
    by_key = {f.key: f for f in maintenance.load(upgraded)}
    for key in (LEGACY_FINDING, "00112233445566aa"):
        assert maintenance.hidden(upgraded, by_key[key])
    for key in (LEGACY_FINDING, "fedcba9876543210"):
        assert maintenance.plan_of(upgraded, by_key[key]) is not None


def test_a_held_item_push_goes_back_to_the_item_book_and_daemon_pushes_stay_held(upgraded: Store) -> None:
    held = upgraded.held_pushes()
    assert [r["title"] for r in held] == ["Ops Agent: backup made"]
    assert [r["title"] for r in upgraded.held_pushes(states=("undelivered",))] == ["Earlier"]
    record = upgraded.item(HELD_ITEM)
    assert record is not None and record["push"] == {} and record["needs"]


def test_open_records_still_work_through_the_current_api(upgraded: Store) -> None:
    approvals = Approvals(upgraded, clock=lambda: NOW)
    change = next(a for a in upgraded.approvals(states=("approved",)) if a["subject"] == "change:1:apply")
    binding = {"proposal": 1, "commit": "2" * 40, "base": "1" * 40, "action": "apply"}
    assert approvals.consume(change["id"], "change", binding)
    assert not approvals.consume(change["id"], "change", binding)
    update = next(a for a in upgraded.approvals(states=("approved",)) if a["subject"] == "update.example_os@16.2")
    assert approvals.bound_update(update)
    [run] = [r for r in upgraded.recent_update_runs() if r["state"] == "installing"]
    jobs = upgraded.update_jobs(int(run["id"]))
    assert [j.state for j in jobs] == ["installing", "pending"]
    assert upgraded.transition_job(jobs[0].id, expect=("installing",), to="verifying", now=NOW)
    assert upgraded.decide_fact(1, "applied", now=NOW)
    change_row = upgraded.change(1)
    assert change_row is not None and change_row["state"] == "open" and change_row["files"]
    claimed = upgraded.claim_due(NOW + timedelta(hours=2))
    assert sorted(t.system_key or t.kind for t in claimed) == ["health.check", "reminder", "updates.window"]
    thread = upgraded.get_thread(f"ask:{QUESTION}")
    assert thread is not None and thread["turns"] == 2 and not thread["unread"]
    turn = upgraded.append_turn(f"item:{'c-59923230bec5'}", role="agent", text="Not urgent.", now=NOW)
    assert turn.thread_id == "item:c-59923230bec5"
    assert [t.id for t in upgraded.stuck_turns()] == [5]
    usage = upgraded.usage(Period.at(NOW, UTC), at=NOW)
    assert usage["day"]["runs"] == 3 and usage["day"]["tokens"] == 1500
    assert upgraded.consume_action("act-example-1", now=NOW) is not None
    assert upgraded.next_activity() == 42
    question = upgraded.item(QUESTION)
    assert question is not None and question["kind"] == "question"


def test_a_second_open_changes_nothing(upgraded: Store, tmp_path: Path) -> None:
    upgraded.close()
    once = dump(tmp_path / "ops.db")
    Store.open(tmp_path / "ops.db").close()
    assert dump(tmp_path / "ops.db") == once


def test_the_files_0_23_0_left_beside_the_store_are_deleted(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    v19_store(path)
    leftovers = [tmp_path / "ops.db.v18.bak", tmp_path / "run.lock", tmp_path / "selftest.json"]
    for leftover in leftovers:
        leftover.write_text("x")
    Store.open(path).close()
    assert not any(p.exists() for p in leftovers)


@pytest.mark.parametrize("where", ["sql", "indexes", "findings"])
def test_a_failure_midway_leaves_the_store_at_19_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           where: str) -> None:  # fmt: skip
    path = tmp_path / "ops.db"
    v19_store(path)
    (tmp_path / "run.lock").write_text("")
    once = dump(path)
    if where == "sql":
        broken = store_mod.UPGRADE_19 + "; INSERT INTO no_such_table VALUES (1)"  # noqa: S608 - a constant
        monkeypatch.setattr(store_mod, "UPGRADE_19", broken)
    elif where == "indexes":
        # After every copy and every drop: the whole upgrade still rolls back.
        monkeypatch.setattr(store_mod, "INDEXES", store_mod.INDEXES + "; CREATE INDEX broken ON no_such_table(x)")
    else:

        def fail(_: sqlite3.Connection) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(store_mod, "_upgrade_findings", fail)
    with pytest.raises(sqlite3.OperationalError):
        Store.open(path)
    assert dump(path) == once and (tmp_path / "run.lock").exists()
    # Not quarantined either: the daemon runs degraded and the file stays for the next attempt.
    with pytest.raises(StoreUnavailable):
        Store.open_or_recover(path, retry_delays=())
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))


def test_a_stored_finding_the_upgrade_cannot_read_is_dropped_not_wedging_the_start(tmp_path: Path) -> None:
    """A Python-side error over one stored finding (here `affected` holding a number, so its fingerprint raises)
    drops that finding with its plan and hiding; the upgrade still commits, so the daemon starts (S11)."""
    path = tmp_path / "ops.db"
    v19_store(path)
    conn = sqlite3.connect(path)
    try:
        stored = json.loads(
            conn.execute("SELECT value FROM kv WHERE key = ?", (maintenance.FINDINGS_KEY,)).fetchone()[0]
        )
        broken = dict(stored[0]) | {"key": "ffffffffffffffff", "affected": ["configuration.yaml:12", 3]}
        conn.execute("UPDATE kv SET value = ? WHERE key = ?", (json.dumps([broken, *stored]), maintenance.FINDINGS_KEY))
        for prefix in (maintenance.PLAN_KEY, maintenance.HIDDEN_KEY):
            conn.execute("INSERT INTO kv VALUES (?, ?, ?)", (f"{prefix}ffffffffffffffff", '{"evidence": "x"}', "t"))
        conn.commit()
    finally:
        conn.close()
    upgraded = Store.open(path)
    try:
        assert upgraded._db().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [f.key for f in maintenance.load(upgraded)] == [LEGACY_FINDING, "fedcba9876543210", "00112233445566aa"]
        for prefix in (maintenance.PLAN_KEY, maintenance.HIDDEN_KEY):
            assert upgraded.kv_get(f"{prefix}ffffffffffffffff", "gone") == "gone"
        # The readable findings are still rewritten as S11 says.
        by_key = {f.key: f for f in maintenance.load(upgraded)}
        assert maintenance.hidden(upgraded, by_key[LEGACY_FINDING])
    finally:
        upgraded.close()


@pytest.mark.parametrize(("version", "says"), [(18, "older than this agent upgrades"), (1, "older than"),
                                               (21, "newer than this agent")])  # fmt: skip
def test_a_store_at_any_other_version_is_refused_untouched(tmp_path: Path, version: int, says: str) -> None:
    path = tmp_path / "ops.db"
    v19_store(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    once = dump(path)
    with pytest.raises(StoreUnavailable, match=says):
        Store.open_or_recover(path, retry_delays=())
    assert dump(path) == once and not list(tmp_path.glob("*.corrupt-*"))


def test_tables_without_a_schema_version_refuse_to_open(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE stray (x INTEGER)")
    conn.close()
    with pytest.raises(StoreUnavailable, match="older than this agent upgrades"):
        Store.open_or_recover(path, retry_delays=())
