"""Durable agent state in SQLite (`/data/ops.db`, root only, 0600).

Ten tables (`docs/spec/storage.md`): the audit log of every capability request, the task schedule
(compare-and-set claims), the per-run usage ledger, the chat's turns, the update runs and jobs, one
`approvals` record for every approval and proposal (a kind and a payload), the items and their events,
and `kv` for small state: daily counters, quiet-hours holds, push action ids, reload times, the chat's
identity and read mark, update and maintenance flags. Files that the health check needs without the
store (`state.json`, `baseline.json`, `open-items.json`, `notify.json`) stay files.

The schema is created whole on a fresh install. A store written by 0.18 (schema 18) is upgraded by
one migration in one transaction; any other version refuses to open. Opening is deliberately
conservative:
- only a file SQLite reports as corrupt or not a database, or one that fails
  `quick_check`, is moved aside and recreated (the store holds no policy and
  the system tasks are re-created at every start);
- a locked or busy database is retried with backoff;
- anything else (disk full, I/O error, permissions, a schema this code does not upgrade) is never
  "repaired": `StoreUnavailable` is raised and the daemon runs degraded, without store-dependent
  features, and alerts the admin.

Every write runs in one transaction. A write records what it touched (a job, a run, an approval, a
change, a fact, a task, a kv key); before the transaction commits, the `observer` (the item book) writes
the items those records show, so an item and its source never disagree after a crash.

Tasks fire at least once, not exactly once: a crash between a task's effect
and its rescheduling repeats the firing after restart. Any future mutating
task must therefore carry an `idempotency_key` (unique per task) that its
executor checks before acting.
"""

import contextlib
import json
import logging
import os
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opsd.budget import DEFAULT_RUN_SHARE, MIN_RUN_SECONDS, WINDOW, Caps, Period, Reservation

_LOGGER = logging.getLogger(__name__)

# The whole schema. `IF NOT EXISTS` lets the upgrade from 18 run the same text: the tables it keeps
# unchanged (audit, kv, update_runs, update_jobs, item_events) already exist, the ones it rebuilds were
# renamed out of the way first. A renamed table keeps its index names, so the indexes are created only
# after the old tables are dropped.
TABLES = """
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        run_id TEXT,
        principal TEXT NOT NULL,
        channel TEXT NOT NULL,
        capability TEXT NOT NULL,
        args TEXT NOT NULL DEFAULT '{}',
        tier TEXT,
        decision TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        notify_admin INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('reminder', 'followup', 'recurring', 'retry')),
        title TEXT NOT NULL,
        owner TEXT NOT NULL,
        created_by TEXT NOT NULL,
        system_key TEXT UNIQUE,
        schedule TEXT NOT NULL DEFAULT '{}',
        payload TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'done', 'cancelled', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 0,
        next_fire_at TEXT,
        last_fired_at TEXT,
        thread_id TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS approvals (
        kind TEXT NOT NULL CHECK (kind IN ('approval', 'request', 'change', 'fact')),
        id TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        plan_hash TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL DEFAULT '',
        run_when TEXT NOT NULL DEFAULT 'window',
        used_by INTEGER,
        detail TEXT NOT NULL DEFAULT '',
        run_id TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        decided_at TEXT,
        decided_by TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, id),
        CHECK (kind != 'approval' OR state IN ('pending', 'approved', 'rejected', 'expired', 'used', 'void')),
        CHECK (kind != 'request' OR state IN ('open', 'done', 'void', 'expired')),
        CHECK (kind != 'change' OR state IN ('refused', 'open', 'applying', 'applied', 'reverting', 'reverted',
            'failed', 'stale', 'rejected', 'expired')),
        CHECK (kind != 'fact' OR state IN ('pending', 'applied', 'rejected'))
    );
    CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY,
        role TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'system')),
        text TEXT NOT NULL,
        at TEXT NOT NULL,
        run_id TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        meta TEXT NOT NULL DEFAULT '{}',
        client_id TEXT,
        reply_to INTEGER
    );
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        class TEXT NOT NULL,
        day TEXT NOT NULL,
        week TEXT NOT NULL,
        month TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        reserved_turns INTEGER NOT NULL,
        reserved_tokens INTEGER NOT NULL,
        reserved_seconds REAL NOT NULL DEFAULT 0,
        reserved_share REAL NOT NULL DEFAULT 0,
        turns INTEGER,
        tokens INTEGER,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        duration_s REAL NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        seconds REAL,
        share_5h REAL,
        share_7d REAL,
        acct_5h REAL,
        acct_7d REAL,
        share_estimated INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS update_runs (
        id INTEGER PRIMARY KEY,
        window_day TEXT NOT NULL,
        trigger TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('report', 'act')),
        state TEXT NOT NULL CHECK (state IN ('planned', 'backing_up', 'installing', 'done', 'aborted', 'failed')),
        plan TEXT NOT NULL DEFAULT '[]',
        review TEXT NOT NULL DEFAULT '{}',
        backup TEXT NOT NULL DEFAULT '{}',
        before TEXT,
        before_first TEXT,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS update_jobs (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES update_runs(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        entity_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'installing', 'rebooting', 'verifying',
            'succeeded', 'rolling_back', 'rolled_back', 'rollback_failed', 'failed', 'skipped', 'reboot_pending')),
        install_attempts INTEGER NOT NULL DEFAULT 0,
        rollback_attempts INTEGER NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT '',
        regressions TEXT NOT NULL DEFAULT '[]',
        idempotency_key TEXT NOT NULL UNIQUE,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('update', 'reminder', 'investigation', 'proposal', 'check', 'backup',
            'reload', 'window', 'maintenance')),
        kicker TEXT NOT NULL DEFAULT '',
        ref TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('proposed', 'waiting', 'scheduled', 'running', 'done', 'failed',
            'cancelled', 'expired')),
        gist TEXT NOT NULL DEFAULT '',
        need TEXT,
        about TEXT,
        actions TEXT NOT NULL DEFAULT '[]',
        phase TEXT,
        follows TEXT,
        facts TEXT NOT NULL DEFAULT '[]',
        agent TEXT,
        change TEXT,
        footnote TEXT,
        quiet INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 1,
        as_of TEXT NOT NULL,
        at TEXT NOT NULL,
        needs_since TEXT,
        resolved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '{}',
        push TEXT NOT NULL DEFAULT '{}',
        needs INTEGER GENERATED ALWAYS AS (state = 'waiting' OR (state = 'failed' AND need IS NOT NULL)) VIRTUAL,
        CHECK ((state = 'waiting') = (need IS NOT NULL) OR (state = 'failed'))
    );
    CREATE TABLE IF NOT EXISTS item_events (
        id INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('you', 'daemon', 'agent')),
        text TEXT NOT NULL,
        revision INTEGER NOT NULL
    )
"""
INDEXES = """
    CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
    CREATE INDEX IF NOT EXISTS audit_run ON audit(run_id);
    CREATE INDEX IF NOT EXISTS tasks_due ON tasks(state, next_fire_at);
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE INDEX IF NOT EXISTS approvals_state ON approvals(kind, state, created_at);
    CREATE INDEX IF NOT EXISTS approvals_subject ON approvals(kind, subject, state);
    CREATE UNIQUE INDEX IF NOT EXISTS turns_client ON turns(client_id) WHERE client_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS turns_reply ON turns(reply_to) WHERE reply_to IS NOT NULL;
    CREATE INDEX IF NOT EXISTS runs_day ON runs(day);
    CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at);
    CREATE INDEX IF NOT EXISTS update_runs_day ON update_runs(window_day, trigger);
    CREATE INDEX IF NOT EXISTS update_runs_state ON update_runs(state);
    CREATE INDEX IF NOT EXISTS update_jobs_run ON update_jobs(run_id, seq);
    CREATE INDEX IF NOT EXISTS update_jobs_state ON update_jobs(state);
    CREATE INDEX IF NOT EXISTS items_needs ON items(needs, needs_since);
    CREATE INDEX IF NOT EXISTS items_at ON items(at);
    CREATE INDEX IF NOT EXISTS items_state ON items(state, resolved_at);
    CREATE INDEX IF NOT EXISTS item_events_item ON item_events(item_id, id)
"""
SCHEMA = TABLES + ";" + INDEXES

UPGRADE_FROM = 18
SCHEMA_VERSION = 19

# The tables 19 rebuilds; renamed first so that TABLES creates their new shape.
_REBUILT = ("tasks", "approvals", "turns", "runs", "items")
# 18's tables that 19 folds into others, children before parents (old turns reference threads).
_FOLDED = ("old_turns", "threads", "spend", "counters", "issued_actions", "fact_proposals", "approval_proposals",
           "change_proposals", "held_pushes", "activity", "old_tasks", "old_approvals", "old_runs",
           "old_items")  # fmt: skip
# Copies 18's rows into 19's shape. Runs after the renames and TABLES, before the old tables are dropped.
UPGRADE_18 = """
    INSERT INTO tasks (id, kind, title, owner, created_by, system_key, schedule, payload, state, attempts,
            generation, next_fire_at, last_fired_at, thread_id, idempotency_key, created_at, updated_at)
        SELECT id, kind, title, owner, created_by, system_key, schedule, payload,
            CASE state WHEN 'waiting_approval' THEN 'pending' ELSE state END, attempts, generation, next_fire_at,
            last_fired_at, thread_id, idempotency_key, created_at, updated_at
        FROM old_tasks WHERE kind IN ('reminder', 'followup', 'recurring', 'retry');
    INSERT INTO approvals (kind, id, subject, state, payload, plan_hash, requested_by, run_when, used_by, detail,
            created_at, expires_at, decided_at, decided_by, updated_at)
        SELECT 'approval', id, subject, state, plan, plan_hash, requested_by, run_when, used_by_job, detail,
            created_at, expires_at, decided_at, decided_by, COALESCE(decided_at, created_at)
        FROM old_approvals;
    INSERT INTO approvals (kind, id, subject, state, payload, requested_by, detail, run_id, created_at,
            expires_at, decided_at, updated_at)
        SELECT 'request', CAST(id AS TEXT), entity_id || '@' || to_version, state,
            json_object('entity_id', entity_id, 'from_version', from_version, 'to_version', to_version,
                'plan_run', plan_run, 'title', title, 'thread_id', thread_id),
            requested_by, detail, run_id, created_at, expires_at, decided_at, COALESCE(decided_at, created_at)
        FROM approval_proposals;
    INSERT INTO approvals (kind, id, subject, state, payload, requested_by, detail, run_id, created_at,
            expires_at, decided_at, updated_at)
        SELECT 'change', CAST(id AS TEXT), subject, state,
            json_object('variant', kind, 'why', why, 'effect', effect,
                'files', CASE WHEN json_valid(files) THEN json(files) ELSE json('[]') END,
                'summary', CASE WHEN json_valid(summary) THEN json(summary) ELSE json('[]') END, 'diff', diff,
                'base_sha', base_sha, 'commit_sha', commit_sha, 'ref', ref,
                'fact_id', fact_id, 'applied_sha', applied_sha, 'reverted_sha', reverted_sha,
                'thread_id', thread_id),
            requested_by, detail, run_id, created_at, expires_at, decided_at, updated_at
        FROM change_proposals;
    INSERT INTO approvals (kind, id, state, payload, created_at, decided_at, updated_at)
        SELECT 'fact', CAST(id AS TEXT), status,
            json_object('text', text, 'thread_id', thread_id, 'turn_id', turn_id, 'change_id', change_id),
            created_at, decided_at, COALESCE(decided_at, created_at)
        FROM fact_proposals;
    INSERT INTO turns (id, role, text, at, run_id, status, meta, client_id, reply_to)
        SELECT id, role, text, at, run_id, status, meta, client_id, reply_to FROM old_turns
        WHERE thread_id = (SELECT value FROM kv WHERE key = 'chat.main_thread');
    INSERT INTO runs (run_id, profile, class, day, week, month, started_at, ended_at, status, reserved_turns,
            reserved_tokens, reserved_seconds, reserved_share, turns, tokens, tokens_in, tokens_out,
            tokens_cache_read, duration_s, model, seconds, share_5h, share_7d, acct_5h, acct_7d, share_estimated)
        SELECT run_id, profile, class, day, week, month, started_at, ended_at, status, reserved_turns,
            reserved_tokens, reserved_seconds, reserved_share, turns, tokens, tokens_in, tokens_out,
            tokens_cache_read, duration_s, model, seconds, share_5h, share_7d, acct_5h, acct_7d, share_estimated
        FROM old_runs;
    INSERT INTO items (id, key, kind, kicker, ref, title, state, gist, need, about, actions, phase, follows, facts,
            agent, change, footnote, quiet, revision, as_of, at, needs_since, resolved_at, created_at, updated_at,
            source, push)
        SELECT id, key, kind, kicker, ref, title, state, gist, need, about, actions, phase, follows, facts, agent,
            change, footnote, quiet, revision, as_of, at, needs_since, resolved_at, created_at, updated_at, source,
            push
        FROM old_items;
    INSERT OR REPLACE INTO kv (key, value, updated_at)
        SELECT 'count:' || day || ':' || name, CAST(n AS TEXT), day || 'T00:00:00+00:00' FROM counters;
    INSERT OR REPLACE INTO kv (key, value, updated_at)
        SELECT 'push.held:' || printf('%012d', id),
            json_object('held_at', held_at, 'message', message, 'title', title, 'state', state,
                'outcome', outcome, 'settled_at', settled_at),
            COALESCE(settled_at, held_at)
        FROM held_pushes;
    INSERT OR REPLACE INTO kv (key, value, updated_at)
        SELECT 'push.action:' || action_id,
            json_object('kind', kind, 'tag', tag, 'thread_id', thread_id, 'issued_at', issued_at,
                'expires_at', expires_at, 'used_at', used_at),
            COALESCE(used_at, issued_at)
        FROM issued_actions;
    INSERT OR REPLACE INTO kv (key, value, updated_at)
        SELECT 'chat.read', CAST(last_read_turn_id AS TEXT), updated_at FROM threads
        WHERE id = (SELECT value FROM kv WHERE key = 'chat.main_thread');
    INSERT OR REPLACE INTO kv (key, value, updated_at)
        SELECT 'chat.created_at', created_at, created_at FROM threads
        WHERE id = (SELECT value FROM kv WHERE key = 'chat.main_thread');
    INSERT OR REPLACE INTO kv (key, value, updated_at)
        SELECT 'activity.seq', CAST(MAX(COALESCE((SELECT MAX(id) FROM activity), 0),
            COALESCE((SELECT MAX(CAST(substr(key, 10) AS INTEGER)) FROM items WHERE key LIKE 'activity:%'), 0))
            AS TEXT), '1970-01-01T00:00:00+00:00';
    DELETE FROM kv WHERE key = 'items.backfill' OR key LIKE 'confirm:%'
"""

