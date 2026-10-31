"""SQLite store: migrations, file modes, corruption recovery, CAS claims, single-use actions."""

import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opsd import store as store_mod
from opsd.store import SCHEMA_VERSION, Store, StoreError, checkpoint_main

T0 = datetime(2026, 1, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Store]:
    store = Store.open(tmp_path / "ops.db")
    yield store
    store.close()


def test_migrated_private_and_reopenable(tmp_path: Path) -> None:
    path = tmp_path / "data" / "ops.db"
    store = Store.open(path)
    assert store.schema_version() == SCHEMA_VERSION
    store.bump("2026-01-26", "x")
    store.close()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            assert stat.S_IMODE(candidate.stat().st_mode) == 0o600, suffix
    again = Store.open(path)
    assert again.schema_version() == SCHEMA_VERSION and again.count("2026-01-26", "x") == 1
    again.close()


def test_corrupt_file_is_quarantined_and_recreated(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    path.write_bytes(b"this is not a database" * 100)
    store, recovered = Store.open_or_recover(path, now=T0)
    try:
        assert recovered and store.schema_version() == SCHEMA_VERSION
        assert (tmp_path / "ops.db.corrupt-20260126T120000Z").read_bytes().startswith(b"this is not")
    finally:
        store.close()
    store, recovered = Store.open_or_recover(path)
    assert not recovered
    store.close()


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.close()
    with pytest.raises(StoreError, match="newer"):
        Store.open(path)


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "MIGRATIONS", ("CREATE TABLE a (x INTEGER); CREATE TABLE a (y INTEGER)",))
    monkeypatch.setattr(store_mod, "SCHEMA_VERSION", 1)
    path = tmp_path / "ops.db"
    with pytest.raises(sqlite3.OperationalError):
        Store.open(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'a'").fetchone() is None
    conn.close()


def test_closed_store_fails_closed(db: Store) -> None:
    db.close()
    with pytest.raises(StoreError):
        db.count("d", "x")


def test_audit_roundtrip_and_prune(db: Store) -> None:
    row = db.audit(at=T0, run_id="r1", principal="agent-self", channel="opsctl", capability="baseline.ack",
                   args={"key": "k"}, tier="auto", decision="execute", outcome="ok", detail="d" * 900)  # fmt: skip
    rows = db.audit_rows(run_id="r1")
    assert rows[0]["id"] == row and rows[0]["outcome"] == "ok" and len(rows[0]["detail"]) == 500
    for i in range(5):
        db.audit(at=T0 + timedelta(seconds=i), run_id=None, principal="p", channel="c", capability="x", args={},
                 tier=None, decision="rejected", outcome="rejected")  # fmt: skip
    assert db.prune_audit(before=T0 - timedelta(days=1), keep_rows=3) == 3
    assert len(db.audit_rows()) == 3
    assert db.prune_audit(before=T0 + timedelta(days=1)) == 3


def test_counter_limit_is_atomic(db: Store) -> None:
    results: list[bool] = []

    def worker() -> None:
        results.append(db.bump("d", "manual", limit=5))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 5 and db.count("d", "manual") == 5
    assert db.bump("d", "free") and db.count("d", "free") == 1


def test_tasks_claim_once_and_resume_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    a = Store.open(path)
    b = Store.open(path)  # a second connection, as after a restart
    try:
        task = a.ensure_system_task("health.check", title="t", schedule={"daily_at": "05:30"},
                                    next_fire_at=T0, payload={"trigger": "daily"}, now=T0)  # fmt: skip
        assert a.claim_due(T0 - timedelta(seconds=1)) == []
        first, second = a.claim_due(T0), b.claim_due(T0)
        assert [t.id for t in first] == [task.id] and second == []
        assert first[0].state == "running" and first[0].attempts == 1
        # Crash mid-run: on restart the task fires again.
        assert b.recover_running(T0 + timedelta(minutes=5)) == 1
        resumed = b.claim_due(T0 + timedelta(minutes=5))
        assert [t.id for t in resumed] == [task.id] and resumed[0].attempts == 2
        later = T0 + timedelta(days=1)
        assert b.reschedule(task.id, next_fire_at=later, now=T0, payload={"trigger": "daily"})
        assert not b.reschedule(task.id, next_fire_at=later, now=T0)  # only from running
        assert b.next_due_at() == later
        again = a.ensure_system_task("health.check", title="t2", schedule={}, next_fire_at=T0, payload={}, now=T0)
        assert again.id == task.id and again.title == "t2"
        assert a.system_task("health.check") is not None and a.system_task("nope") is None
        assert a.claim_due(T0)[0].id == task.id
        assert a.finish_task(task.id, "done", now=T0)
        got = a.get_task(task.id)
        assert got is not None and got.state == "done" and got.next_fire_at is None
        with pytest.raises(ValueError, match="final"):
            a.finish_task(task.id, "pending", now=T0)
    finally:
        a.close()
        b.close()


def test_task_constraints(db: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError), db._tx() as conn:
        conn.execute(
            "INSERT INTO tasks (kind, title, owner, created_by, created_at, updated_at)"
            " VALUES ('shell', 't', 'o', 'c', 'x', 'x')"
        )


def test_issued_actions_are_single_use_and_expire(db: Store) -> None:
    db.issue_action("OPS_DONE_" + "a" * 32, kind="DONE", tag="t", thread_id=None, now=T0,
                    expires_at=T0 + timedelta(hours=1))  # fmt: skip
    assert db.consume_action("OPS_DONE_" + "b" * 32, now=T0) is None
    row = db.consume_action("OPS_DONE_" + "a" * 32, now=T0)
    assert row is not None and row["tag"] == "t"
    assert db.consume_action("OPS_DONE_" + "a" * 32, now=T0) is None  # replay
    db.issue_action("OPS_DONE_" + "c" * 32, kind="DONE", tag="t", thread_id=None, now=T0,
                    expires_at=T0 + timedelta(hours=1))  # fmt: skip
    assert db.consume_action("OPS_DONE_" + "c" * 32, now=T0 + timedelta(hours=2)) is None  # expired
    with pytest.raises(sqlite3.IntegrityError):
        db.issue_action("OPS_DONE_" + "a" * 32, kind="DONE", tag="t", thread_id=None, now=T0, expires_at=T0)


def test_checkpoint_main(tmp_path: Path) -> None:
    assert checkpoint_main(tmp_path / "missing.db") == 0
    store = Store.open(tmp_path / "ops.db")
    store.bump("d", "x")
    try:
        assert checkpoint_main(tmp_path / "ops.db") == 0
        wal = tmp_path / "ops.db-wal"
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        store.close()


def test_store_file_created_private_even_with_open_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        store = Store.open(tmp_path / "ops.db")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "ops.db").stat().st_mode) == 0o600
    store.close()