# Update runs in these states own the update pipeline; at most one exists.
ACTIVE_UPDATE_STATES = ("backing_up", "installing")
_ACTIVE_SQL = "(" + ", ".join(f"'{state}'" for state in ACTIVE_UPDATE_STATES) + ")"
JOB_STATES = ("pending", "installing", "rebooting", "verifying", "succeeded", "rolling_back", "rolled_back",
              "rollback_failed", "failed", "skipped", "reboot_pending")  # fmt: skip
# `reboot_pending` ends the run (the agent could not reboot); `UpdateManager.reconcile` settles it
# once the host runs the new version.
JOB_FINAL = frozenset({"succeeded", "rolled_back", "rollback_failed", "failed", "skipped", "reboot_pending"})
UPDATE_KEEP = timedelta(days=180)

MAX_FACT_CHARS = 500
# The copy taken before the upgrade from 18 (`<store>.v18.bak`), kept for a manual rollback.
UPGRADE_COPY_SUFFIX = ".v18.bak"
UPGRADE_COPY_KEEP = timedelta(days=14)
_FILE_MODE = 0o600
# The reload rate state (`opsd.reload`): `{domain: last reload}`, the same JSON 0.18's reloads.json held.
RELOAD_KEY = "reload.last"
_RELOADS_FILE = "reloads.json"
_MAX_IMPORT_BYTES = 65536
COUNT_KEY = "count:"
HELD_KEY = "push.held:"
ACTION_KEY = "push.action:"
MAIN_THREAD_KEY = "chat.main_thread"
READ_KEY = "chat.read"
CHAT_CREATED_KEY = "chat.created_at"
ACTIVITY_SEQ_KEY = "activity.seq"
_CHANGE_PAYLOAD = frozenset({"files", "diff", "base_sha", "commit_sha", "ref", "applied_sha", "reverted_sha"})
_CHANGE_COLUMNS = frozenset({"detail", "decided_at", "expires_at"})

# What a write touched, for the observer: (`job`, id), (`run`, id), (`approval`, id), (`change`, id),
# (`fact`, id), (`task`, id), (`kv`, key) or (`item`, id) for an item written outside the observer.
Touched = tuple[str, Any]


class StoreError(RuntimeError):
    """The store cannot be used; callers fail closed."""


class StoreCorrupt(StoreError):
    """The file is damaged; moving it aside is safe."""


class StoreUnavailable(StoreError):
    """The store could not be opened and must not be recreated (disk, I/O, lock, a schema it cannot use)."""


MIN_KEEP = timedelta(days=8)
_CORRUPT_CODES = frozenset({"SQLITE_CORRUPT", "SQLITE_NOTADB"})
_BUSY_CODES = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED"})
OPEN_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0)


def _error_name(err: sqlite3.Error) -> str:
    """The primary result code name (`SQLITE_IOERR_WRITE` -> `SQLITE_IOERR`)."""
    name = str(getattr(err, "sqlite_errorname", "") or "")
    for code in (*_CORRUPT_CODES, *_BUSY_CODES):
        if name == code or name.startswith(code + "_"):
            return code
    return name


@dataclass(frozen=True, slots=True)
class Task:
    id: int
    kind: str
    title: str
    owner: str
    system_key: str | None
    schedule: dict[str, Any]
    payload: dict[str, Any]
    state: str
    attempts: int
    next_fire_at: str | None
    generation: int = 0
    last_fired_at: str | None = None
    created_by: str = ""
    thread_id: str | None = None
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class UpdateJob:
    id: int
    run_id: int
    seq: int
    entity_id: str
    kind: str
    title: str
    from_version: str
    to_version: str
    target: dict[str, Any]
    state: str
    install_attempts: int
    rollback_attempts: int
    detail: str
    regressions: list[Any]
    started_at: str | None
    finished_at: str | None
    updated_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "run_id": self.run_id, "seq": self.seq, "entity_id": self.entity_id, "kind": self.kind,
            "title": self.title, "from": self.from_version, "to": self.to_version, "state": self.state,
            "detail": self.detail, "regressions": self.regressions, "started_at": self.started_at,
            "finished_at": self.finished_at,
        }  # fmt: skip


def _json_or(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if raw else default
    except TypeError, ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _update_job(row: sqlite3.Row) -> UpdateJob:
    return UpdateJob(
        id=int(row["id"]), run_id=int(row["run_id"]), seq=int(row["seq"]), entity_id=str(row["entity_id"]),
        kind=str(row["kind"]), title=str(row["title"]), from_version=str(row["from_version"]),
        to_version=str(row["to_version"]), target=_json_or(row["target"], {}), state=str(row["state"]),
        install_attempts=int(row["install_attempts"]), rollback_attempts=int(row["rollback_attempts"]),
        detail=str(row["detail"]), regressions=_json_or(row["regressions"], []), started_at=row["started_at"],
        finished_at=row["finished_at"], updated_at=str(row["updated_at"]),
    )  # fmt: skip


# --------------------------------------------------------------- one approvals record, four kinds


def _approval(row: sqlite3.Row) -> dict[str, Any]:
    """An approval (`kind = approval`): the admin's consent bound to one exact action."""
    keys = ("id", "plan_hash", "requested_by", "state", "created_at", "expires_at", "decided_at", "decided_by",
            "subject", "run_when", "detail")  # fmt: skip
    return {k: row[k] for k in keys} | {"used_by_job": row["used_by"], "plan": _json_or(row["payload"], {})}


def _request(row: sqlite3.Row) -> dict[str, Any]:
    """A chat's request that the admin approve an update (`kind = request`); never an approval."""
    payload = _json_or(row["payload"], {})
    return {"id": int(row["id"]), "entity_id": str(payload.get("entity_id") or ""),
            "from_version": str(payload.get("from_version") or ""), "to_version": str(payload.get("to_version") or ""),
            "plan_run": int(payload.get("plan_run") or 0), "title": str(payload.get("title") or ""),
            "thread_id": payload.get("thread_id"), "run_id": row["run_id"], "requested_by": row["requested_by"],
            "state": row["state"], "created_at": row["created_at"], "expires_at": row["expires_at"],
            "decided_at": row["decided_at"], "detail": row["detail"]}  # fmt: skip


def _change(row: sqlite3.Row) -> dict[str, Any]:
    """A config change proposed as a git commit (`kind = change`; `variant` change or facts)."""
    payload = _json_or(row["payload"], {})
    found = {name: payload.get(name) for name in ("base_sha", "commit_sha", "ref", "fact_id", "applied_sha",
                                                  "reverted_sha", "thread_id")}  # fmt: skip
    return found | {
        "id": int(row["id"]), "kind": str(payload.get("variant") or "change"), "subject": row["subject"],
        "why": str(payload.get("why") or ""), "effect": str(payload.get("effect") or ""),
        "files": payload.get("files") if isinstance(payload.get("files"), list) else [],
        "summary": payload.get("summary") if isinstance(payload.get("summary"), list) else [],
        "diff": str(payload.get("diff") or ""), "state": row["state"], "detail": row["detail"],
        "requested_by": row["requested_by"], "run_id": row["run_id"], "created_at": row["created_at"],
        "expires_at": row["expires_at"], "decided_at": row["decided_at"], "updated_at": row["updated_at"],
    }  # fmt: skip


def _fact(row: sqlite3.Row) -> dict[str, Any]:
    """A house fact the admin or the agent noted (`kind = fact`)."""
    payload = _json_or(row["payload"], {})
    return {"id": int(row["id"]), "text": str(payload.get("text") or ""), "thread_id": payload.get("thread_id"),
            "turn_id": payload.get("turn_id"), "created_at": row["created_at"], "status": row["state"],
            "decided_at": row["decided_at"], "change_id": payload.get("change_id")}  # fmt: skip


ITEM_JSON = ("need", "actions", "phase", "facts", "agent", "change", "source", "push")
_ITEM_DEFAULTS: dict[str, Any] = {"actions": [], "facts": [], "source": {}, "push": {}}


def _item(row: sqlite3.Row) -> dict[str, Any]:
    found = dict(row)
    for name in ITEM_JSON:
        default = _ITEM_DEFAULTS.get(name)
        raw = found.get(name)
        if raw is None:
            found[name] = default
            continue
        try:
            found[name] = json.loads(raw)
        except TypeError, ValueError:
            found[name] = default
    found["quiet"] = bool(found["quiet"])
    found["needs"] = bool(found["needs"])
    return found


def _update_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "window_day": row["window_day"], "trigger": row["trigger"], "mode": row["mode"],
        "state": row["state"], "plan": _json_or(row["plan"], []), "review": _json_or(row["review"], {}),
        "backup": _json_or(row["backup"], {}), "before": _json_or(row["before"], {}),
        "before_first": _json_or(row["before_first"], {}), "summary": row["summary"],
        "created_at": row["created_at"], "updated_at": row["updated_at"], "finished_at": row["finished_at"],
    }  # fmt: skip


# Admin turns move queued -> running -> ok | refused | failed; agent turns are `ok`;
# system turns carry their card kind (a refusal status, `error`, `summary`, ...).
IN_FLIGHT = ("queued", "running")
RETRYABLE = ("refused", "failed")
RETRY_KEY = "retry:"


@dataclass(frozen=True, slots=True)
class Turn:
    id: int
    thread_id: str
    role: str
    text: str
    at: str
    status: str = "ok"
    meta: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    reply_to: int | None = None
    client_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "thread": self.thread_id, "role": self.role, "text": self.text, "at": self.at,
            "status": self.status, "meta": self.meta, "run_id": self.run_id, "reply_to": self.reply_to,
            "client_id": self.client_id,
        }  # fmt: skip


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=int(row["id"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        owner=str(row["owner"]),
        system_key=row["system_key"],
        schedule=json.loads(row["schedule"]),
        payload=json.loads(row["payload"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        next_fire_at=row["next_fire_at"],
        generation=int(row["generation"]),
        last_fired_at=row["last_fired_at"],
        created_by=str(row["created_by"]),
        thread_id=row["thread_id"],
        updated_at=str(row["updated_at"]),
    )


def upgrade_copy(store_path: Path) -> Path:
    """Where the copy taken before the upgrade from 18 lives."""
    return store_path.with_name(store_path.name + UPGRADE_COPY_SUFFIX)


def _statements(script: str) -> Iterator[str]:
    return (s for s in script.split(";") if s.strip())


class Store:
    """One connection shared by the event loop and executor threads, serialised by a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._depth = 0
        self._touched: list[Touched] = []
        # The item book (`opsd.items`): writes the items a transaction's records show, inside it.
        self.observer: Callable[[list[Touched]], None] | None = None
        # Called after each commit that the observer saw (it publishes the items it wrote).
        self.committed: Callable[[], None] | None = None
        self._observing = False

    # ----------------------------------------------------------- lifecycle

    @classmethod
    def open(cls, path: Path) -> Store:
        store = cls(path)
        store._connect()
        return store

    @classmethod
    def open_or_recover(
        cls,
        path: Path,
        *,
        now: datetime | None = None,
        retry_delays: tuple[float, ...] = OPEN_RETRY_DELAYS_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> tuple[Store, bool]:
        """Open the store (returns recovered=True if a corrupt file was replaced).

        Raises StoreUnavailable for anything that is not corruption.
        """
        for delay in (*retry_delays, None):
            try:
                return cls._open_classified(path), False
            except StoreCorrupt as err:
                stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
                _LOGGER.error("store corrupt; quarantining", extra={"path": str(path), "error": str(err)[:200]})
                for suffix in ("", "-wal", "-shm"):
                    source = Path(f"{path}{suffix}")
                    if source.exists():
                        source.rename(Path(f"{path}.corrupt-{stamp}{suffix}"))
                return cls._open_classified(path), True
            except StoreUnavailable as err:
                if not str(err).startswith("busy") or delay is None:
                    raise
                _LOGGER.warning("store busy; retrying", extra={"delay_s": delay})
                sleep(delay)
        raise StoreUnavailable("unreachable")  # pragma: no cover - the loop always returns or raises

    @classmethod
    def _open_classified(cls, path: Path) -> Store:
        try:
            return cls.open(path)
        except StoreCorrupt, StoreUnavailable:
            raise
        except StoreError as err:
            raise StoreUnavailable(str(err)) from err
        except sqlite3.Error as err:
            code = _error_name(err)
            if code in _CORRUPT_CODES:
                raise StoreCorrupt(f"{code}: {err}") from err
            if code in _BUSY_CODES:
                raise StoreUnavailable(f"busy: {code}: {err}") from err
            raise StoreUnavailable(f"{code or type(err).__name__}: {err}") from err
        except OSError as err:
            raise StoreUnavailable(f"{type(err).__name__}: {err}") from err

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file private before SQLite opens it.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
        os.close(fd)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            check = conn.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise StoreCorrupt(f"integrity check failed: {check[0] if check else 'no answer'}")
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self._fix_modes()
        self.expire_upgrade_copy(datetime.now(UTC))

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Create the schema on an empty file, or upgrade 0.18's schema; refuse every other version."""
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            # A downgrade: the newer version migrated /data/ops.db. Never touched here (storage.md S2).
            _LOGGER.critical(
                "store written by a newer add-on version", extra={"schema": version, "supported": SCHEMA_VERSION}
            )
            raise StoreError(f"store schema {version} is newer than this agent ({SCHEMA_VERSION}): the add-on was "
                             "downgraded; reinstall the newer version, or restore the add-on's /data from the "
                             "backup taken before the upgrade")  # fmt: skip
        tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if version == 0 and not tables:
            self._run(conn, SCHEMA, version=SCHEMA_VERSION)
            return
        if version != UPGRADE_FROM:
            _LOGGER.critical("store schema too old to upgrade", extra={"schema": version, "upgrades": UPGRADE_FROM})
            raise StoreError(f"store schema {version} is older than this agent upgrades ({UPGRADE_FROM}, add-on "
                             "0.18): install 0.18 first and start it once, or restore the add-on's /data from a "
                             "backup")  # fmt: skip
        self._copy_aside(conn)
        reloads = self._read_reloads()
        renames = "".join(f"ALTER TABLE {name} RENAME TO old_{name};" for name in _REBUILT)
        drops = "".join(f"DROP TABLE {name};" for name in _FOLDED)
        self._run(conn, renames + TABLES + ";" + UPGRADE_18 + ";" + drops + INDEXES, version=SCHEMA_VERSION,
                  extra=(lambda c: self._import_reloads(c, reloads)) if reloads is not None else None)  # fmt: skip
        with contextlib.suppress(OSError):
            (self.path.parent / _RELOADS_FILE).unlink()

    def _copy_aside(self, conn: sqlite3.Connection) -> None:
        """Before the upgrade from 18: a consistent copy of the store (SQLite's backup API), private like it.

        For the admin's manual rollback (storage.md S12). Without it the upgrade does not start.
        """
        target = upgrade_copy(self.path)
        temp = target.with_name(f"{target.name}.tmp")
        try:
            temp.unlink(missing_ok=True)
            # Created private first: the copy is never readable by anyone the store is not.
            os.close(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, _FILE_MODE))
            copy = sqlite3.connect(temp)
            try:
                conn.backup(copy)
            finally:
                copy.close()
            os.chmod(temp, _FILE_MODE)
            made, store = temp.stat(), self.path.stat()
            if stat.S_IMODE(made.st_mode) != _FILE_MODE or made.st_uid != store.st_uid:
                raise OSError(f"mode {oct(stat.S_IMODE(made.st_mode))} owner {made.st_uid}, not the store's")
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except (OSError, sqlite3.Error) as err:
            with contextlib.suppress(OSError):
                temp.unlink()
            _LOGGER.critical("store not copied aside; not upgrading", extra={"error": str(err)[:200]})
            raise StoreError(f"could not copy the store aside before the upgrade ({type(err).__name__}: {err}); "
                             "the store is unchanged") from err  # fmt: skip
        _LOGGER.info("store copied aside before the upgrade", extra={"copy": str(target)})

    def expire_upgrade_copy(self, now: datetime) -> bool:
        """Delete the pre-upgrade copy once it is `UPGRADE_COPY_KEEP` old (at a start and the daily prune)."""
        target = upgrade_copy(self.path)
        try:
            made = datetime.fromtimestamp(target.stat().st_mtime, UTC)
        except FileNotFoundError:
            return False
        if now - made < UPGRADE_COPY_KEEP:
            return False
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        _LOGGER.info("pre-upgrade store copy deleted", extra={"copy": str(target)})
        return True

    @staticmethod
    def _run(conn: sqlite3.Connection, script: str, *, version: int,
             extra: Callable[[sqlite3.Connection], None] | None = None) -> None:  # fmt: skip
        """One script and the new `user_version` in one transaction; any failure leaves the file as it was."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _statements(script):
                conn.execute(statement)
            if extra is not None:
                extra(conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        _LOGGER.info("store migrated", extra={"version": version})

    def _read_reloads(self) -> str | None:
        """0.18's reload rate state, verbatim: `opsd.reload` refuses a corrupt one the same way it did."""
        path = self.path.parent / _RELOADS_FILE
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_IMPORT_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(raw) > _MAX_IMPORT_BYTES:
            return "too large"
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _import_reloads(conn: sqlite3.Connection, raw: str) -> None:
        conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                     (RELOAD_KEY, raw, _iso(datetime.now(UTC))))  # fmt: skip

    def _fix_modes(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(f"{self.path}{suffix}", _FILE_MODE)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def schema_version(self) -> int:
        with self._lock:
            return int(self._db().execute("PRAGMA user_version").fetchone()[0])

    def table_counts(self) -> dict[str, int]:
        """Rows per table, for the diagnostics export (table names come from the schema, never from input)."""
        with self._lock:
            conn = self._db()
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            return {n: int(conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0]) for n in names}  # noqa: S608

    def recent_failed_runs(self, *, since: datetime, limit: int = 20) -> list[dict[str, Any]]:
        """Runs since `since` that did not end ok: when, profile and status only (no prompts or output)."""
        rows = self._all(
            "SELECT started_at, profile, status FROM runs"
            " WHERE started_at >= ? AND status NOT IN ('ok', 'running') ORDER BY started_at DESC LIMIT ?",
            (_iso(since), limit),
        )
        return [{"at": r[0], "profile": r[1], "status": r[2]} for r in rows]

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("store is closed")
        return self._conn

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._db().execute(sql, args).fetchone()
            return row

    def _all(self, sql: str, args: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db().execute(sql, args).fetchall())

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction. Nested use joins the outer one (a savepoint), so callers can group writes."""
        with self._lock:
            conn = self._db()
            if self._depth:
                self._depth += 1
                name = f"sp{self._depth}"
                conn.execute(f"SAVEPOINT {name}")
                try:
                    yield conn
                except BaseException:
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                    raise
                finally:
                    self._depth -= 1
                conn.execute(f"RELEASE {name}")
                return
            conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            self._touched = []
            try:
                yield conn
                self._observe(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0
            conn.execute("COMMIT")
            seen, self._touched = bool(self._touched), []
        if seen and self.committed is not None:
            try:
                self.committed()
            except Exception:
                _LOGGER.exception("store commit hook failed")

    def atomic(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """Group several writes (and the items they show) into one transaction."""
        return self._tx()

    def _touch(self, kind: str, ref: Any) -> None:
        if self._depth and not self._observing:
            self._touched.append((kind, ref))

    def _observe(self, conn: sqlite3.Connection) -> None:
        """Before COMMIT: the observer writes the items these records show, in this transaction.

        A failure in it never blocks the record's own change: its writes roll back alone and are logged.
        """
        if not self._touched or self.observer is None or self._observing:
            return
        touched = list(dict.fromkeys(self._touched))
        self._observing = True
        conn.execute("SAVEPOINT observe")
        try:
            self.observer(touched)
        except Exception:
            conn.execute("ROLLBACK TO observe")
            _LOGGER.exception("items not written", extra={"touched": str(touched)[:200]})
        finally:
            conn.execute("RELEASE observe")
            self._observing = False

    # --------------------------------------------------------------- audit

    def audit_begin(
        self,
        *,
        at: datetime,
        run_id: str | None,
        principal: str,
        channel: str,
        capability: str,
        args: dict[str, Any],
        tier: str | None,
        decision: str,
        notify_admin: bool = False,
    ) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO audit (at, run_id, principal, channel, capability, args, tier, decision, outcome,"
                " notify_admin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (_iso(at), run_id, principal, channel, capability, json.dumps(args, sort_keys=True, default=str),
                 tier, decision, int(notify_admin)),
            )  # fmt: skip
            return int(cursor.lastrowid or 0)

    def audit_finish(self, audit_id: int, outcome: str, detail: str = "") -> None:
        with self._tx() as conn:
            conn.execute("UPDATE audit SET outcome = ?, detail = ? WHERE id = ?", (outcome, detail[:500], audit_id))

    def audit(self, *, outcome: str, detail: str = "", **fields: Any) -> int:
        audit_id = self.audit_begin(**fields)
        self.audit_finish(audit_id, outcome, detail)
        return audit_id

    def audit_rows(self, *, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._all("SELECT * FROM audit WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit))
        return [dict(r) for r in rows]

    def prune_audit(self, *, before: datetime, keep_rows: int = 50_000) -> int:
        with self._tx() as conn:
            removed = conn.execute("DELETE FROM audit WHERE at < ?", (_iso(before),)).rowcount
            removed += conn.execute(
                "DELETE FROM audit WHERE id <= (SELECT id FROM audit ORDER BY id DESC LIMIT 1 OFFSET ?)", (keep_rows,)
            ).rowcount
        return removed

    # -------------------------------------------------------- daily counters

    def count(self, day: str, name: str) -> int:
        """A daily counter (`kv` key `count:<day>:<name>`)."""
        return int(self.kv_get(f"{COUNT_KEY}{day}:{name}", "0") or 0)

    def unbump(self, day: str, name: str) -> None:
        """Give one unit of a daily counter back (a request refused before it did anything)."""
        key = f"{COUNT_KEY}{day}:{name}"
        with self._tx() as conn:
            conn.execute("UPDATE kv SET value = CAST(CAST(value AS INTEGER) - 1 AS TEXT) WHERE key = ?"
                         " AND CAST(value AS INTEGER) > 0", (key,))  # fmt: skip

    def bump(self, day: str, name: str, *, limit: int | None = None) -> bool:
        """Increment a daily counter; with `limit`, only if it stays within it (atomic)."""
        key = f"{COUNT_KEY}{day}:{name}"
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            current = int(row[0] or 0) if row else 0
            if limit is not None and current >= limit:
                return False
            conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE"
                         " SET value = excluded.value, updated_at = excluded.updated_at",
                         (key, str(current + 1), f"{day}T00:00:00+00:00"))  # fmt: skip
            return True

    # --------------------------------------------------------------- tasks

    def ensure_system_task(
        self,
        key: str,
        *,
        title: str,
        schedule: dict[str, Any],
        next_fire_at: datetime,
        payload: dict[str, Any],
        now: datetime,
    ) -> Task:
        """Create or reset a system-owned recurring task (idempotent; run at every start)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tasks (kind, title, owner, created_by, system_key, schedule, payload, state,"
                " next_fire_at, created_at, updated_at) VALUES ('recurring', ?, 'system', 'daemon', ?, ?, ?,"
                " 'pending', ?, ?, ?) ON CONFLICT(system_key) DO UPDATE SET title = excluded.title,"
                " schedule = excluded.schedule, payload = excluded.payload, state = 'pending',"
                " next_fire_at = excluded.next_fire_at, updated_at = excluded.updated_at",
                (title, key, json.dumps(schedule), json.dumps(payload), _iso(next_fire_at), _iso(now), _iso(now)),
            )
            row = conn.execute("SELECT * FROM tasks WHERE system_key = ?", (key,)).fetchone()
        return _task(row)

    def system_task(self, key: str) -> Task | None:
        row = self._one("SELECT * FROM tasks WHERE system_key = ?", (key,))
        return _task(row) if row else None

    def get_task(self, task_id: int) -> Task | None:
        row = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return _task(row) if row else None

    def claim_due(self, now: datetime, *, limit: int = 10) -> list[Task]:
        """Move due pending tasks to `running` (compare-and-set, so a task fires once)."""
        claimed: list[Task] = []
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE state = 'pending' AND next_fire_at IS NOT NULL AND next_fire_at <= ?"
                " ORDER BY next_fire_at LIMIT ?",
                (_iso(now), limit),
            ).fetchall()
            for row in rows:
                updated = conn.execute(
                    "UPDATE tasks SET state = 'running', attempts = attempts + 1, updated_at = ?"
                    " WHERE id = ? AND state = 'pending'",
                    (_iso(now), row["id"]),
                ).rowcount
                if updated:
                    self._touch("task", int(row["id"]))
                    claimed.append(_task(conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()))
        return claimed

    def _task_cas(self, task_id: int, sql: str, args: tuple[Any, ...]) -> bool:
        with self._tx() as conn:
            done = bool(conn.execute(sql, args).rowcount)
            if done:
                self._touch("task", task_id)
            return done

    def reschedule(
        self, task_id: int, *, next_fire_at: datetime, now: datetime, payload: dict[str, Any] | None = None
    ) -> bool:
        """A running task goes back to pending at its next fire time."""
        if payload is None:
            return self._task_cas(task_id, "UPDATE tasks SET state = 'pending', next_fire_at = ?, updated_at = ?"
                                  " WHERE id = ? AND state = 'running'",
                                  (_iso(next_fire_at), _iso(now), task_id))  # fmt: skip
        return self._task_cas(task_id, "UPDATE tasks SET state = 'pending', next_fire_at = ?, payload = ?,"
                              " updated_at = ? WHERE id = ? AND state = 'running'",
                              (_iso(next_fire_at), json.dumps(payload), _iso(now), task_id))  # fmt: skip

    def finish_task(self, task_id: int, state: str, *, now: datetime) -> bool:
        if state not in ("done", "cancelled", "failed"):
            raise ValueError(f"not a final state: {state}")
        return self._task_cas(task_id, "UPDATE tasks SET state = ?, next_fire_at = NULL, updated_at = ?"
                              " WHERE id = ? AND state = 'running'", (state, _iso(now), task_id))  # fmt: skip

    def recover_running(self, now: datetime) -> int:
        """After a crash or restart, tasks caught mid-run fire again."""
        with self._tx() as conn:
            ids = [int(r[0]) for r in conn.execute("SELECT id FROM tasks WHERE state = 'running'").fetchall()]
            conn.execute("UPDATE tasks SET state = 'pending', next_fire_at = ?, updated_at = ? WHERE state = 'running'",
                         (_iso(now), _iso(now)))  # fmt: skip
            for task_id in ids:
                self._touch("task", task_id)
        return len(ids)

    def next_due_at(self) -> datetime | None:
        row = self._one("SELECT MIN(next_fire_at) FROM tasks WHERE state = 'pending' AND next_fire_at IS NOT NULL")
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    # ------------------------------------------------- reminders, follow-ups

    def create_task(
        self,
        *,
        kind: str,
        title: str,
        owner: str,
        created_by: str,
        next_fire_at: datetime,
        payload: dict[str, Any],
        idempotency_key: str,
        now: datetime,
        generation: int = 0,
        thread_id: str | None = None,
    ) -> tuple[Task, bool]:
        """Insert a one-off task; the same idempotency key returns the existing task (created=False)."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row is not None:
                return _task(row), False
            cursor = conn.execute(
                "INSERT INTO tasks (kind, title, owner, created_by, payload, state, generation, next_fire_at,"
                " thread_id, idempotency_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (kind, title, owner, created_by, json.dumps(payload), generation, _iso(next_fire_at), thread_id,
                 idempotency_key, _iso(now), _iso(now)),
            )  # fmt: skip
            self._touch("task", int(cursor.lastrowid or 0))
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _task(row), True

    def open_tasks(self, *, limit: int = 50) -> list[Task]:
        """Pending, user-visible tasks (never the system's own)."""
        return self.list_tasks("upcoming", now=datetime.now(UTC), limit=limit)

    def open_investigations(self) -> list[Task]:
        """Follow-ups that are chat investigations, pending or running."""
        rows = self._all(
            "SELECT * FROM tasks WHERE system_key IS NULL AND kind = 'followup' AND state IN ('pending', 'running')"
            " AND json_extract(payload, '$.investigation') = 1 ORDER BY id")  # fmt: skip
        return [_task(r) for r in rows]

    def tasks_created_by(self, created_by: str) -> list[Task]:
        return [_task(r) for r in self._all("SELECT * FROM tasks WHERE created_by = ? ORDER BY id", (created_by,))]

    def count_open_tasks(self, owner: str) -> int:
        row = self._one("SELECT COUNT(*) FROM tasks WHERE system_key IS NULL AND owner = ?"
                        " AND state IN ('pending', 'running')", (owner,))  # fmt: skip
        return int(row[0]) if row else 0

    def cancel_task(self, task_id: int, *, owner: str, now: datetime) -> bool:
        """Only the owner's pending tasks; system tasks can never be cancelled."""
        return self._close_pending(task_id, "cancelled", owner=owner, now=now)

    def mark_done(self, task_id: int, *, now: datetime) -> bool:
        """The admin tapped Done on a (snoozed) reminder: it will not fire again."""
        return self._close_pending(task_id, "done", owner=None, now=now)

    def _close_pending(self, task_id: int, state: str, *, owner: str | None, now: datetime) -> bool:
        # `owner IS NULL` in the parameter means "any owner".
        return self._task_cas(task_id, "UPDATE tasks SET state = ?, next_fire_at = NULL, updated_at = ? WHERE id = ?"
                              " AND system_key IS NULL AND state = 'pending' AND (? IS NULL OR owner = ?)",
                              (state, _iso(now), task_id, owner, owner))  # fmt: skip

    def mark_fired(self, task_id: int, fire_at: str, *, now: datetime) -> bool:
        """Record that this firing is being delivered; False if it already was (at-least-once guard)."""
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE tasks SET last_fired_at = ?, updated_at = ? WHERE id = ?"
                    " AND (last_fired_at IS NULL OR last_fired_at != ?)",
                    (fire_at, _iso(now), task_id, fire_at),
                ).rowcount
            )

    def reopen_task(self, task_id: int, *, next_fire_at: datetime, now: datetime) -> bool:
        """Snooze a delivered (done) one-off task: it fires again later."""
        return self._task_cas(task_id, "UPDATE tasks SET state = 'pending', next_fire_at = ?, updated_at = ?"
                              " WHERE id = ? AND system_key IS NULL AND state = 'done'",
                              (_iso(next_fire_at), _iso(now), task_id))  # fmt: skip

    def list_tasks(self, view: str, *, now: datetime, limit: int = 50) -> list[Task]:
        """The user-visible tasks: `upcoming` (open) or `done` (closed in the last 14 days)."""
        if view == "upcoming":
            sql = (
                "SELECT * FROM tasks WHERE system_key IS NULL AND state IN ('pending', 'running')"
                " ORDER BY next_fire_at LIMIT ?"
            )
            args: tuple[Any, ...] = (limit,)
        elif view == "done":
            sql = (
                "SELECT * FROM tasks WHERE system_key IS NULL AND state IN ('done', 'cancelled', 'failed')"
                " AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?"
            )
            args = (_iso(now - timedelta(days=14)), limit)
        else:
            raise ValueError(f"unknown task view: {view}")
        return [_task(r) for r in self._all(sql, args)]

    def admin_cancel_task(self, task_id: int, *, now: datetime) -> bool:
        """The admin may cancel any open user-visible task (theirs or the agent's), never a system task."""
        return self._close_pending(task_id, "cancelled", owner=None, now=now)

    # --------------------------------------------------------- retry tasks

    def schedule_retry(self, turn_id: int, *, at: datetime, attempt: int, thread_id: str, now: datetime) -> Task:
        """One retry task per admin turn (its `system_key`), rescheduled in place for each attempt."""
        key = f"{RETRY_KEY}{turn_id}"
        payload = json.dumps({"turn_id": turn_id, "attempt": attempt})
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tasks (kind, title, owner, created_by, system_key, payload, state, next_fire_at,"
                " thread_id, idempotency_key, created_at, updated_at) VALUES ('retry', ?, 'admin', 'daemon', ?, ?,"
                " 'pending', ?, ?, ?, ?, ?) ON CONFLICT(system_key) DO UPDATE SET state = 'pending',"
                " payload = excluded.payload, next_fire_at = excluded.next_fire_at,"
                " idempotency_key = excluded.idempotency_key, updated_at = excluded.updated_at",
                (f"Retry message #{turn_id}", key, payload, _iso(at), thread_id, f"{key}:{attempt}", _iso(now),
                 _iso(now)),
            )  # fmt: skip
            row = conn.execute("SELECT * FROM tasks WHERE system_key = ?", (key,)).fetchone()
        return _task(row)

    def cancel_retry(self, turn_id: int, *, now: datetime) -> bool:
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE tasks SET state = 'cancelled', next_fire_at = NULL, updated_at = ?"
                    " WHERE system_key = ? AND state = 'pending'",
                    (_iso(now), f"{RETRY_KEY}{turn_id}"),
                ).rowcount
            )

    # ---------------------------------------------------------------- chat

    def chat_id(self) -> str:
        """The one chat's id (`kv` `chat.main_thread`), or "" before the first message."""
        return self.kv_get(MAIN_THREAD_KEY)

    def create_thread(self, thread_id: str, *, principal: str, channel: str, now: datetime) -> None:
        """Start the one chat as `thread_id`; a no-op once it exists (there is no other thread)."""
        del principal, channel
        with self._tx():
            if self.chat_id():
                return
            self.kv_set(MAIN_THREAD_KEY, thread_id, now=now)
            self.kv_set(CHAT_CREATED_KEY, _iso(now), now=now)
            self.kv_set(READ_KEY, "0", now=now)

    def _turn(self, row: sqlite3.Row, chat: str) -> Turn:
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return Turn(id=int(row["id"]), thread_id=chat, role=str(row["role"]), text=str(row["text"]),
                    at=str(row["at"]), status=str(row["status"]), meta=meta if isinstance(meta, dict) else {},
                    run_id=row["run_id"], reply_to=row["reply_to"], client_id=row["client_id"])  # fmt: skip

    def _chat_or_none(self, thread_id: str) -> str | None:
        chat = self.chat_id()
        return chat if chat and thread_id == chat else None

    def _turn_row(self, conn: sqlite3.Connection, turn_id: int, chat: str) -> Turn:
        return self._turn(conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone(), chat)

    def append_turn(
        self,
        thread_id: str,
        *,
        role: str,
        text: str,
        now: datetime,
        run_id: str | None = None,
        status: str = "ok",
        meta: dict[str, Any] | None = None,
        client_id: str | None = None,
        reply_to: int | None = None,
    ) -> Turn:
        if not text.strip():
            raise ValueError("a turn needs text")
        with self._tx() as conn:
            return self._insert_turn(conn, thread_id, role=role, text=text, now=now, run_id=run_id, status=status,
                                     meta=meta, client_id=client_id, reply_to=reply_to)  # fmt: skip

    def _insert_turn(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        *,
        role: str,
        text: str,
        now: datetime,
        run_id: str | None,
        status: str,
        meta: dict[str, Any] | None,
        client_id: str | None,
        reply_to: int | None,
    ) -> Turn:
        chat = self._chat_or_none(thread_id)
        if chat is None:
            raise StoreError("no such chat")
        cursor = conn.execute(
            "INSERT INTO turns (role, text, at, run_id, status, meta, client_id, reply_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (role, text, _iso(now), run_id, status, json.dumps(meta or {}, default=str), client_id, reply_to),
        )
        return self._turn_row(conn, int(cursor.lastrowid or 0), chat)

    def submit_turn(
        self, thread_id: str, *, text: str, now: datetime, client_id: str | None, meta: dict[str, Any] | None = None
    ) -> tuple[Turn, bool]:
        """Append an admin turn (queued); the same `client_id` returns the existing one."""
        with self._tx() as conn:
            if client_id is not None:
                existing = self.turn_by_client(thread_id, client_id)
                if existing is not None:
                    return existing, False
            turn = self._insert_turn(conn, thread_id, role="admin", text=text, now=now, run_id=None, status="queued",
                                     meta=meta, client_id=client_id, reply_to=None)  # fmt: skip
        return turn, True

    def turn_by_client(self, thread_id: str, client_id: str) -> Turn | None:
        chat = self._chat_or_none(thread_id)
        row = self._one("SELECT * FROM turns WHERE client_id = ?", (client_id,)) if chat else None
        return self._turn(row, chat) if row and chat else None

    def get_turn(self, turn_id: int) -> Turn | None:
        row = self._one("SELECT * FROM turns WHERE id = ?", (turn_id,))
        return self._turn(row, self.chat_id()) if row else None

    def set_turn_status(
        self, turn_id: int, status: str, *, expect: tuple[str, ...], meta: dict[str, Any] | None = None
    ) -> bool:
        """Compare-and-set an admin turn's status (exactly one caller wins a transition)."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None or str(row["status"]) not in expect:
                return False
            merged = {**self._turn(row, "").meta, **(meta or {})}
            conn.execute(
                "UPDATE turns SET status = ?, meta = ? WHERE id = ?", (status, json.dumps(merged, default=str), turn_id)
            )
            return True

    def update_turn_meta(self, turn_id: int, meta: dict[str, Any]) -> Turn | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                return None
            merged = {**self._turn(row, "").meta, **meta}
            conn.execute("UPDATE turns SET meta = ? WHERE id = ?", (json.dumps(merged, default=str), turn_id))
            return self._turn_row(conn, turn_id, self.chat_id())

    def update_card(self, turn_id: int, *, text: str, meta: dict[str, Any], now: datetime) -> Turn | None:
        """Rewrite a daemon-written `system` card in place (an investigation's progress)."""
        del now
        if not text.strip():
            raise ValueError("a card needs text")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ? AND role = 'system'", (turn_id,)).fetchone()
            if row is None:
                return None
            merged = {**self._turn(row, "").meta, **meta}
            conn.execute("UPDATE turns SET text = ?, meta = ? WHERE id = ?",
                         (text, json.dumps(merged, default=str), turn_id))  # fmt: skip
            return self._turn_row(conn, turn_id, self.chat_id())

    def card_by_status(self, thread_id: str, status: str, task_id: int) -> Turn | None:
        """The `system` card of this status that belongs to a task (`meta.task`)."""
        chat = self._chat_or_none(thread_id)
        row = self._one(
            "SELECT * FROM turns WHERE role = 'system' AND status = ? AND json_extract(meta, '$.task') = ?"
            " ORDER BY id DESC LIMIT 1", (status, task_id)) if chat else None  # fmt: skip
        return self._turn(row, chat) if row and chat else None

    def complete_turn(
        self, admin_turn: int, *, text: str, now: datetime, run_id: str | None, meta: dict[str, Any] | None = None
    ) -> Turn | None:
        """The agent's reply and the admin turn's `ok`, in one transaction; None if it was not running."""
        if not text.strip():
            raise ValueError("an agent reply needs text")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ?", (admin_turn,)).fetchone()
            if row is None or row["status"] != "running":
                return None
            conn.execute("UPDATE turns SET status = 'ok' WHERE id = ?", (admin_turn,))
            return self._insert_turn(conn, self.chat_id(), role="agent", text=text, now=now, run_id=run_id,
                                     status="ok", meta=meta, client_id=None, reply_to=admin_turn)  # fmt: skip

    def refuse_turn(
        self,
        admin_turn: int,
        *,
        outcome: str,
        card: str | None,
        text: str,
        now: datetime,
        meta: dict[str, Any] | None = None,
        turn_meta: dict[str, Any] | None = None,
        expect: tuple[str, ...] = IN_FLIGHT,
    ) -> tuple[Turn, Turn | None] | None:
        """Mark an admin turn refused/failed and write (or update) its one card; None if not in `expect`.

        A turn has at most one card: a later refusal of the same turn (an
        automatic retry that is blocked again) updates it in place.
        """
        if outcome not in RETRYABLE:
            raise ValueError(f"not a refusal outcome: {outcome}")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ?", (admin_turn,)).fetchone()
            if row is None or str(row["status"]) not in expect:
                return None
            chat = self.chat_id()
            merged = {**self._turn(row, chat).meta, **(turn_meta or {})}
            conn.execute(
                "UPDATE turns SET status = ?, meta = ? WHERE id = ?",
                (outcome, json.dumps(merged, default=str), admin_turn),
            )
            updated = self._turn_row(conn, admin_turn, chat)
            if card is None:
                return updated, None
            existing = conn.execute(
                "SELECT id FROM turns WHERE reply_to = ? AND role = 'system' AND status != 'summary'", (admin_turn,)
            ).fetchone()
            if existing is None:
                written = self._insert_turn(conn, chat, role="system", text=text, now=now, run_id=None, status=card,
                                            meta=meta, client_id=None, reply_to=admin_turn)  # fmt: skip
                return updated, written
            conn.execute(
                "UPDATE turns SET text = ?, status = ?, meta = ?, at = ? WHERE id = ?",
                (text, card, json.dumps(meta or {}, default=str), _iso(now), existing["id"]),
            )
            return updated, self._turn_row(conn, int(existing["id"]), chat)

    def card_for(self, admin_turn: int) -> Turn | None:
        row = self._one("SELECT * FROM turns WHERE reply_to = ? AND role = 'system' AND status != 'summary'",
                        (admin_turn,))  # fmt: skip
        return self._turn(row, self.chat_id()) if row else None

    def turns_page(self, thread_id: str, *, before: int | None = None, limit: int = 30) -> list[Turn]:
        """Up to `limit` turns before turn id `before` (all if None), oldest first."""
        chat = self._chat_or_none(thread_id)
        if chat is None:
            return []
        rows = self._all("SELECT * FROM turns WHERE id < ? ORDER BY id DESC LIMIT ?",
                         (before if before is not None else sys.maxsize, limit))  # fmt: skip
        return [self._turn(r, chat) for r in reversed(rows)]

    def context_turns(self, thread_id: str, *, after: int, before: int, limit: int) -> list[Turn]:
        """The conversation the model sees: admin and agent turns between two ids, oldest first."""
        chat = self._chat_or_none(thread_id)
        if chat is None:
            return []
        rows = self._all("SELECT * FROM turns WHERE id > ? AND id < ? AND role IN ('admin', 'agent')"
                         " ORDER BY id DESC LIMIT ?", (after, before, limit))  # fmt: skip
        return [self._turn(r, chat) for r in reversed(rows)]

    def count_context(self, thread_id: str, *, after: int, before: int) -> int:
        if self._chat_or_none(thread_id) is None:
            return 0
        row = self._one("SELECT COUNT(*) FROM turns WHERE id > ? AND id < ? AND role IN ('admin', 'agent')",
                        (after, before))  # fmt: skip
        return int(row[0]) if row else 0

    def latest_summary(self, thread_id: str) -> Turn | None:
        chat = self._chat_or_none(thread_id)
        row = self._one("SELECT * FROM turns WHERE role = 'system' AND status = 'summary' ORDER BY id DESC"
                        " LIMIT 1") if chat else None  # fmt: skip
        return self._turn(row, chat) if row and chat else None

    def in_flight(self, thread_id: str) -> Turn | None:
        """The oldest queued or running admin turn of the chat."""
        chat = self._chat_or_none(thread_id)
        row = self._one("SELECT * FROM turns WHERE role = 'admin' AND status IN ('queued', 'running')"
                        " ORDER BY id LIMIT 1") if chat else None  # fmt: skip
        return self._turn(row, chat) if row and chat else None

    def stuck_turns(self) -> list[Turn]:
        """Admin turns left queued or running (by a restart)."""
        rows = self._all("SELECT * FROM turns WHERE role = 'admin' AND status IN ('queued', 'running') ORDER BY id")
        return [self._turn(r, self.chat_id()) for r in rows]

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """The one chat's summary; None for any other id."""
        chat = self._chat_or_none(thread_id)
        if chat is None:
            return None
        row = self._one(
            "SELECT COUNT(*), MAX(at), (SELECT MAX(id) FROM turns WHERE role != 'admin' AND status != 'summary'),"
            " (SELECT COUNT(*) FROM turns WHERE role = 'admin' AND status IN ('queued', 'running')) FROM turns"
        )
        count, last_at, last_reply, busy = (row[0], row[1], row[2], row[3]) if row else (0, None, None, 0)
        created = self.kv_get(CHAT_CREATED_KEY)
        read = int(self.kv_get(READ_KEY, "0") or 0)
        return {"id": chat, "channel": "panel", "created_at": created, "updated_at": last_at or created,
                "turns": int(count or 0), "unread": last_reply is not None and int(last_reply) > read,
                "in_flight": int(busy or 0) > 0}  # fmt: skip

    def mark_read(self, thread_id: str, *, upto: int | None = None) -> bool:
        if self._chat_or_none(thread_id) is None:
            return False
        with self._tx() as conn:
            row = conn.execute("SELECT MAX(id) FROM turns").fetchone()
            last = int(row[0] or 0)
            target = last if upto is None else min(upto, last)
            if target <= int(self.kv_get(READ_KEY, "0") or 0):
                return False
            self.kv_set(READ_KEY, str(target), now=datetime.now(UTC))
            return True

    # --------------------------------------------------------- run ledger

    # A running run counts its reservation; a finished one its real figures.
    _USAGE_SQL = (
        "SELECT COUNT(*), SUM(COALESCE(turns, reserved_turns)), SUM(COALESCE(tokens, reserved_tokens)),"
        " SUM(tokens_cache_read), SUM(COALESCE(seconds, reserved_seconds)),"
        " SUM(COALESCE(share_5h, reserved_share)), SUM(COALESCE(share_7d, reserved_share)),"
        " SUM(CASE WHEN acct_5h IS NOT NULL THEN share_5h ELSE 0 END), SUM(share_estimated) FROM runs WHERE "
    )

    def _usage(self, conn: sqlite3.Connection, clause: str, args: tuple[Any, ...]) -> dict[str, float]:
        row = conn.execute(self._USAGE_SQL + clause, args).fetchone()
        return {
            "runs": int(row[0] or 0),
            "turns": int(row[1] or 0),
            "tokens": int(row[2] or 0),
            "cache_read": int(row[3] or 0),
            "seconds": round(float(row[4] or 0.0), 1),
            "share_5h": float(row[5] or 0.0),
            "share_7d": float(row[6] or 0.0),
            # Only shares the provider actually reported (not reservations or estimates).
            "measured_share_5h": float(row[7] or 0.0),
            # Runs whose share is a run-time estimate (below the reported resolution).
            "estimated_runs": int(row[8] or 0),
        }

    def _typical_share(self, conn: sqlite3.Connection, profile: str, column: str, default: float) -> float:
        """Mean measured share of the profile's last five runs (the next run's reservation)."""
        rows = conn.execute(
            f"SELECT {column} FROM runs WHERE profile = ? AND {column} IS NOT NULL ORDER BY started_at DESC LIMIT 5",  # noqa: S608
            (profile,),
        ).fetchall()
        return sum(float(r[0]) for r in rows) / len(rows) if rows else default

    def _account(self, conn: sqlite3.Connection, at: datetime) -> dict[str, float | None]:
        """The account's latest measured utilisation, if from the last hour."""
        row = conn.execute(
            "SELECT acct_5h, acct_7d FROM runs WHERE ended_at >= ? AND (acct_5h IS NOT NULL OR acct_7d IS NOT NULL)"
            " ORDER BY ended_at DESC LIMIT 1",
            (_iso(at - timedelta(hours=1)),),
        ).fetchone()
        return {"five_hour": row[0], "seven_day": row[1]} if row else {"five_hour": None, "seven_day": None}

    def _scopes(self, conn: sqlite3.Connection, period: Period, at: datetime) -> dict[str, dict[str, float]]:
        return {
            "window": self._usage(conn, "started_at >= ?", (_iso(at - WINDOW),)),
            "day": self._usage(conn, "day = ?", (period.day,)),
            # Rolling seven days, like the subscription's weekly window.
            "week": self._usage(conn, "started_at >= ?", (_iso(at - timedelta(days=7)),)),
        }

    def reserve_run(
        self,
        *,
        run_id: str,
        profile: str,
        budget_class: str,
        period: Period,
        at: datetime,
        max_turns: int,
        budget_usd: float,
        caps: Caps,
    ) -> Reservation | str:
        """Atomically reserve a run's usage: the reservation, or the cap that refused it.

        Checked in one transaction, so concurrent runs cannot both squeeze into
        the last slot: yielding when the account itself is busy, the agent's
        share of the 5-hour and 7-day windows, run time per window (the
        fallback), and the profile's daily run cap. `budget_usd` is the cost cap passed to the provider; the
        ledger does not record cost.
        """
        with self._tx() as conn:
            scopes = self._scopes(conn, period, at)
            account = self._account(conn, at)
            for window, value in account.items():
                if value is not None and value >= caps.account_busy:
                    return f"account_busy:{window}"
            share = self._typical_share(conn, profile, "share_5h", DEFAULT_RUN_SHARE)
            share_7d = self._typical_share(conn, profile, "share_7d", DEFAULT_RUN_SHARE / 5)
            if scopes["window"]["share_5h"] + share > caps.share_stop:
                return "share_cap:five_hour"
            if scopes["week"]["share_7d"] + share_7d > caps.share_stop:
                return "share_cap:seven_day"
            seconds = min(
                caps.seconds_for(profile),
                caps.window_seconds - scopes["window"]["seconds"],
                caps.week_seconds - scopes["week"]["seconds"],
            )
            if seconds < MIN_RUN_SECONDS:
                return "share_cap:run_time"
            mine = self._usage(conn, "day = ? AND profile = ?", (period.day, profile))
            if mine["runs"] >= caps.runs_for(profile):
                return f"run_cap_reached:{profile}"
            conn.execute(
                "INSERT INTO runs (run_id, profile, class, day, week, month, started_at, reserved_turns,"
                " reserved_tokens, reserved_seconds, reserved_share) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, profile, budget_class, period.day, period.week, period.month, _iso(at), max_turns, 0,
                 seconds, max(share, share_7d)),
            )  # fmt: skip
            return Reservation(turns=max_turns, budget_usd=budget_usd, seconds=seconds)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        at: datetime,
        turns: int,
        tokens_in: int,
        tokens_out: int,
        tokens_cache_read: int,
        duration_s: float,
        model: str,
        charge_reservation: bool = False,
        share_5h: float | None = None,
        share_7d: float | None = None,
        acct_5h: float | None = None,
        acct_7d: float | None = None,
        share_estimated: bool = False,
    ) -> None:
        """Replace the reservation by the real figures (or keep a quarter of it, for aborted runs that report
        nothing)."""
        with self._tx() as conn:
            if charge_reservation:
                conn.execute(
                    "UPDATE runs SET status = ?, ended_at = ?, turns = MAX(reserved_turns / 4, ?),"
                    " tokens = MAX(reserved_tokens / 4, ?), duration_s = ?, model = ?,"
                    " seconds = MAX(reserved_seconds / 4, ?), share_5h = COALESCE(?, reserved_share),"
                    " share_7d = COALESCE(?, reserved_share), acct_5h = ?, acct_7d = ?, share_estimated = ?"
                    " WHERE run_id = ?",
                    (status, _iso(at), turns, tokens_in + tokens_out, duration_s, model[:60], duration_s, share_5h,
                     share_7d, acct_5h, acct_7d, int(share_estimated), run_id),
                )  # fmt: skip
                return
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = ?, turns = ?, tokens = ?, tokens_in = ?, tokens_out = ?,"
                " tokens_cache_read = ?, duration_s = ?, model = ?, seconds = ?,"
                " share_5h = COALESCE(?, reserved_share), share_7d = COALESCE(?, reserved_share), acct_5h = ?,"
                " acct_7d = ?, share_estimated = ? WHERE run_id = ?",
                (status, _iso(at), turns, tokens_in + tokens_out, tokens_in, tokens_out, tokens_cache_read,
                 duration_s, model[:60], duration_s, share_5h, share_7d, acct_5h, acct_7d, int(share_estimated),
                 run_id),
            )  # fmt: skip

    def close_stale_runs(self, *, at: datetime) -> int:
        """Runs left `running` by a crash are charged their reservation."""
        with self._tx() as conn:
            return conn.execute(
                "UPDATE runs SET status = 'interrupted', ended_at = ?, turns = reserved_turns,"
                " tokens = reserved_tokens, seconds = reserved_seconds, share_5h = reserved_share,"
                " share_7d = reserved_share WHERE status = 'running'",
                (_iso(at),),
            ).rowcount

    def usage(self, period: Period, *, at: datetime) -> dict[str, Any]:
        """Usage per window/day/week and per profile today, plus the last run (for the sensors)."""
        with self._lock:
            conn = self._db()
            scopes: dict[str, Any] = self._scopes(conn, period, at)
            rows = conn.execute("SELECT DISTINCT profile FROM runs WHERE day = ?", (period.day,)).fetchall()
            scopes["profiles"] = {
                str(r[0]): self._usage(conn, "day = ? AND profile = ?", (period.day, str(r[0]))) for r in rows
            }
            last = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
            scopes["account"] = self._account(conn, at)
        scopes["last_run"] = dict(last) if last else None
        return scopes

    def run_shares(self, *, since: datetime) -> list[tuple[datetime, float, float]]:
        """`(started_at, share_5h, share_7d)` per run since `since` (measured, else the reservation)."""
        rows = self._all("SELECT started_at, COALESCE(share_5h, reserved_share), COALESCE(share_7d, reserved_share)"
                         " FROM runs WHERE started_at >= ? ORDER BY started_at", (_iso(since),))  # fmt: skip
        return [(datetime.fromisoformat(r[0]), float(r[1] or 0.0), float(r[2] or 0.0)) for r in rows]

    def window_frees_at(self, *, at: datetime, span: timedelta = WINDOW) -> datetime | None:
        """When the oldest run in the rolling window ages out (a cap's earliest possible relief)."""
        row = self._one("SELECT MIN(started_at) FROM runs WHERE started_at >= ?", (_iso(at - span),))
        return datetime.fromisoformat(row[0]) + span if row and row[0] else None

    # ------------------------------------------------------------------ kv

    def kv_get(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return str(row[0]) if row else default

    def kv_set(self, key: str, value: str, *, now: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, _iso(now)),
            )
            self._touch("kv", key)

    def kv_prefix(self, prefix: str, *, limit: int = 200) -> dict[str, str]:
        """Non-empty values whose key starts with `prefix` (a literal prefix, not a pattern)."""
        rows = self._all("SELECT key, value FROM kv WHERE substr(key, 1, ?) = ? AND value != '' ORDER BY key LIMIT ?",
                         (len(prefix), prefix, limit))  # fmt: skip
        return {str(r["key"]): str(r["value"]) for r in rows}

    def _kv_json(self, conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        found = _json_or(row[0], {}) if row else None
        return found if isinstance(found, dict) else None

    def _kv_put(self, conn: sqlite3.Connection, key: str, value: dict[str, Any], at: str) -> None:
        conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE"
                     " SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, json.dumps(value, default=str, sort_keys=True), at))  # fmt: skip

    # ------------------------------------------------------------- updates

    def start_update_run(
        self, *, window_day: str, trigger: str, mode: str, plan: list[dict[str, Any]], now: datetime
    ) -> int | None:
        """A new run in `planned`, or None while another run is backing up or installing (never overlap)."""
        with self._tx() as conn:
            busy = conn.execute(f"SELECT id FROM update_runs WHERE state IN {_ACTIVE_SQL}").fetchone()  # noqa: S608
            if busy is not None:
                return None
            cursor = conn.execute(
                "INSERT INTO update_runs (window_day, trigger, mode, state, plan, created_at, updated_at)"
                " VALUES (?, ?, ?, 'planned', ?, ?, ?)",
                (window_day, trigger, mode, json.dumps(plan, default=str), _iso(now), _iso(now)),
            )
            run_id = int(cursor.lastrowid or 0)
            self._touch("run", run_id)
            return run_id

    def move_update_job(self, job_id: int, *, run_id: int, now: datetime) -> bool:
        """Move a job that waits for its reboot into the run of its own that performs it (`UpdateManager`)."""
        with self._tx() as conn:
            cursor = conn.execute("UPDATE update_jobs SET run_id = ?, updated_at = ? WHERE id = ? AND state = ?",
                                  (run_id, _iso(now), job_id, "reboot_pending"))  # fmt: skip
            if cursor.rowcount == 1:
                self._touch("job", job_id)
        return cursor.rowcount == 1

    def update_run(self, run_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM update_runs WHERE id = ?", (run_id,))
        return _update_run(row) if row else None

    def active_update_run(self) -> dict[str, Any] | None:
        row = self._one(f"SELECT * FROM update_runs WHERE state IN {_ACTIVE_SQL} ORDER BY id LIMIT 1")  # noqa: S608
        return _update_run(row) if row else None

    def window_run(self, window_day: str, trigger: str) -> dict[str, Any] | None:
        """The run already started for this window (tasks fire at least once)."""
        row = self._one("SELECT * FROM update_runs WHERE window_day = ? AND trigger = ? ORDER BY id DESC LIMIT 1",
                        (window_day, trigger))  # fmt: skip
        return _update_run(row) if row else None

    def recent_update_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return [_update_run(r) for r in self._all("SELECT * FROM update_runs ORDER BY id DESC LIMIT ?", (limit,))]

    def set_update_run(
        self,
        run_id: int,
        *,
        now: datetime,
        state: str | None = None,
        expect: tuple[str, ...] | None = None,
        **fields: Any,
    ) -> bool:
        """Update a run; with `expect`, only from those states (compare-and-set)."""
        allowed = {"plan", "review", "backup", "before", "before_first", "summary"}
        if set(fields) - allowed:
            raise ValueError(f"unknown update run fields: {sorted(set(fields) - allowed)}")
        sets = ["updated_at = ?"]
        values: list[Any] = [_iso(now)]
        if state is not None:
            sets.append("state = ?")
            values.append(state)
            if state in ("done", "aborted", "failed"):
                sets.append("finished_at = ?")
                values.append(_iso(now))
        for name, value in fields.items():
            sets.append(f"{name} = ?")
            values.append(value if isinstance(value, str) else json.dumps(value, default=str))
        where = "id = ?"
        values.append(run_id)
        if expect:
            where += f" AND state IN ({','.join('?' * len(expect))})"
            values.extend(expect)
        with self._tx() as conn:
            done = bool(conn.execute(f"UPDATE update_runs SET {', '.join(sets)} WHERE {where}", values).rowcount)  # noqa: S608
            if done:
                self._touch("run", run_id)
            return done

    def add_update_jobs(self, run_id: int, jobs: list[dict[str, Any]], *, now: datetime) -> list[UpdateJob]:
        """Insert the run's jobs in order; the idempotency key binds a job to its run, entity and versions."""
        with self._tx() as conn:
            for seq, job in enumerate(jobs):
                conn.execute(
                    "INSERT INTO update_jobs (run_id, seq, entity_id, kind, title, from_version, to_version, target,"
                    " idempotency_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(idempotency_key) DO NOTHING",
                    (run_id, seq, job["entity_id"], job["kind"], job["title"], job["from_version"], job["to_version"],
                     json.dumps(job.get("target", {})),
                     f"{run_id}:{job['entity_id']}:{job['from_version']}:{job['to_version']}", _iso(now)),
                )  # fmt: skip
            for row in conn.execute("SELECT id FROM update_jobs WHERE run_id = ?", (run_id,)).fetchall():
                self._touch("job", int(row[0]))
        return self.update_jobs(run_id)

    def update_jobs(self, run_id: int) -> list[UpdateJob]:
        return [_update_job(r) for r in self._all("SELECT * FROM update_jobs WHERE run_id = ? ORDER BY seq", (run_id,))]

    def update_job(self, job_id: int) -> UpdateJob | None:
        row = self._one("SELECT * FROM update_jobs WHERE id = ?", (job_id,))
        return _update_job(row) if row else None

    def transition_job(
        self,
        job_id: int,
        *,
        expect: tuple[str, ...],
        to: str,
        now: datetime,
        detail: str | None = None,
        regressions: list[Any] | None = None,
        count_install: bool = False,
        count_rollback: bool = False,
    ) -> bool:
        """Compare-and-set a job's state. `count_install` refuses unless no install was ever attempted."""
        if to not in JOB_STATES:
            raise ValueError(f"unknown job state {to}")
        sets = ["state = ?", "updated_at = ?"]
        values: list[Any] = [to, _iso(now)]
        if detail is not None:
            sets.append("detail = ?")
            values.append(detail[:500])
        if regressions is not None:
            sets.append("regressions = ?")
            values.append(json.dumps(regressions[:30], default=str))
        if count_install:
            sets.append("install_attempts = install_attempts + 1")
            sets.append("started_at = ?")
            values.append(_iso(now))
        if count_rollback:
            sets.append("rollback_attempts = rollback_attempts + 1")
        if to in JOB_FINAL:
            sets.append("finished_at = ?")
            values.append(_iso(now))
        where = f"id = ? AND state IN ({','.join('?' * len(expect))})"
        if count_install:
            where += " AND install_attempts = 0"
        if count_rollback:
            where += " AND rollback_attempts = 0"
        with self._tx() as conn:
            done = bool(conn.execute(f"UPDATE update_jobs SET {', '.join(sets)} WHERE {where}",  # noqa: S608
                                     (*values, job_id, *expect)).rowcount)  # fmt: skip
            if done:
                self._touch("job", job_id)
            return done

    def update_jobs_where(self, *, kind: str, states: tuple[str, ...], since: datetime) -> list[UpdateJob]:
        """Jobs of one kind in these states, updated since `since`, oldest first."""
        marks = ",".join("?" * len(states))
        rows = self._all(f"SELECT * FROM update_jobs WHERE kind = ? AND state IN ({marks}) AND updated_at >= ?"  # noqa: S608
                         " ORDER BY id", (kind, *states, _iso(since)))  # fmt: skip
        return [_update_job(r) for r in rows]

    def set_job_target(self, job_id: int, target: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE update_jobs SET target = ? WHERE id = ?", (json.dumps(target, default=str), job_id))
            self._touch("job", job_id)

    def rollback_history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM update_jobs WHERE state IN ('rolled_back', 'rollback_failed', 'rolling_back')"
                         " ORDER BY id DESC LIMIT ?", (limit,))  # fmt: skip
        return [_update_job(r).to_json() for r in rows]

    def recent_update_jobs(self, *, since: datetime) -> list[UpdateJob]:
        """Jobs updated since `since` or not final yet, oldest first."""
        final = ",".join(f"'{s}'" for s in sorted(JOB_FINAL))
        rows = self._all(f"SELECT * FROM update_jobs WHERE updated_at >= ? OR state NOT IN ({final}) ORDER BY id",  # noqa: S608
                         (_iso(since),))  # fmt: skip
        return [_update_job(r) for r in rows]

    # ------------------------------------------------- the approvals record

    @staticmethod
    def _next_number(conn: sqlite3.Connection, kind: str) -> int:
        row = conn.execute("SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) + 1 FROM approvals WHERE kind = ?",
                           (kind,)).fetchone()  # fmt: skip
        return int(row[0])

    def _of_kind(self, kind: str, states: tuple[str, ...] | None, limit: int, *, extra: str = "",
                 args: tuple[Any, ...] = (), order: str = "created_at DESC") -> list[sqlite3.Row]:  # fmt: skip
        where = "kind = ?"
        values: list[Any] = [kind]
        if states:
            where += f" AND state IN ({','.join('?' * len(states))})"
            values.extend(states)
        if extra:
            where += f" AND {extra}"
            values.extend(args)
        return self._all(f"SELECT * FROM approvals WHERE {where} ORDER BY {order} LIMIT ?", (*values, limit))  # noqa: S608

    # ----------------------------------------------------------- approvals

    def create_approval(self, *, approval_id: str, plan: dict[str, Any], plan_hash: str, subject: str,
                        requested_by: str, run_when: str, now: datetime, expires_at: datetime) -> None:  # fmt: skip
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO approvals (kind, id, payload, plan_hash, requested_by, state, created_at, expires_at,"
                " subject, run_when, updated_at) VALUES ('approval', ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (approval_id, json.dumps(plan, sort_keys=True), plan_hash, requested_by, _iso(now), _iso(expires_at),
                 subject, run_when, _iso(now)),
            )  # fmt: skip
            self._touch("approval", approval_id)

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE kind = 'approval' AND id = ?", (approval_id,))
        return _approval(row) if row else None

    def approvals(self, *, states: tuple[str, ...] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [_approval(r) for r in self._of_kind("approval", states, limit)]

    def decide_approval(self, approval_id: str, *, expect: tuple[str, ...], to: str, now: datetime,
                        by: str | None = None, detail: str = "", job: int | None = None) -> bool:  # fmt: skip
        """Compare-and-set an approval's state (single use: `approved` -> `used` happens once)."""
        sets = ["state = ?", "decided_at = COALESCE(decided_at, ?)", "detail = ?", "updated_at = ?"]
        values: list[Any] = [to, _iso(now), detail[:300], _iso(now)]
        if by is not None:
            sets.append("decided_by = ?")
            values.append(by)
        if job is not None:
            sets.append("used_by = ?")
            values.append(job)
        where = f"kind = 'approval' AND id = ? AND state IN ({','.join('?' * len(expect))})"
        with self._tx() as conn:
            done = bool(conn.execute(f"UPDATE approvals SET {', '.join(sets)} WHERE {where}",  # noqa: S608
                                     (*values, approval_id, *expect)).rowcount)  # fmt: skip
            if done:
                self._touch("approval", approval_id)
            return done

    # --------------------------------------------------- approval requests

    def create_proposal(self, *, entity_id: str, from_version: str, to_version: str, plan_run: int, title: str,
                        thread_id: str | None, run_id: str, requested_by: str, now: datetime,
                        expires_at: datetime) -> dict[str, Any]:  # fmt: skip
        """A chat's request that the admin approve an update (never an approval itself)."""
        payload = {"entity_id": entity_id, "from_version": from_version, "to_version": to_version,
                   "plan_run": plan_run, "title": title[:120], "thread_id": thread_id}  # fmt: skip
        with self._tx() as conn:
            number = str(self._next_number(conn, "request"))
            conn.execute(
                "INSERT INTO approvals (kind, id, subject, state, payload, requested_by, run_id, created_at,"
                " expires_at, updated_at) VALUES ('request', ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (number, f"{entity_id}@{to_version}", json.dumps(payload), requested_by[:100], run_id, _iso(now),
                 _iso(expires_at), _iso(now)),
            )  # fmt: skip
            row = conn.execute("SELECT * FROM approvals WHERE kind = 'request' AND id = ?", (number,)).fetchone()
        return _request(row)

    def proposals(self, *, states: tuple[str, ...] = ("open",), run_id: str | None = None,
                  limit: int = 50) -> list[dict[str, Any]]:  # fmt: skip
        extra, args = ("run_id = ?", (run_id,)) if run_id is not None else ("", ())
        rows = self._of_kind("request", states, limit, extra=extra, args=args, order="CAST(id AS INTEGER) DESC")
        return [_request(r) for r in rows]

    def settle_proposal(self, proposal_id: int, *, to: str, now: datetime, detail: str = "") -> bool:
        """Compare-and-set an open request to `done`, `void` or `expired`."""
        if to not in ("done", "void", "expired"):
            raise ValueError(f"not a final proposal state: {to}")
        with self._tx() as conn:
            return bool(conn.execute(
                "UPDATE approvals SET state = ?, decided_at = ?, updated_at = ?, detail = ? WHERE kind = 'request'"
                " AND id = ? AND state = 'open'",
                (to, _iso(now), _iso(now), detail[:200], str(proposal_id))).rowcount)  # fmt: skip

    # --------------------------------------------------- change proposals

    def create_change(self, *, kind: str, subject: str, why: str, effect: str, state: str, requested_by: str,
                      now: datetime, expires_at: datetime, detail: str = "", files: list[dict[str, Any]] | None = None,
                      diff: str = "", base_sha: str | None = None, commit_sha: str | None = None,
                      thread_id: str | None = None, run_id: str | None = None, fact_id: int | None = None,
                      summary: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
        payload = {"variant": kind, "why": why[:600], "effect": effect[:300], "files": files or [],
                   "summary": summary or [], "diff": diff, "base_sha": base_sha, "commit_sha": commit_sha, "ref": None,
                   "fact_id": fact_id, "applied_sha": None, "reverted_sha": None, "thread_id": thread_id}  # fmt: skip
        with self._tx() as conn:
            number = self._next_number(conn, "change")
            conn.execute(
                "INSERT INTO approvals (kind, id, subject, state, payload, detail, requested_by, run_id, created_at,"
                " expires_at, updated_at) VALUES ('change', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(number), subject[:120], state, json.dumps(payload), detail[:300], requested_by[:100], run_id,
                 _iso(now), _iso(expires_at), _iso(now)),
            )  # fmt: skip
            self._touch("change", number)
            row = conn.execute("SELECT * FROM approvals WHERE kind = 'change' AND id = ?", (str(number),)).fetchone()
        return _change(row)

    def change(self, change_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE kind = 'change' AND id = ?", (str(change_id),))
        return _change(row) if row else None

    def changes(self, *, states: tuple[str, ...] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [_change(r) for r in self._of_kind("change", states, limit, order="CAST(id AS INTEGER) DESC")]

    def move_change(self, change_id: int, *, expect: tuple[str, ...], to: str, now: datetime,
                    **fields: Any) -> bool:  # fmt: skip
        """Compare-and-set a proposal's state (and set some fields with it)."""
        unknown = set(fields) - _CHANGE_PAYLOAD - _CHANGE_COLUMNS
        if unknown:
            raise ValueError(f"unknown change fields: {sorted(unknown)}")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE kind = 'change' AND id = ?", (str(change_id),)).fetchone()
            if row is None or row["state"] not in expect:
                return False
            payload = _json_or(row["payload"], {}) | {k: v for k, v in fields.items() if k in _CHANGE_PAYLOAD}
            sets = ["state = ?", "updated_at = ?", "payload = ?"]
            values: list[Any] = [to, _iso(now), json.dumps(payload)]
            for name in sorted(set(fields) & _CHANGE_COLUMNS):
                sets.append(f"{name} = ?")
                values.append(fields[name])
            conn.execute(f"UPDATE approvals SET {', '.join(sets)} WHERE kind = 'change' AND id = ? AND state = ?",  # noqa: S608
                         (*values, str(change_id), row["state"]))  # fmt: skip
            self._touch("change", change_id)
            return True

    # ---------------------------------------------------------------- facts

    def add_fact(self, text: str, *, thread_id: str | None, turn_id: int | None, now: datetime) -> int:
        payload = {"text": text[:MAX_FACT_CHARS], "thread_id": thread_id, "turn_id": turn_id, "change_id": None}
        with self._tx() as conn:
            number = self._next_number(conn, "fact")
            conn.execute("INSERT INTO approvals (kind, id, state, payload, created_at, updated_at)"
                         " VALUES ('fact', ?, 'pending', ?, ?, ?)", (str(number), json.dumps(payload), _iso(now),
                                                                     _iso(now)))  # fmt: skip
            self._touch("fact", number)
            return number

    def fact(self, fact_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE kind = 'fact' AND id = ?", (str(fact_id),))
        return _fact(row) if row else None

    def list_facts(self, *, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        """Pending proposals, plus the ones decided in the last 30 days."""
        rows = self._of_kind("fact", None, limit, extra="(state = 'pending' OR decided_at >= ?)",
                             args=(_iso(now - timedelta(days=30)),),
                             order="state != 'pending', created_at DESC")  # fmt: skip
        return [_fact(r) for r in rows]

    def decide_fact(self, fact_id: int, status: str, *, now: datetime) -> bool:
        if status not in ("applied", "rejected"):
            raise ValueError(f"not a decision: {status}")
        with self._tx() as conn:
            done = bool(conn.execute("UPDATE approvals SET state = ?, decided_at = ?, updated_at = ?"
                                     " WHERE kind = 'fact' AND id = ? AND state = 'pending'",
                                     (status, _iso(now), _iso(now), str(fact_id))).rowcount)  # fmt: skip
            if done:
                self._touch("fact", fact_id)
            return done

    def link_fact(self, fact_id: int, change_id: int) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE approvals SET payload = json_set(payload, '$.change_id', ?) WHERE kind = 'fact'"
                         " AND id = ?", (change_id, str(fact_id)))  # fmt: skip
            self._touch("fact", fact_id)

    # ------------------------------------------------------- quiet hours

    def hold_push(self, message: str, *, title: str, now: datetime) -> int:
        """Hold a push for after quiet hours (`kv` `push.held:<n>`), across restarts."""
        with self._tx() as conn:
            row = conn.execute("SELECT MAX(key) FROM kv WHERE substr(key, 1, ?) = ?", (len(HELD_KEY), HELD_KEY))
            last = row.fetchone()[0]
            number = int(str(last)[len(HELD_KEY) :]) + 1 if last else 1
            self._kv_put(conn, f"{HELD_KEY}{number:012d}", {"held_at": _iso(now), "message": message,
                                                            "title": title[:120], "state": "held", "outcome": "",
                                                            "settled_at": None}, _iso(now))  # fmt: skip
            return number

    def held_pushes(self, *, states: tuple[str, ...] = ("held",), since: datetime | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:  # fmt: skip
        found = []
        for key, raw in self.kv_prefix(HELD_KEY, limit=10_000).items():
            value = _json_or(raw, {})
            if value.get("state") not in states or (since is not None and str(value.get("held_at")) < _iso(since)):
                continue
            found.append(value | {"id": int(key[len(HELD_KEY) :])})
        return found[:limit]

    def settle_held(self, ids: list[int], *, to: str, outcome: str, now: datetime) -> None:
        if to not in ("delivered", "undelivered"):
            raise ValueError(f"not a final held-push state: {to}")
        with self._tx() as conn:
            for number in ids:
                key = f"{HELD_KEY}{number:012d}"
                value = self._kv_json(conn, key)
                if value is not None and value.get("state") == "held":
                    self._kv_put(conn, key, value | {"state": to, "outcome": outcome[:40], "settled_at": _iso(now)},
                                 _iso(now))  # fmt: skip

    # ------------------------------------------------------ push actions

    def issue_action(
        self, action_id: str, *, kind: str, tag: str, thread_id: str | None, now: datetime, expires_at: datetime
    ) -> None:
        """An action id put on an outbound push (`kv` `push.action:<id>`); an id is never issued twice."""
        value = {"kind": kind, "tag": tag, "thread_id": thread_id, "issued_at": _iso(now),
                 "expires_at": _iso(expires_at), "used_at": None}  # fmt: skip
        with self._tx() as conn:
            conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                         (f"{ACTION_KEY}{action_id}", json.dumps(value, sort_keys=True), _iso(now)))  # fmt: skip

    def issued_action(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._kv_json(self._db(), f"{ACTION_KEY}{action_id}")
        return value | {"action_id": action_id} if value is not None else None

    def consume_action(self, action_id: str, *, now: datetime) -> dict[str, Any] | None:
        """Single use: the action is marked used in the same transaction that reads it."""
        key = f"{ACTION_KEY}{action_id}"
        with self._tx() as conn:
            value = self._kv_json(conn, key)
            if value is None or value.get("used_at") is not None or str(value.get("expires_at")) <= _iso(now):
                return None
            used = value | {"used_at": _iso(now)}
            self._kv_put(conn, key, used, _iso(now))
        return used | {"action_id": action_id}

    # ------------------------------------------------------------ retention

    def prune(self, *, now: datetime, keep: timedelta = timedelta(days=90)) -> dict[str, int]:
        """Bound the store: expired/used actions, old runs and counters, decided proposals, resolved items.

        Runs and counters are kept at least 8 days, so the rolling 7-day
        window and today's counters are never pruned away.
        """
        self.expire_upgrade_copy(now)
        cutoff = _iso(now - max(keep, MIN_KEEP))
        week, quarter = _iso(now - timedelta(days=7)), _iso(now - timedelta(days=90))
        with self._tx() as conn:
            actions = conn.execute(
                "DELETE FROM kv WHERE substr(key, 1, ?) = ? AND (json_extract(value, '$.expires_at') < ?"
                " OR (json_extract(value, '$.used_at') IS NOT NULL AND json_extract(value, '$.used_at') < ?))",
                (len(ACTION_KEY), ACTION_KEY, _iso(now), week),
            ).rowcount
            conn.execute("DELETE FROM approvals WHERE kind = 'fact' AND state != 'pending' AND decided_at < ?",
                         (quarter,))  # fmt: skip
            conn.execute("DELETE FROM approvals WHERE kind = 'request' AND state != 'open' AND decided_at < ?",
                         (quarter,))  # fmt: skip
            # Resolved items (never one in Needs you) and their events.
            conn.execute("DELETE FROM items WHERE needs = 0 AND resolved_at IS NOT NULL AND resolved_at < ?",
                         (quarter,))  # fmt: skip
            conn.execute("DELETE FROM item_events WHERE item_id NOT IN (SELECT id FROM items) AND at < ?", (quarter,))
            conn.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ? AND json_extract(value, '$.state') != 'held'"
                         " AND json_extract(value, '$.settled_at') < ?", (len(HELD_KEY), HELD_KEY, week))  # fmt: skip
            conn.execute("DELETE FROM kv WHERE key LIKE 'investigation.cancel:%' AND updated_at < ?", (week,))
            runs = conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,)).rowcount
            counters = conn.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ? AND substr(key, ?, 10) < ?",
                                    (len(COUNT_KEY), COUNT_KEY, len(COUNT_KEY) + 1, cutoff[:10])).rowcount  # fmt: skip
            conn.execute(
                "DELETE FROM update_runs WHERE state IN ('done', 'aborted', 'failed') AND updated_at < ?"
                " AND (json_extract(backup, '$.slug') IS NULL OR json_extract(backup, '$.deleted_at') IS NOT NULL)",
                (_iso(now - UPDATE_KEEP),),
            )
            conn.execute("DELETE FROM kv WHERE (key LIKE 'update_review:%' OR key LIKE 'update_seen:%')"
                         " AND updated_at < ?", (_iso(now - UPDATE_KEEP),))  # fmt: skip
            tasks = conn.execute(
                "DELETE FROM tasks WHERE (system_key IS NULL OR kind = 'retry')"
                " AND state IN ('done', 'cancelled', 'failed') AND updated_at < ?",
                (cutoff,),
            ).rowcount
        return {"actions": actions, "runs": runs, "counters": counters, "tasks": tasks}

    # ---------------------------------------------------------------- items

    _ITEM_COLUMNS = ("id", "key", "kind", "kicker", "ref", "title", "state", "gist", "need", "about", "actions",
                     "phase", "follows", "facts", "agent", "change", "footnote", "quiet", "revision", "as_of", "at",
                     "needs_since", "resolved_at", "created_at", "updated_at", "source", "push")  # fmt: skip

    def item(self, item_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM items WHERE id = ?", (item_id,))
        return _item(row) if row else None

    def item_by_key(self, key: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM items WHERE key = ?", (key,))
        return _item(row) if row else None

    def items_all(self, *, open_only: bool = False, prefix: str | None = None) -> list[dict[str, Any]]:
        """Items, optionally only unresolved ones (or in Needs you), optionally of one key prefix."""
        where = ["1 = 1"]
        args: list[Any] = []
        if open_only:
            where.append("(resolved_at IS NULL OR needs = 1)")
        if prefix is not None:
            where.append("substr(key, 1, ?) = ?")
            args.extend([len(prefix), prefix])
        return [_item(r) for r in self._all(f"SELECT * FROM items WHERE {' AND '.join(where)}", args)]  # noqa: S608

    def items_due(self, now: datetime) -> list[dict[str, Any]]:
        """Items whose source asked to be composed again by now (`source.recheck_at`)."""
        rows = self._all("SELECT * FROM items WHERE json_extract(source, '$.recheck_at') <= ?", (_iso(now),))
        return [_item(r) for r in rows]

    def clear_recheck(self, item_id: str) -> None:
        """Forget an item's `recheck_at` once its recompose is done (never moves the revision)."""
        with self._tx() as conn:
            conn.execute("UPDATE items SET source = json_remove(source, '$.recheck_at') WHERE id = ?", (item_id,))

    def items_since(self, since: datetime) -> list[dict[str, Any]]:
        """Unresolved items, items in Needs you, and items resolved since `since` (for Home)."""
        rows = self._all("SELECT * FROM items WHERE resolved_at IS NULL OR needs = 1 OR resolved_at >= ?",
                         (_iso(since),))  # fmt: skip
        return [_item(r) for r in rows]

    def put_item(self, record: dict[str, Any]) -> None:
        """Insert or replace one item (the caller computed every column)."""
        values = []
        for name in self._ITEM_COLUMNS:
            value = record.get(name)
            if name in ITEM_JSON:
                value = (
                    None
                    if value is None and name not in _ITEM_DEFAULTS
                    else json.dumps(value if value is not None else _ITEM_DEFAULTS[name], default=str, sort_keys=True)
                )
            elif name == "quiet":
                value = int(bool(value))
            values.append(value)
        marks = ", ".join("?" * len(self._ITEM_COLUMNS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._ITEM_COLUMNS if c != "id")
        with self._tx() as conn:
            conn.execute(f"INSERT INTO items ({', '.join(self._ITEM_COLUMNS)}) VALUES ({marks})"  # noqa: S608
                         f" ON CONFLICT(id) DO UPDATE SET {updates}", values)  # fmt: skip
            self._touch("item", record.get("id"))

    def set_item_fields(self, item_id: str, **fields: Any) -> None:
        """Columns that never change an item's revision (`as_of`, `push`)."""
        allowed = {"as_of", "push"}
        if set(fields) - allowed:
            raise ValueError(f"not a silent item field: {sorted(set(fields) - allowed)}")
        sets = [f"{name} = ?" for name in fields]
        values = [
            json.dumps(v, default=str) if name in ITEM_JSON and v is not None else v for name, v in fields.items()
        ]
        with self._tx() as conn:
            conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", (*values, item_id))  # noqa: S608

    def add_item_event(self, item_id: str, *, at: datetime | str, actor: str, text: str, revision: int) -> None:
        when = at if isinstance(at, str) else _iso(at)
        with self._tx() as conn:
            conn.execute("INSERT INTO item_events (item_id, at, actor, text, revision) VALUES (?, ?, ?, ?, ?)",
                         (item_id, when, actor, text[:500], revision))  # fmt: skip

    def item_events(self, item_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Oldest first (the last `limit`)."""
        rows = self._all("SELECT at, actor, text, revision FROM item_events WHERE item_id = ? ORDER BY id DESC"
                         " LIMIT ?", (item_id, limit))  # fmt: skip
        return [dict(r) for r in reversed(rows)]

    def recent_events(self, *, since: datetime, limit: int) -> list[dict[str, Any]]:
        """The timeline across items since `since`, newest first (the chat's activity context)."""
        rows = self._all(
            "SELECT e.id, e.at, e.actor, e.text, i.id AS item, i.kind, i.title, i.needs, i.agent FROM item_events e"
            " JOIN items i ON i.id = e.item_id WHERE e.at >= ? ORDER BY e.id DESC LIMIT ?",
            (_iso(since), limit))  # fmt: skip
        return [dict(r) | {"needs": bool(r["needs"]), "agent": _json_or(r["agent"], {})} for r in rows]

    def needs_count(self) -> int:
        """The Needs you badge: one query, never counted anywhere else."""
        row = self._one("SELECT count(*) FROM items WHERE needs = 1")
        return int(row[0]) if row else 0

    def items_page(self, *, kinds: tuple[str, ...] | None, needs_only: bool, before: tuple[str, str] | None,
                   limit: int) -> list[dict[str, Any]]:  # fmt: skip
        """History: newest `at` first, keyset-paged by (`at`, `id`)."""
        where = ["1 = 1"]
        args: list[Any] = []
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        if needs_only:
            where.append("needs = 1")
        if before is not None:
            where.append("(at < ? OR (at = ? AND id < ?))")
            args.extend([before[0], before[0], before[1]])
        rows = self._all(f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT ?",  # noqa: S608
                         (*args, limit))  # fmt: skip
        return [_item(r) for r in rows]

    def next_activity(self) -> int:
        """The next standalone entry's number (`a-<n>`), never reused after a prune."""
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (ACTIVITY_SEQ_KEY,)).fetchone()
            number = int(row[0] or 0) + 1 if row else 1
            conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE"
                         " SET value = excluded.value, updated_at = excluded.updated_at",
                         (ACTIVITY_SEQ_KEY, str(number), _iso(datetime.now(UTC))))  # fmt: skip
            return number


def checkpoint_main(path: Path, *, timeout_s: float = 60.0) -> int:
    """`opsd checkpoint`: run from the add-on's `backup_pre` hook.

    Opens the file directly (no migrations, no integrity check) and never
    fails the backup: a busy or broken store is reported and the backup
    goes ahead with the WAL file next to it.
    """
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(path, timeout=timeout_s)
        try:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as err:
        print(f"ops-backup-pre: checkpoint skipped: {err}", file=sys.stderr)
        return 0
    if busy and busy[0]:
        print("ops-backup-pre: checkpoint incomplete (store busy); backup includes the WAL", file=sys.stderr)
    return 0