class _Err(sqlite3.OperationalError):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.sqlite_errorname = name


@pytest.mark.parametrize("name", ["SQLITE_FULL", "SQLITE_IOERR_WRITE", "SQLITE_CANTOPEN", "SQLITE_READONLY"])
def test_disk_and_io_errors_never_recreate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    path = tmp_path / "ops.db"
    Store.open(path).close()

    def fail(cls: type[Store], p: Path) -> Store:
        raise _Err(name)

    monkeypatch.setattr(Store, "open", classmethod(fail))
    with pytest.raises(store_mod.StoreUnavailable, match=name.split("_")[1]):
        Store.open_or_recover(path, retry_delays=())
    assert [p.name for p in tmp_path.iterdir() if "corrupt" in p.name] == []


def test_busy_is_retried_with_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_open = Store.open

    def flaky(cls: type[Store], p: Path) -> Store:
        calls.append(1)
        if len(calls) < 3:
            raise _Err("SQLITE_BUSY")
        return real_open(p)

    monkeypatch.setattr(Store, "open", classmethod(flaky))
    sleeps: list[float] = []
    store, recovered = Store.open_or_recover(tmp_path / "ops.db", retry_delays=(0.5, 1.0, 2.0), sleep=sleeps.append)
    store.close()
    assert not recovered and sleeps == [0.5, 1.0]
    calls.clear()
    monkeypatch.setattr(Store, "open", classmethod(lambda cls, p: (_ for _ in ()).throw(_Err("SQLITE_LOCKED"))))
    with pytest.raises(store_mod.StoreUnavailable, match="busy"):
        Store.open_or_recover(tmp_path / "ops.db", retry_delays=(0.1,), sleep=sleeps.append)


def test_newer_schema_is_never_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(store_mod.StoreUnavailable, match="newer"):
        Store.open_or_recover(path, retry_delays=())
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))


def test_failed_quick_check_is_quarantined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Store.open
    state = {"first": True}

    def corrupt_once(cls: type[Store], p: Path) -> Store:
        if state.pop("first", False):
            raise store_mod.StoreCorrupt("integrity check failed: page 3")
        return real_open(p)

    monkeypatch.setattr(Store, "open", classmethod(corrupt_once))
    path = tmp_path / "ops.db"
    path.write_text("x")
    store, recovered = Store.open_or_recover(path, now=T0)
    store.close()
    assert recovered and list(tmp_path.glob("ops.db.corrupt-*"))


def test_idempotency_keys_are_unique(db: Store) -> None:
    with db._tx() as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO tasks (kind, title, owner, created_by, created_at, updated_at)"
                " VALUES ('reminder', 't', 'o', 'c', 'x', 'x')"
            )
    with pytest.raises(sqlite3.IntegrityError), db._tx() as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO tasks (kind, title, owner, created_by, created_at, updated_at, idempotency_key)"
                " VALUES ('intent', 't', 'o', 'c', 'x', 'x', 'update:core@2026.11.1')"
            )


def test_checkpoint_never_fails_the_backup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "ops.db"
    store = Store.open(path)
    store.bump("d", "x")
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert checkpoint_main(path, timeout_s=0.1) == 0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        store.close()
    assert "ops-backup-pre" in capsys.readouterr().err
    path.write_bytes(b"garbage" * 100)
    assert checkpoint_main(path) == 0


def test_migration_15_renames_declined_and_dismissed_to_rejected(tmp_path: Path) -> None:
    import sqlite3

    from opsd.store import MIGRATIONS

    path = tmp_path / "ops.db"
    conn = sqlite3.connect(path)
    for number in range(14):
        for statement in MIGRATIONS[number].split(";"):
            if statement.strip():
                conn.execute(statement)
    now = "2026-01-26T00:00:00+00:00"
    conn.execute("INSERT INTO approvals (id, plan, plan_hash, requested_by, state, created_at, expires_at, subject,"
                 " detail) VALUES ('a', '{}', 'h', 'u', 'declined', ?, ?, 'update.x@2', 'not now')",
                 (now, now))  # fmt: skip
    conn.execute("INSERT INTO approvals (id, plan, plan_hash, requested_by, state, created_at, expires_at, subject,"
                 " used_by_job) VALUES ('b', '{}', 'h', 'u', 'used', ?, ?, 'update.y@3', 7)", (now, now))  # fmt: skip
    conn.execute("INSERT INTO change_proposals (kind, subject, state, detail, requested_by, created_at, expires_at,"
                 " updated_at) VALUES ('change', 's', 'declined', 'declined by u-admin', 'x', ?, ?, ?)",
                 (now, now, now))  # fmt: skip
    conn.execute("INSERT INTO change_proposals (kind, subject, state, detail, requested_by, created_at, expires_at,"
                 " updated_at, applied_sha) VALUES ('facts', 't', 'applied', 'committed abc', 'x', ?, ?, ?, 'abc')",
                 (now, now, now))  # fmt: skip
    conn.execute("INSERT INTO fact_proposals (text, created_at, status, change_id) VALUES ('f', ?, 'dismissed', 1)",
                 (now,))  # fmt: skip
    conn.execute("PRAGMA user_version = 14")
    conn.commit()
    conn.close()
    store = Store.open(path)
    try:
        assert store.schema_version() == SCHEMA_VERSION
        a, b = store.approval("a"), store.approval("b")
        assert a is not None and a["state"] == "rejected" and a["detail"] == "rejected"
        assert b is not None and b["state"] == "used" and b["used_by_job"] == 7
        rejected, applied = store.change(1), store.change(2)
        assert rejected is not None and rejected["state"] == "rejected" and rejected["detail"] == "rejected by u-admin"
        assert applied is not None and applied["state"] == "applied" and applied["applied_sha"] == "abc"
        fact = store._db().execute("SELECT status, change_id FROM fact_proposals").fetchone()
        assert tuple(fact) == ("rejected", 1)
        indexes = {r[0] for r in store._db().execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"approvals_subject", "change_proposals_state", "fact_proposals_status"} <= indexes
        with pytest.raises(sqlite3.IntegrityError):
            store._db().execute("UPDATE approvals SET state = 'declined' WHERE id = 'a'")
    finally:
        store.close()
