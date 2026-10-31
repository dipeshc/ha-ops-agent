"""Durable agent state in SQLite (`/data/ops.db`, root only, 0600).

Holds what needs transactions or queries: the audit log of every capability
request, the task schedule (with compare-and-set claims), the budget ledger
and daily counters, issued notification actions (single use), plus the
approval and conversation tables that later phases fill. Small singletons
(options, `state.json`, `baseline.json`) stay in JSON files.

Schema changes are numbered migrations applied in one transaction and
tracked in `PRAGMA user_version`. Opening is deliberately conservative:
- only a file SQLite reports as corrupt or not a database, or one that fails
  `quick_check`, is moved aside and recreated (the store holds no policy and
  the system tasks are re-created at every start);
- a locked or busy database is retried with backoff;
- anything else (disk full, I/O error, permissions, a schema newer than this
  code) is never "repaired": `StoreUnavailable` is raised and the daemon runs
  degraded, without store-dependent features, and alerts the admin.

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
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opsd.budget import ACCOUNT_BUSY, DEFAULT_RUN_SHARE, MIN_RUN_SECONDS, WINDOW, Caps, Period, Reservation

_LOGGER = logging.getLogger(__name__)

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE audit (
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
    CREATE INDEX audit_at ON audit(at);
    CREATE INDEX audit_run ON audit(run_id);

    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('reminder', 'followup', 'watch', 'window', 'intent', 'recurring')),
        title TEXT NOT NULL,
        owner TEXT NOT NULL,
        created_by TEXT NOT NULL,
        system_key TEXT UNIQUE,
        schedule TEXT NOT NULL DEFAULT '{}',
        condition TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'running', 'waiting_approval', 'done', 'cancelled', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 0,
        next_fire_at TEXT,
        expires_at TEXT,
        thread_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX tasks_due ON tasks(state, next_fire_at);

    -- Filled by approvals.
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        plan TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'approved', 'declined', 'expired', 'used', 'void')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT
    );

    CREATE TABLE threads (
        id TEXT PRIMARY KEY,
        principal TEXT NOT NULL,
        channel TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT
    );
    CREATE TABLE turns (
        id INTEGER PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'system')),
        text TEXT NOT NULL,
        at TEXT NOT NULL,
        run_id TEXT
    );

    CREATE TABLE spend (
        day TEXT NOT NULL,
        class TEXT NOT NULL,
        cost_usd REAL NOT NULL DEFAULT 0,
        runs INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, class)
    );
    CREATE TABLE counters (
        day TEXT NOT NULL,
        name TEXT NOT NULL,
        n INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, name)
    );

    -- Action ids put on outbound pushes. An inbound tap is honoured only for
    -- an id issued here, unexpired and not yet used.
    CREATE TABLE issued_actions (
        action_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        tag TEXT NOT NULL,
        thread_id TEXT,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );
    """,
    # 2: at-least-once firing needs idempotent executors (see module docstring).
    """
    ALTER TABLE tasks ADD COLUMN idempotency_key TEXT;
    CREATE UNIQUE INDEX tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
    """,
    # 3: reminders, follow-ups and chat threads.
    """
    ALTER TABLE tasks ADD COLUMN last_fired_at TEXT;
    ALTER TABLE threads ADD COLUMN turns INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX turns_thread ON turns(thread_id, id);
    """,
    # 4: per-run usage ledger with reservation, AI control state.
    """
    CREATE TABLE runs (
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
        reserved_usd REAL NOT NULL,
        turns INTEGER,
        tokens INTEGER,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL,
        duration_s REAL NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        reserved_seconds REAL NOT NULL DEFAULT 0,
        seconds REAL,
        reserved_share REAL NOT NULL DEFAULT 0,
        share_5h REAL,
        share_7d REAL,
        acct_5h REAL,
        acct_7d REAL
    );
    CREATE INDEX runs_day ON runs(day);
    CREATE INDEX runs_started ON runs(started_at);
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
    """,
    # 5: chat UX. Turn status and meta (refusal cards are `system` turns), idempotent
    # submission, thread titles, sources and read marks, and the `retry` task kind
    # (a CHECK change, so the tasks table is rebuilt).
    """
    ALTER TABLE turns ADD COLUMN status TEXT NOT NULL DEFAULT 'ok';
    ALTER TABLE turns ADD COLUMN meta TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE turns ADD COLUMN client_id TEXT;
    ALTER TABLE turns ADD COLUMN reply_to INTEGER;
    CREATE UNIQUE INDEX turns_client ON turns(thread_id, client_id) WHERE client_id IS NOT NULL;
    CREATE INDEX turns_reply ON turns(reply_to) WHERE reply_to IS NOT NULL;
    ALTER TABLE threads ADD COLUMN title TEXT NOT NULL DEFAULT '';
    ALTER TABLE threads ADD COLUMN source TEXT NOT NULL DEFAULT '';
    ALTER TABLE threads ADD COLUMN last_read_turn_id INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX threads_updated ON threads(updated_at);
    CREATE TABLE tasks_v5 (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL
            CHECK (kind IN ('reminder', 'followup', 'watch', 'window', 'intent', 'recurring', 'retry')),
        title TEXT NOT NULL,
        owner TEXT NOT NULL,
        created_by TEXT NOT NULL,
        system_key TEXT UNIQUE,
        schedule TEXT NOT NULL DEFAULT '{}',
        condition TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'running', 'waiting_approval', 'done', 'cancelled', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 0,
        next_fire_at TEXT,
        expires_at TEXT,
        thread_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        idempotency_key TEXT,
        last_fired_at TEXT
    );
    INSERT INTO tasks_v5 (id, kind, title, owner, created_by, system_key, schedule, condition, payload, state,
        attempts, generation, next_fire_at, expires_at, thread_id, created_at, updated_at, idempotency_key,
        last_fired_at)
        SELECT id, kind, title, owner, created_by, system_key, schedule, condition, payload, state, attempts,
        generation, next_fire_at, expires_at, thread_id, created_at, updated_at, idempotency_key, last_fired_at
        FROM tasks;
    DROP TABLE tasks;
    ALTER TABLE tasks_v5 RENAME TO tasks;
    CREATE INDEX tasks_due ON tasks(state, next_fire_at);
    CREATE UNIQUE INDEX tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
    """,
    # 6: shares below the reported resolution are run-time estimates, flagged. Runs that
    # recorded a measured 0 were only flagged here (older versions re-estimated them
    # at start; they have long aged out of every window). Empty agent turns are dropped.
    """
    ALTER TABLE runs ADD COLUMN share_estimated INTEGER NOT NULL DEFAULT 0;
    UPDATE runs SET share_estimated = 1
        WHERE status != 'running' AND acct_5h IS NOT NULL AND (share_5h = 0 OR share_7d = 0);
    DELETE FROM turns WHERE role = 'agent' AND trim(text) = '';
    """,
    # 7: thread management (pin, archive, titles) and proposed house facts.
    """
    ALTER TABLE threads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE threads ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE threads ADD COLUMN auto_title TEXT NOT NULL DEFAULT '';
    ALTER TABLE threads ADD COLUMN user_title TEXT NOT NULL DEFAULT '';
    ALTER TABLE threads ADD COLUMN titled_at_turns INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE fact_proposals (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        thread_id TEXT,
        turn_id INTEGER,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'dismissed')),
        decided_at TEXT
    );
    CREATE INDEX fact_proposals_status ON fact_proposals(status, created_at);
    """,
    # 8: updates. One `update_runs` row per window (plan, backup, health
    # before), one `update_jobs` row per update with a compare-and-set state machine.
    """
    CREATE TABLE update_runs (
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
    CREATE INDEX update_runs_day ON update_runs(window_day, trigger);
    CREATE INDEX update_runs_state ON update_runs(state);
    CREATE TABLE update_jobs (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES update_runs(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        entity_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'installing', 'verifying', 'succeeded',
            'rolling_back', 'rolled_back', 'rollback_failed', 'failed', 'skipped')),
        install_attempts INTEGER NOT NULL DEFAULT 0,
        rollback_attempts INTEGER NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT '',
        regressions TEXT NOT NULL DEFAULT '[]',
        idempotency_key TEXT NOT NULL UNIQUE,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX update_jobs_run ON update_jobs(run_id, seq);
    CREATE INDEX update_jobs_state ON update_jobs(state);
    """,
    # 9: approvals use the `approvals` table: what is approved (`subject`,
    # entity@version), when to run it, and the one job that consumed it.
    """
    ALTER TABLE approvals ADD COLUMN subject TEXT NOT NULL DEFAULT '';
    ALTER TABLE approvals ADD COLUMN run_when TEXT NOT NULL DEFAULT 'window';
    ALTER TABLE approvals ADD COLUMN used_by_job INTEGER;
    ALTER TABLE approvals ADD COLUMN detail TEXT NOT NULL DEFAULT '';
    CREATE INDEX approvals_subject ON approvals(subject, state);
    """,
    # 10: a chat asked the admin to approve an update (`approval.request`). A
    # separate table on purpose: nothing in the approval state machine can ever read a
    # proposal as a pending or approved approval.
    """
    CREATE TABLE approval_proposals (
        id INTEGER PRIMARY KEY,
        entity_id TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        plan_run INTEGER NOT NULL,
        title TEXT NOT NULL,
        thread_id TEXT,
        run_id TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'void', 'expired')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        detail TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX approval_proposals_state ON approval_proposals(state, entity_id);
    CREATE INDEX approval_proposals_run ON approval_proposals(run_id);
    """,
    # 11: config changes proposed as git commits on refs/ops/proposals/<id>; `files`
    # holds each path with its old and new blob and SHA-256 (the approval binds to them).
    """
    CREATE TABLE change_proposals (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('change', 'facts')),
        subject TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT '',
        effect TEXT NOT NULL DEFAULT '',
        files TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '[]',
        diff TEXT NOT NULL DEFAULT '',
        base_sha TEXT,
        commit_sha TEXT,
        ref TEXT,
        state TEXT NOT NULL CHECK (state IN ('refused', 'open', 'applying', 'applied', 'reverting', 'reverted',
            'failed', 'stale', 'declined', 'expired')),
        detail TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL,
        thread_id TEXT,
        run_id TEXT,
        fact_id INTEGER,
        applied_sha TEXT,
        reverted_sha TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX change_proposals_state ON change_proposals(state, created_at);
    ALTER TABLE fact_proposals ADD COLUMN change_id INTEGER;
    """,
    # 12: quiet hours. Non-urgent pushes held until the window ends, across restarts.
    """
    CREATE TABLE held_pushes (
        id INTEGER PRIMARY KEY,
        held_at TEXT NOT NULL,
        message TEXT NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'delivered', 'undelivered')),
        outcome TEXT NOT NULL DEFAULT '',
        settled_at TEXT
    );
    CREATE INDEX held_pushes_state ON held_pushes(state, id);
    """,
    # 13: one chat plus an activity log. Every earlier thread becomes a read-only
    # "Earlier conversation" entry in the log (pinned, so it is never pruned); no turn is lost.
    """
    CREATE TABLE activity (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        needs_admin INTEGER NOT NULL DEFAULT 0,
        ref TEXT NOT NULL DEFAULT '{}',
        turn_id INTEGER
    );
    CREATE INDEX activity_at ON activity(at);
    INSERT INTO activity (at, kind, title, detail, ref)
        SELECT updated_at, 'conversation',
            CASE WHEN user_title != '' THEN user_title WHEN auto_title != '' THEN auto_title
                 WHEN title != '' THEN title ELSE 'Conversation' END,
            turns || ' messages (earlier conversation, read only)', json_object('thread', id)
        FROM threads ORDER BY updated_at;
    UPDATE threads SET pinned = 1, archived = 1;
    """,
    # 14: the earlier conversations go: every thread but the one
    # chat (`chat.main_thread` in kv), their turns (cascade) and their activity entries. Rows that
    # pointed at them keep existing without the link; pending retries of their turns are cancelled.
    # Without a recorded main chat nothing is deleted or unlinked (NOT IN an empty set is true).
    """
    DELETE FROM activity WHERE kind = 'conversation';
    UPDATE tasks SET state = 'cancelled', next_fire_at = NULL
        WHERE kind = 'retry' AND state = 'pending' AND thread_id IS NOT NULL
        AND thread_id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    UPDATE tasks SET thread_id = NULL
        WHERE thread_id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    UPDATE fact_proposals SET thread_id = NULL, turn_id = NULL
        WHERE thread_id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    UPDATE approval_proposals SET thread_id = NULL
        WHERE thread_id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    UPDATE change_proposals SET thread_id = NULL
        WHERE thread_id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    UPDATE issued_actions SET thread_id = NULL
        WHERE thread_id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    DELETE FROM threads WHERE id NOT IN (SELECT value FROM kv WHERE key = 'chat.main_thread')
        AND EXISTS (SELECT 1 FROM kv WHERE key = 'chat.main_thread');
    """,
    # 15: "approve" and "reject" everywhere: the `declined` (approvals, change
    # proposals) and `dismissed` (house facts) states become `rejected`. SQLite cannot alter
    # a CHECK constraint, so each table is rebuilt with the same columns and indexes.
    """
    CREATE TABLE approvals_v15 (
        id TEXT PRIMARY KEY,
        plan TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'approved', 'rejected', 'expired', 'used', 'void')),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT,
        subject TEXT NOT NULL DEFAULT '',
        run_when TEXT NOT NULL DEFAULT 'window',
        used_by_job INTEGER,
        detail TEXT NOT NULL DEFAULT ''
    );
    INSERT INTO approvals_v15 (id, plan, plan_hash, requested_by, state, created_at, expires_at, decided_at,
            decided_by, subject, run_when, used_by_job, detail)
        SELECT id, plan, plan_hash, requested_by, CASE state WHEN 'declined' THEN 'rejected' ELSE state END,
            created_at, expires_at, decided_at, decided_by, subject, run_when, used_by_job,
            CASE detail WHEN 'not now' THEN 'rejected' WHEN 'declined' THEN 'rejected' ELSE detail END
        FROM approvals;
    DROP TABLE approvals;
    ALTER TABLE approvals_v15 RENAME TO approvals;
    CREATE INDEX approvals_subject ON approvals(subject, state);
    CREATE TABLE change_proposals_v15 (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('change', 'facts')),
        subject TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT '',
        effect TEXT NOT NULL DEFAULT '',
        files TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '[]',
        diff TEXT NOT NULL DEFAULT '',
        base_sha TEXT,
        commit_sha TEXT,
        ref TEXT,
        state TEXT NOT NULL CHECK (state IN ('refused', 'open', 'applying', 'applied', 'reverting', 'reverted',
            'failed', 'stale', 'rejected', 'expired')),
        detail TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL,
        thread_id TEXT,
        run_id TEXT,
        fact_id INTEGER,
        applied_sha TEXT,
        reverted_sha TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        updated_at TEXT NOT NULL
    );
    INSERT INTO change_proposals_v15 (id, kind, subject, why, effect, files, summary, diff, base_sha, commit_sha,
            ref, state, detail, requested_by, thread_id, run_id, fact_id, applied_sha, reverted_sha, created_at,
            expires_at, decided_at, updated_at)
        SELECT id, kind, subject, why, effect, files, summary, diff, base_sha, commit_sha, ref,
            CASE state WHEN 'declined' THEN 'rejected' ELSE state END,
            CASE WHEN detail LIKE 'declined by %' THEN 'rejected by ' || substr(detail, 13) ELSE detail END,
            requested_by, thread_id, run_id, fact_id, applied_sha, reverted_sha, created_at, expires_at,
            decided_at, updated_at
        FROM change_proposals;
    DROP TABLE change_proposals;
    ALTER TABLE change_proposals_v15 RENAME TO change_proposals;
    CREATE INDEX change_proposals_state ON change_proposals(state, created_at);
    CREATE TABLE fact_proposals_v15 (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        thread_id TEXT,
        turn_id INTEGER,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected')),
        decided_at TEXT,
        change_id INTEGER
    );
    INSERT INTO fact_proposals_v15 (id, text, thread_id, turn_id, created_at, status, decided_at, change_id)
        SELECT id, text, thread_id, turn_id, created_at,
            CASE status WHEN 'dismissed' THEN 'rejected' ELSE status END, decided_at, change_id
        FROM fact_proposals;
    DROP TABLE fact_proposals;
    ALTER TABLE fact_proposals_v15 RENAME TO fact_proposals;
    CREATE INDEX fact_proposals_status ON fact_proposals(status, created_at);
    """,
    # 16: an OS update is installed, then activated by a reboot (Supervisor 2026.07.0
    # stopped rebooting after /os/update). `rebooting`: installed, the daemon reboots or waits for
    # its reboot. `reboot_pending`: installed, waits for a reboot the agent did not make. The
    # CHECK constraint cannot be altered, so the table is rebuilt with the same columns and indexes.
    """
    CREATE TABLE update_jobs_v16 (
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
    INSERT INTO update_jobs_v16 (id, run_id, seq, entity_id, kind, title, from_version, to_version, target, state,
            install_attempts, rollback_attempts, detail, regressions, idempotency_key, started_at, finished_at,
            updated_at)
        SELECT id, run_id, seq, entity_id, kind, title, from_version, to_version, target, state, install_attempts,
            rollback_attempts, detail, regressions, idempotency_key, started_at, finished_at, updated_at
        FROM update_jobs;
    DROP TABLE update_jobs;
    ALTER TABLE update_jobs_v16 RENAME TO update_jobs;
    CREATE INDEX update_jobs_run ON update_jobs(run_id, seq);
    CREATE INDEX update_jobs_state ON update_jobs(state);
    """,
    # 17: one record per item (`opsd.items`). Every surface (Home, History, the badge, pushes,
    # the old Activity, approvals, tasks and changes views) reads its state from `items`; `needs` is
    # derived and the CHECK keeps `need` present exactly while waiting (failed may carry an ack).
    # `chat` threads keep the `threads` table. Each earlier activity entry becomes a resolved item
    # with one event (nothing old is ever "Needs you": the derived items carry today's needs).
    """
    CREATE TABLE items (
        id TEXT PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('update', 'reminder', 'investigation', 'proposal', 'check', 'backup',
            'reload', 'window')),
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
        confirm TEXT,
        derived INTEGER NOT NULL DEFAULT 1,
        push TEXT NOT NULL DEFAULT '{}',
        needs INTEGER GENERATED ALWAYS AS (state = 'waiting' OR (state = 'failed' AND need IS NOT NULL)) VIRTUAL,
        CHECK ((state = 'waiting') = (need IS NOT NULL) OR (state = 'failed'))
    );
    CREATE INDEX items_needs ON items(needs, needs_since);
    CREATE INDEX items_at ON items(at);
    CREATE INDEX items_state ON items(state, resolved_at);
    CREATE TABLE item_events (
        id INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('you', 'daemon', 'agent')),
        text TEXT NOT NULL,
        revision INTEGER NOT NULL
    );
    CREATE INDEX item_events_item ON item_events(item_id, id);
    ALTER TABLE activity ADD COLUMN item TEXT;
    INSERT INTO items (id, key, kind, kicker, title, state, gist, agent, quiet, revision, as_of, at, resolved_at,
            created_at, updated_at, derived)
        SELECT 'a-' || id, 'activity:' || id,
            CASE kind WHEN 'backup' THEN 'backup' WHEN 'reload' THEN 'reload' WHEN 'updates' THEN 'window'
                WHEN 'approval' THEN 'update' WHEN 'change' THEN 'proposal' WHEN 'investigation' THEN 'investigation'
                WHEN 'reminder' THEN 'reminder' WHEN 'followup' THEN 'reminder' ELSE 'check' END,
            CASE kind WHEN 'notice' THEN 'Message from the agent' WHEN 'followup' THEN 'Follow-up' ELSE '' END,
            CASE WHEN kind IN ('notice', 'followup') THEN 'The agent sent you a message' ELSE title END,
            CASE WHEN lower(title) LIKE '%failed%' OR lower(title) LIKE '%rolled back%' THEN 'failed' ELSE 'done' END,
            CASE WHEN kind IN ('notice', 'followup') THEN '' ELSE substr(detail, 1, 300) END,
            CASE WHEN kind IN ('notice', 'followup')
                THEN json_object('text', title || ': ' || detail, 'sources', json_array(), 'not_read', json_array(),
                    'run_id', NULL)
                ELSE NULL END,
            CASE WHEN needs_admin = 0 THEN 1 ELSE 0 END, 1, at, at, at, at, at, 0
        FROM activity;
    INSERT INTO item_events (item_id, at, actor, text, revision)
        SELECT 'a-' || id, at, 'daemon', CASE WHEN kind IN ('notice', 'followup') THEN 'The agent sent you a message'
            ELSE title END, 1
        FROM activity;
    UPDATE activity SET item = 'a-' || id;
    """,
)

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

SCHEMA_VERSION = len(MIGRATIONS)
_FILE_MODE = 0o600


class StoreError(RuntimeError):
    """The store cannot be used; callers fail closed."""


class StoreCorrupt(StoreError):
    """The file is damaged; moving it aside is safe."""


class StoreUnavailable(StoreError):
    """The store could not be opened and must not be recreated (disk, I/O, lock, newer schema)."""


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


def _approval(row: sqlite3.Row) -> dict[str, Any]:
    keys = ("id", "plan_hash", "requested_by", "state", "created_at", "expires_at", "decided_at", "decided_by",
            "subject", "run_when", "used_by_job", "detail")  # fmt: skip
    return {k: row[k] for k in keys} | {"plan": _json_or(row["plan"], {})}


def _activity(row: sqlite3.Row) -> dict[str, Any]:
    found = dict(row)
    found["ref"] = _json_or(row["ref"], {})
    # Derived from the entry's item: an entry needs the admin only while its item is in Needs you.
    keys = row.keys()
    found["needs_admin"] = bool(row["item_needs"]) if "item_needs" in keys else bool(row["needs_admin"])
    found.pop("item_needs", None)
    return found


ITEM_JSON = ("need", "actions", "phase", "facts", "agent", "change", "source", "confirm", "push")
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
    found["derived"] = bool(found["derived"])
    found["needs"] = bool(found["needs"])
    return found


def _change(row: sqlite3.Row) -> dict[str, Any]:
    found = dict(row)
    found["files"] = _json_or(row["files"], [])
    found["summary"] = _json_or(row["summary"], [])
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


def _turn(row: sqlite3.Row) -> Turn:
    try:
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return Turn(
        id=int(row["id"]),
        thread_id=str(row["thread_id"]),
        role=str(row["role"]),
        text=str(row["text"]),
        at=str(row["at"]),
        status=str(row["status"]),
        meta=meta if isinstance(meta, dict) else {},
        run_id=row["run_id"],
        reply_to=row["reply_to"],
        client_id=row["client_id"],
    )


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


class Store:
    """One connection shared by the event loop and executor threads, serialised by a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

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

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            # A downgrade: the newer version migrated /data/ops.db. Never touched here; see the README.
            _LOGGER.critical(
                "store written by a newer add-on version", extra={"schema": version, "supported": SCHEMA_VERSION}
            )
            raise StoreError(f"store schema {version} is newer than this agent ({SCHEMA_VERSION}): the add-on was "
                             "downgraded; reinstall the newer version, or restore the add-on's /data from the "
                             "backup taken before the upgrade")  # fmt: skip
        for number in range(version, SCHEMA_VERSION):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[number].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {number + 1}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            _LOGGER.info("store migrated", extra={"version": number + 1})

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
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT started_at, profile, status FROM runs"
                    " WHERE started_at >= ? AND status NOT IN ('ok', 'running') ORDER BY started_at DESC LIMIT ?",
                    (_iso(since), limit),
                )
                .fetchall()
            )
        return [{"at": r[0], "profile": r[1], "status": r[2]} for r in rows]

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("store is closed")
        return self._conn

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

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
        with self._lock:
            if run_id is None:
                rows = self._db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = (
                    self._db()
                    .execute("SELECT * FROM audit WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit))
                    .fetchall()
                )
        return [dict(r) for r in rows]

    def prune_audit(self, *, before: datetime, keep_rows: int = 50_000) -> int:
        with self._tx() as conn:
            removed = conn.execute("DELETE FROM audit WHERE at < ?", (_iso(before),)).rowcount
            removed += conn.execute(
                "DELETE FROM audit WHERE id <= (SELECT id FROM audit ORDER BY id DESC LIMIT 1 OFFSET ?)", (keep_rows,)
            ).rowcount
        return removed

    # ------------------------------------------------- budgets and counters

    def count(self, day: str, name: str) -> int:
        with self._lock:
            row = self._db().execute("SELECT n FROM counters WHERE day = ? AND name = ?", (day, name)).fetchone()
        return int(row[0]) if row else 0

    def unbump(self, day: str, name: str) -> None:
        """Give one unit of a daily counter back (a request refused before it did anything)."""
        with self._tx() as conn:
            conn.execute("UPDATE counters SET n = n - 1 WHERE day = ? AND name = ? AND n > 0", (day, name))

    def bump(self, day: str, name: str, *, limit: int | None = None) -> bool:
        """Increment a daily counter; with `limit`, only if it stays within it (atomic)."""
        with self._tx() as conn:
            row = conn.execute("SELECT n FROM counters WHERE day = ? AND name = ?", (day, name)).fetchone()
            current = int(row[0]) if row else 0
            if limit is not None and current >= limit:
                return False
            conn.execute(
                "INSERT INTO counters (day, name, n) VALUES (?, ?, 1) ON CONFLICT(day, name) DO UPDATE SET n = n + 1",
                (day, name),
            )
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
        with self._lock:
            row = self._db().execute("SELECT * FROM tasks WHERE system_key = ?", (key,)).fetchone()
        return _task(row) if row else None

    def get_task(self, task_id: int) -> Task | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
                    claimed.append(_task(conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()))
        return claimed

    def reschedule(
        self, task_id: int, *, next_fire_at: datetime, now: datetime, payload: dict[str, Any] | None = None
    ) -> bool:
        """A running task goes back to pending at its next fire time."""
        with self._tx() as conn:
            if payload is None:
                return bool(
                    conn.execute(
                        "UPDATE tasks SET state = 'pending', next_fire_at = ?, updated_at = ?"
                        " WHERE id = ? AND state = 'running'",
                        (_iso(next_fire_at), _iso(now), task_id),
                    ).rowcount
                )
            return bool(
                conn.execute(
                    "UPDATE tasks SET state = 'pending', next_fire_at = ?, payload = ?, updated_at = ?"
                    " WHERE id = ? AND state = 'running'",
                    (_iso(next_fire_at), json.dumps(payload), _iso(now), task_id),
                ).rowcount
            )

    def finish_task(self, task_id: int, state: str, *, now: datetime) -> bool:
        if state not in ("done", "cancelled", "failed"):
            raise ValueError(f"not a final state: {state}")
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE tasks SET state = ?, next_fire_at = NULL, updated_at = ?"
                    " WHERE id = ? AND state = 'running'",
                    (state, _iso(now), task_id),
                ).rowcount
            )

    def recover_running(self, now: datetime) -> int:
        """After a crash or restart, tasks caught mid-run fire again."""
        with self._tx() as conn:
            return conn.execute(
                "UPDATE tasks SET state = 'pending', next_fire_at = ?, updated_at = ? WHERE state = 'running'",
                (_iso(now), _iso(now)),
            ).rowcount

    def next_due_at(self) -> datetime | None:
        with self._lock:
            row = (
                self._db()
                .execute("SELECT MIN(next_fire_at) FROM tasks WHERE state = 'pending' AND next_fire_at IS NOT NULL")
                .fetchone()
            )
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
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _task(row), True

    def open_tasks(self, *, limit: int = 50) -> list[Task]:
        """Pending, user-visible tasks (never the system's own)."""
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM tasks WHERE system_key IS NULL AND state IN ('pending', 'running')"
                    " ORDER BY next_fire_at LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        return [_task(r) for r in rows]

    def open_investigations(self) -> list[Task]:
        """Follow-ups that are chat investigations, pending or running."""
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM tasks WHERE system_key IS NULL AND kind = 'followup' AND state IN ('pending', 'running')"
                " AND json_extract(payload, '$.investigation') = 1 ORDER BY id").fetchall()  # fmt: skip
        return [_task(r) for r in rows]

    def tasks_created_by(self, created_by: str) -> list[Task]:
        with self._lock:
            rows = self._db().execute("SELECT * FROM tasks WHERE created_by = ? ORDER BY id", (created_by,)).fetchall()
        return [_task(r) for r in rows]

    def count_open_tasks(self, owner: str) -> int:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT COUNT(*) FROM tasks WHERE system_key IS NULL AND owner = ?"
                    " AND state IN ('pending', 'running')",
                    (owner,),
                )
                .fetchone()
            )
        return int(row[0])

    def cancel_task(self, task_id: int, *, owner: str, now: datetime) -> bool:
        """Only the owner's pending tasks; system tasks can never be cancelled."""
        return self._close_pending(task_id, "cancelled", owner=owner, now=now)

    def mark_done(self, task_id: int, *, now: datetime) -> bool:
        """The admin tapped Done on a (snoozed) reminder: it will not fire again."""
        return self._close_pending(task_id, "done", owner=None, now=now)

    def _close_pending(self, task_id: int, state: str, *, owner: str | None, now: datetime) -> bool:
        # `owner IS NULL` in the parameter means "any owner".
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE tasks SET state = ?, next_fire_at = NULL, updated_at = ?"
                    " WHERE id = ? AND system_key IS NULL AND state = 'pending' AND (? IS NULL OR owner = ?)",
                    (state, _iso(now), task_id, owner, owner),
                ).rowcount
            )

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
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE tasks SET state = 'pending', next_fire_at = ?, updated_at = ?"
                    " WHERE id = ? AND system_key IS NULL AND state = 'done'",
                    (_iso(next_fire_at), _iso(now), task_id),
                ).rowcount
            )

    def list_tasks(self, view: str, *, now: datetime, limit: int = 50) -> list[Task]:
        """The panel's reminders: `upcoming` (open) or `done` (closed in the last 14 days)."""
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
        with self._lock:
            rows = self._db().execute(sql, args).fetchall()
        return [_task(r) for r in rows]

    def admin_cancel_task(self, task_id: int, *, now: datetime) -> bool:
        """The admin may cancel any open user-visible task (theirs or the agent's), never a system task."""
        return self._close_pending(task_id, "cancelled", owner=None, now=now)

    # -------------------------------------------------------------- threads

    def create_thread(self, thread_id: str, *, principal: str, channel: str, now: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO threads (id, principal, channel, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (thread_id, principal, channel, _iso(now), _iso(now)),
            )

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

    @staticmethod
    def _insert_turn(
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
        cursor = conn.execute(
            "INSERT INTO turns (thread_id, role, text, at, run_id, status, meta, client_id, reply_to)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, role, text, _iso(now), run_id, status, json.dumps(meta or {}, default=str), client_id,
             reply_to),
        )  # fmt: skip
        conn.execute("UPDATE threads SET turns = turns + 1, updated_at = ? WHERE id = ?", (_iso(now), thread_id))
        return _turn(conn.execute("SELECT * FROM turns WHERE id = ?", (cursor.lastrowid,)).fetchone())

    def submit_turn(
        self, thread_id: str, *, text: str, now: datetime, client_id: str | None, meta: dict[str, Any] | None = None
    ) -> tuple[Turn, bool]:
        """Append an admin turn (queued); the same `client_id` in the thread returns the existing one."""
        with self._tx() as conn:
            if client_id is not None:
                row = conn.execute(
                    "SELECT * FROM turns WHERE thread_id = ? AND client_id = ?", (thread_id, client_id)
                ).fetchone()
                if row is not None:
                    return _turn(row), False
            turn = self._insert_turn(conn, thread_id, role="admin", text=text, now=now, run_id=None, status="queued",
                                     meta=meta, client_id=client_id, reply_to=None)  # fmt: skip
        return turn, True

    def turn_by_client(self, thread_id: str, client_id: str) -> Turn | None:
        with self._lock:
            row = (
                self._db()
                .execute("SELECT * FROM turns WHERE thread_id = ? AND client_id = ?", (thread_id, client_id))
                .fetchone()
            )
        return _turn(row) if row else None

    def get_turn(self, turn_id: int) -> Turn | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn(row) if row else None

    def set_turn_status(
        self, turn_id: int, status: str, *, expect: tuple[str, ...], meta: dict[str, Any] | None = None
    ) -> bool:
        """Compare-and-set an admin turn's status (exactly one caller wins a transition)."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None or str(row["status"]) not in expect:
                return False
            merged = {**_turn(row).meta, **(meta or {})}
            conn.execute(
                "UPDATE turns SET status = ?, meta = ? WHERE id = ?", (status, json.dumps(merged, default=str), turn_id)
            )
            return True

    def update_turn_meta(self, turn_id: int, meta: dict[str, Any]) -> Turn | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                return None
            merged = {**_turn(row).meta, **meta}
            conn.execute("UPDATE turns SET meta = ? WHERE id = ?", (json.dumps(merged, default=str), turn_id))
            return _turn(conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone())

    def update_card(self, turn_id: int, *, text: str, meta: dict[str, Any], now: datetime) -> Turn | None:
        """Rewrite a daemon-written `system` card in place (an investigation's progress)."""
        if not text.strip():
            raise ValueError("a card needs text")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id = ? AND role = 'system'", (turn_id,)).fetchone()
            if row is None:
                return None
            merged = {**_turn(row).meta, **meta}
            conn.execute("UPDATE turns SET text = ?, meta = ? WHERE id = ?",
                         (text, json.dumps(merged, default=str), turn_id))  # fmt: skip
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (_iso(now), str(row["thread_id"])))
            return _turn(conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone())

    def card_by_status(self, thread_id: str, status: str, task_id: int) -> Turn | None:
        """The `system` card of this status that belongs to a task (`meta.task`)."""
        with self._lock:
            row = self._db().execute(
                "SELECT * FROM turns WHERE thread_id = ? AND role = 'system' AND status = ?"
                " AND json_extract(meta, '$.task') = ? ORDER BY id DESC LIMIT 1", (thread_id, status, task_id)
            ).fetchone()  # fmt: skip
        return _turn(row) if row else None

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
            return self._insert_turn(conn, str(row["thread_id"]), role="agent", text=text, now=now, run_id=run_id,
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
            merged = {**_turn(row).meta, **(turn_meta or {})}
            conn.execute(
                "UPDATE turns SET status = ?, meta = ? WHERE id = ?",
                (outcome, json.dumps(merged, default=str), admin_turn),
            )
            updated = _turn(conn.execute("SELECT * FROM turns WHERE id = ?", (admin_turn,)).fetchone())
            if card is None:
                return updated, None
            thread = str(row["thread_id"])
            existing = conn.execute(
                "SELECT id FROM turns WHERE reply_to = ? AND role = 'system' AND status != 'summary'", (admin_turn,)
            ).fetchone()
            if existing is None:
                written = self._insert_turn(conn, thread, role="system", text=text, now=now, run_id=None, status=card,
                                            meta=meta, client_id=None, reply_to=admin_turn)  # fmt: skip
                return updated, written
            conn.execute(
                "UPDATE turns SET text = ?, status = ?, meta = ?, at = ? WHERE id = ?",
                (text, card, json.dumps(meta or {}, default=str), _iso(now), existing["id"]),
            )
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (_iso(now), thread))
            card_row = conn.execute("SELECT * FROM turns WHERE id = ?", (existing["id"],)).fetchone()
            return updated, _turn(card_row)

    def card_for(self, admin_turn: int) -> Turn | None:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM turns WHERE reply_to = ? AND role = 'system' AND status != 'summary'", (admin_turn,)
                )
                .fetchone()
            )
        return _turn(row) if row else None

    def turns_page(self, thread_id: str, *, before: int | None = None, limit: int = 30) -> list[Turn]:
        """Up to `limit` turns before turn id `before` (all if None), oldest first."""
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM turns WHERE thread_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                    (thread_id, before if before is not None else sys.maxsize, limit),
                )
                .fetchall()
            )
        return [_turn(r) for r in reversed(rows)]

    def context_turns(self, thread_id: str, *, after: int, before: int, limit: int) -> list[Turn]:
        """The conversation the model sees: admin and agent turns between two ids, oldest first."""
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM turns WHERE thread_id = ? AND id > ? AND id < ? AND role IN ('admin', 'agent')"
                    " ORDER BY id DESC LIMIT ?",
                    (thread_id, after, before, limit),
                )
                .fetchall()
            )
        return [_turn(r) for r in reversed(rows)]

    def count_context(self, thread_id: str, *, after: int, before: int) -> int:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT COUNT(*) FROM turns WHERE thread_id = ? AND id > ? AND id < ?"
                    " AND role IN ('admin', 'agent')",
                    (thread_id, after, before),
                )
                .fetchone()
            )
        return int(row[0])

    def latest_summary(self, thread_id: str) -> Turn | None:
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM turns WHERE thread_id = ? AND role = 'system' AND status = 'summary'"
                    " ORDER BY id DESC LIMIT 1",
                    (thread_id,),
                )
                .fetchone()
            )
        return _turn(row) if row else None

    def in_flight(self, thread_id: str) -> Turn | None:
        """The oldest queued or running admin turn of the thread."""
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM turns WHERE thread_id = ? AND role = 'admin' AND status IN ('queued', 'running')"
                    " ORDER BY id LIMIT 1",
                    (thread_id,),
                )
                .fetchone()
            )
        return _turn(row) if row else None

    def stuck_turns(self) -> list[Turn]:
        """Admin turns left queued or running (by a restart)."""
        with self._lock:
            rows = (
                self._db()
                .execute("SELECT * FROM turns WHERE role = 'admin' AND status IN ('queued', 'running') ORDER BY id")
                .fetchall()
            )
        return [_turn(r) for r in rows]

    _THREAD_SQL = (
        "SELECT t.*, (SELECT MAX(id) FROM turns WHERE thread_id = t.id AND role != 'admin'"
        " AND status != 'summary') AS last_reply, (SELECT COUNT(*) FROM turns WHERE thread_id = t.id AND"
        " role = 'admin' AND status IN ('queued', 'running')) AS in_flight FROM threads t"
    )

    @staticmethod
    def _thread(row: sqlite3.Row) -> dict[str, Any]:
        last_reply = row["last_reply"]
        return {
            "id": str(row["id"]),
            "channel": str(row["channel"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "turns": int(row["turns"]),
            "unread": last_reply is not None and int(last_reply) > int(row["last_read_turn_id"]),
            "in_flight": int(row["in_flight"]) > 0,
        }

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute(self._THREAD_SQL + " WHERE t.id = ?", (thread_id,)).fetchone()
        return self._thread(row) if row else None

    def add_fact(self, text: str, *, thread_id: str | None, turn_id: int | None, now: datetime) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO fact_proposals (text, thread_id, turn_id, created_at) VALUES (?, ?, ?, ?)",
                (text[:MAX_FACT_CHARS], thread_id, turn_id, _iso(now)),
            )
            return int(cursor.lastrowid or 0)

    def list_facts(self, *, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        """Pending proposals, plus the ones decided in the last 30 days."""
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM fact_proposals WHERE status = 'pending' OR decided_at >= ?"
                    " ORDER BY status != 'pending', created_at DESC LIMIT ?",
                    (_iso(now - timedelta(days=30)), limit),
                )
                .fetchall()
            )
        return [dict(r) for r in rows]

    def decide_fact(self, fact_id: int, status: str, *, now: datetime) -> bool:
        if status not in ("applied", "rejected"):
            raise ValueError(f"not a decision: {status}")
        with self._tx() as conn:
            return bool(
                conn.execute(
                    "UPDATE fact_proposals SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                    (status, _iso(now), fact_id),
                ).rowcount
            )

    def mark_read(self, thread_id: str, *, upto: int | None = None) -> bool:
        with self._tx() as conn:
            row = conn.execute("SELECT MAX(id) FROM turns WHERE thread_id = ?", (thread_id,)).fetchone()
            last = int(row[0] or 0)
            target = last if upto is None else min(upto, last)
            return bool(
                conn.execute(
                    "UPDATE threads SET last_read_turn_id = ? WHERE id = ? AND last_read_turn_id < ?",
                    (target, thread_id, target),
                ).rowcount
            )

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

    # --------------------------------------------------------- run ledger

    # A running run counts its reservation; a finished one its real figures.
    _USAGE_SQL = (
        "SELECT COUNT(*), SUM(COALESCE(turns, reserved_turns)), SUM(COALESCE(tokens, reserved_tokens)),"
        " SUM(tokens_cache_read), SUM(COALESCE(cost_usd, reserved_usd)), SUM(COALESCE(seconds, reserved_seconds)),"
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
            "usd": round(float(row[4] or 0.0), 4),
            "seconds": round(float(row[5] or 0.0), 1),
            "share_5h": float(row[6] or 0.0),
            "share_7d": float(row[7] or 0.0),
            # Only shares Claude Code actually reported (not reservations or estimates).
            "measured_share_5h": float(row[8] or 0.0),
            # Runs whose share is a run-time estimate (below the reported resolution).
            "estimated_runs": int(row[9] or 0),
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
        scopes = {
            "window": self._usage(conn, "started_at >= ?", (_iso(at - WINDOW),)),
            "day": self._usage(conn, "day = ?", (period.day,)),
            # Rolling seven days, like the subscription's weekly window.
            "week": self._usage(conn, "started_at >= ?", (_iso(at - timedelta(days=7)),)),
        }
        return scopes

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
        fallback), and the profile's daily run cap.
        """
        with self._tx() as conn:
            scopes = self._scopes(conn, period, at)
            account = self._account(conn, at)
            for window, value in account.items():
                if value is not None and value >= ACCOUNT_BUSY:
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
                " reserved_tokens, reserved_usd, reserved_seconds, reserved_share) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?)",
                (run_id, profile, budget_class, period.day, period.week, period.month, _iso(at), max_turns, 0,
                 max(0.0, budget_usd), seconds, max(share, share_7d)),
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
        cost_usd: float,
        duration_s: float,
        model: str,
        charge_reservation: bool = False,
        share_5h: float | None = None,
        share_7d: float | None = None,
        acct_5h: float | None = None,
        acct_7d: float | None = None,
        share_estimated: bool = False,
    ) -> None:
        """Replace the reservation by the real figures (or keep it, for aborted runs that report nothing)."""
        with self._tx() as conn:
            if charge_reservation:
                conn.execute(
                    "UPDATE runs SET status = ?, ended_at = ?, turns = MAX(reserved_turns / 4, ?),"
                    " tokens = MAX(reserved_tokens / 4, ?), cost_usd = ?, duration_s = ?, model = ?,"
                    " seconds = MAX(reserved_seconds / 4, ?), share_5h = COALESCE(?, reserved_share),"
                    " share_7d = COALESCE(?, reserved_share), acct_5h = ?, acct_7d = ?, share_estimated = ?"
                    " WHERE run_id = ?",
                    (status, _iso(at), turns, tokens_in + tokens_out, max(0.0, cost_usd), duration_s, model[:60],
                     duration_s, share_5h, share_7d, acct_5h, acct_7d, int(share_estimated), run_id),
                )  # fmt: skip
                return
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = ?, turns = ?, tokens = ?, tokens_in = ?, tokens_out = ?,"
                " tokens_cache_read = ?, cost_usd = ?, duration_s = ?, model = ?, seconds = ?,"
                " share_5h = COALESCE(?, reserved_share), share_7d = COALESCE(?, reserved_share), acct_5h = ?,"
                " acct_7d = ?, share_estimated = ? WHERE run_id = ?",
                (status, _iso(at), turns, tokens_in + tokens_out, tokens_in, tokens_out, tokens_cache_read,
                 max(0.0, cost_usd), duration_s, model[:60], duration_s, share_5h, share_7d, acct_5h, acct_7d,
                 int(share_estimated), run_id),
            )  # fmt: skip

    def close_stale_runs(self, *, at: datetime) -> int:
        """Runs left `running` by a crash are charged their reservation."""
        with self._tx() as conn:
            return conn.execute(
                "UPDATE runs SET status = 'interrupted', ended_at = ?, turns = reserved_turns,"
                " tokens = reserved_tokens, cost_usd = reserved_usd, seconds = reserved_seconds,"
                " share_5h = reserved_share, share_7d = reserved_share WHERE status = 'running'",
                (_iso(at),),
            ).rowcount

    def usage(self, period: Period, *, at: datetime) -> dict[str, Any]:
        """Usage per window/day/week/month and per profile today, plus the last run (for the sensors)."""
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
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT started_at, COALESCE(share_5h, reserved_share), COALESCE(share_7d, reserved_share)"
                    " FROM runs WHERE started_at >= ? ORDER BY started_at",
                    (_iso(since),),
                )
                .fetchall()
            )
        return [(datetime.fromisoformat(r[0]), float(r[1] or 0.0), float(r[2] or 0.0)) for r in rows]

    def window_frees_at(self, *, at: datetime, span: timedelta = WINDOW) -> datetime | None:
        """When the oldest run in the rolling window ages out (a cap's earliest possible relief)."""
        with self._lock:
            row = (
                self._db()
                .execute("SELECT MIN(started_at) FROM runs WHERE started_at >= ?", (_iso(at - span),))
                .fetchone()
            )
        return datetime.fromisoformat(row[0]) + span if row and row[0] else None

    # ------------------------------------------------------------------ kv

    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def kv_set(self, key: str, value: str, *, now: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, _iso(now)),
            )

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
            return int(cursor.lastrowid or 0)

    def move_update_job(self, job_id: int, *, run_id: int, now: datetime) -> bool:
        """Move a job that waits for its reboot into the run of its own that performs it (`UpdateManager`)."""
        with self._tx() as conn:
            cursor = conn.execute("UPDATE update_jobs SET run_id = ?, updated_at = ? WHERE id = ? AND state = ?",
                                  (run_id, _iso(now), job_id, "reboot_pending"))  # fmt: skip
        return cursor.rowcount == 1

    def update_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM update_runs WHERE id = ?", (run_id,)).fetchone()
        return _update_run(row) if row else None

    def active_update_run(self) -> dict[str, Any] | None:
        with self._lock:
            row = (
                self._db()
                .execute(f"SELECT * FROM update_runs WHERE state IN {_ACTIVE_SQL} ORDER BY id LIMIT 1")  # noqa: S608
                .fetchone()
            )
        return _update_run(row) if row else None

    def window_run(self, window_day: str, trigger: str) -> dict[str, Any] | None:
        """The run already started for this window (tasks fire at least once)."""
        with self._lock:
            row = (
                self._db()
                .execute(
                    "SELECT * FROM update_runs WHERE window_day = ? AND trigger = ? ORDER BY id DESC LIMIT 1",
                    (window_day, trigger),
                )
                .fetchone()
            )
        return _update_run(row) if row else None

    def recent_update_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db().execute("SELECT * FROM update_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_update_run(r) for r in rows]

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
            return bool(conn.execute(f"UPDATE update_runs SET {', '.join(sets)} WHERE {where}", values).rowcount)  # noqa: S608

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
        return self.update_jobs(run_id)

    def update_jobs(self, run_id: int) -> list[UpdateJob]:
        with self._lock:
            rows = self._db().execute("SELECT * FROM update_jobs WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [_update_job(r) for r in rows]

    def update_job(self, job_id: int) -> UpdateJob | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM update_jobs WHERE id = ?", (job_id,)).fetchone()
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
            return bool(
                conn.execute(
                    f"UPDATE update_jobs SET {', '.join(sets)} WHERE {where}",  # noqa: S608
                    (*values, job_id, *expect),
                ).rowcount
            )

    def update_jobs_where(self, *, kind: str, states: tuple[str, ...], since: datetime) -> list[UpdateJob]:
        """Jobs of one kind in these states, updated since `since`, oldest first."""
        marks = ",".join("?" * len(states))
        with self._lock:
            rows = (
                self._db()
                .execute(
                    f"SELECT * FROM update_jobs WHERE kind = ? AND state IN ({marks}) AND updated_at >= ?"  # noqa: S608
                    " ORDER BY id",
                    (kind, *states, _iso(since)),
                )
                .fetchall()
            )
        return [_update_job(r) for r in rows]

    # ----------------------------------------------------------- approvals

    def create_approval(self, *, approval_id: str, plan: dict[str, Any], plan_hash: str, subject: str,
                        requested_by: str, run_when: str, now: datetime, expires_at: datetime) -> None:  # fmt: skip
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO approvals (id, plan, plan_hash, requested_by, state, created_at, expires_at, subject,"
                " run_when) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (approval_id, json.dumps(plan, sort_keys=True), plan_hash, requested_by, _iso(now), _iso(expires_at),
                 subject, run_when),
            )  # fmt: skip

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _approval(row) if row else None

    def approvals(self, *, states: tuple[str, ...] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if states:
                rows = self._db().execute(
                    f"SELECT * FROM approvals WHERE state IN ({','.join('?' * len(states))})"  # noqa: S608
                    " ORDER BY created_at DESC LIMIT ?", (*states, limit)).fetchall()  # fmt: skip
            else:
                rows = self._db().execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                                          (limit,)).fetchall()  # fmt: skip
        return [_approval(r) for r in rows]

    def decide_approval(self, approval_id: str, *, expect: tuple[str, ...], to: str, now: datetime,
                        by: str | None = None, detail: str = "", job: int | None = None) -> bool:  # fmt: skip
        """Compare-and-set an approval's state (single use: `approved` -> `used` happens once)."""
        sets = ["state = ?", "decided_at = COALESCE(decided_at, ?)", "detail = ?"]
        values: list[Any] = [to, _iso(now), detail[:300]]
        if by is not None:
            sets.append("decided_by = ?")
            values.append(by)
        if job is not None:
            sets.append("used_by_job = ?")
            values.append(job)
        where = f"id = ? AND state IN ({','.join('?' * len(expect))})"
        with self._tx() as conn:
            return bool(conn.execute(f"UPDATE approvals SET {', '.join(sets)} WHERE {where}",  # noqa: S608
                                     (*values, approval_id, *expect)).rowcount)  # fmt: skip

    # ------------------------------------------------------- activity log

    def add_activity(self, *, kind: str, title: str, detail: str, needs_admin: bool, ref: dict[str, Any],
                     now: datetime, item: str | None = None) -> dict[str, Any]:  # fmt: skip
        """`needs_admin` records what the writer thought at the time; reads derive it from `item`."""
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO activity (at, kind, title, detail, needs_admin, ref, item) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_iso(now), kind[:20], title[:160], detail[:2000], int(needs_admin), json.dumps(ref, default=str),
                 item),
            )  # fmt: skip
            row = conn.execute("SELECT * FROM activity WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _activity(row)

    def link_activity(self, activity_id: int, item: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE activity SET item = ? WHERE id = ?", (item, activity_id))

    def set_activity_turn(self, activity_id: int, turn_id: int) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE activity SET turn_id = ? WHERE id = ?", (turn_id, activity_id))

    def activity(self, *, before: int | None = None, since: datetime | None = None,
                 limit: int = 50) -> list[dict[str, Any]]:  # fmt: skip
        """Newest first."""
        where: list[str] = ["1 = 1"]
        args: list[Any] = []
        if before is not None:
            where.append("activity.id < ?")
            args.append(before)
        if since is not None:
            where.append("activity.at >= ?")
            args.append(_iso(since))
        with self._lock:
            sql = ("SELECT activity.*, COALESCE(items.needs, 0) AS item_needs FROM activity"  # noqa: S608
                   f" LEFT JOIN items ON items.id = activity.item WHERE {' AND '.join(where)}"
                   " ORDER BY activity.id DESC LIMIT ?")  # fmt: skip
            rows = self._db().execute(sql, (*args, limit)).fetchall()
        return [_activity(r) for r in rows]

    # ------------------------------------------------------- quiet hours

    def hold_push(self, message: str, *, title: str, now: datetime) -> int:
        with self._tx() as conn:
            cursor = conn.execute("INSERT INTO held_pushes (held_at, message, title) VALUES (?, ?, ?)",
                                  (_iso(now), message, title[:120]))  # fmt: skip
            return int(cursor.lastrowid or 0)

    def held_pushes(self, *, states: tuple[str, ...] = ("held",), since: datetime | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:  # fmt: skip
        where = f"state IN ({','.join('?' * len(states))})"
        args: list[Any] = list(states)
        if since is not None:
            where += " AND held_at >= ?"
            args.append(_iso(since))
        with self._lock:
            rows = self._db().execute(f"SELECT * FROM held_pushes WHERE {where} ORDER BY id LIMIT ?",  # noqa: S608
                                      (*args, limit)).fetchall()  # fmt: skip
        return [dict(r) for r in rows]

    def settle_held(self, ids: list[int], *, to: str, outcome: str, now: datetime) -> None:
        if to not in ("delivered", "undelivered"):
            raise ValueError(f"not a final held-push state: {to}")
        with self._tx() as conn:
            conn.executemany("UPDATE held_pushes SET state = ?, outcome = ?, settled_at = ? WHERE id = ?"
                             " AND state = 'held'", [(to, outcome[:40], _iso(now), i) for i in ids])  # fmt: skip

    # --------------------------------------------------- change proposals

    _CHANGE_FIELDS = frozenset({"files", "diff", "base_sha", "commit_sha", "ref", "detail", "applied_sha",
                                "reverted_sha", "decided_at", "expires_at"})  # fmt: skip

    def create_change(self, *, kind: str, subject: str, why: str, effect: str, state: str, requested_by: str,
                      now: datetime, expires_at: datetime, detail: str = "", files: list[dict[str, Any]] | None = None,
                      diff: str = "", base_sha: str | None = None, commit_sha: str | None = None,
                      thread_id: str | None = None, run_id: str | None = None, fact_id: int | None = None,
                      summary: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO change_proposals (kind, subject, why, effect, files, summary, diff, base_sha, commit_sha,"
                " state, detail, requested_by, thread_id, run_id, fact_id, created_at, expires_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, subject[:120], why[:600], effect[:300], json.dumps(files or []), json.dumps(summary or []),
                 diff, base_sha, commit_sha,
                 state, detail[:300], requested_by[:100], thread_id, run_id, fact_id, _iso(now), _iso(expires_at),
                 _iso(now)),
            )  # fmt: skip
            row = conn.execute("SELECT * FROM change_proposals WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _change(row)

    def change(self, change_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM change_proposals WHERE id = ?", (change_id,)).fetchone()
        return _change(row) if row else None

    def changes(self, *, states: tuple[str, ...] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if states:
                rows = self._db().execute(
                    f"SELECT * FROM change_proposals WHERE state IN ({','.join('?' * len(states))})"  # noqa: S608
                    " ORDER BY id DESC LIMIT ?", (*states, limit)).fetchall()  # fmt: skip
            else:
                rows = self._db().execute("SELECT * FROM change_proposals ORDER BY id DESC LIMIT ?",
                                          (limit,)).fetchall()  # fmt: skip
        return [_change(r) for r in rows]

    def move_change(self, change_id: int, *, expect: tuple[str, ...], to: str, now: datetime,
                    **fields: Any) -> bool:  # fmt: skip
        """Compare-and-set a proposal's state (and set some fields with it)."""
        unknown = set(fields) - self._CHANGE_FIELDS
        if unknown:
            raise ValueError(f"unknown change fields: {sorted(unknown)}")
        sets = ["state = ?", "updated_at = ?"]
        values: list[Any] = [to, _iso(now)]
        for name, value in fields.items():
            sets.append(f"{name} = ?")
            values.append(json.dumps(value) if name == "files" else value)
        where = f"id = ? AND state IN ({','.join('?' * len(expect))})"
        with self._tx() as conn:
            return bool(conn.execute(f"UPDATE change_proposals SET {', '.join(sets)} WHERE {where}",  # noqa: S608
                                     (*values, change_id, *expect)).rowcount)  # fmt: skip

    def link_fact(self, fact_id: int, change_id: int) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE fact_proposals SET change_id = ? WHERE id = ?", (change_id, fact_id))

    # ------------------------------------------------- approval proposals

    def create_proposal(self, *, entity_id: str, from_version: str, to_version: str, plan_run: int, title: str,
                        thread_id: str | None, run_id: str, requested_by: str, now: datetime,
                        expires_at: datetime) -> dict[str, Any]:  # fmt: skip
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO approval_proposals (entity_id, from_version, to_version, plan_run, title, thread_id,"
                " run_id, requested_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entity_id, from_version, to_version, plan_run, title[:120], thread_id, run_id, requested_by[:100],
                 _iso(now), _iso(expires_at)),
            )  # fmt: skip
            row = conn.execute("SELECT * FROM approval_proposals WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def proposals(self, *, states: tuple[str, ...] = ("open",), run_id: str | None = None,
                  limit: int = 50) -> list[dict[str, Any]]:  # fmt: skip
        where = f"state IN ({','.join('?' * len(states))})"
        args: list[Any] = list(states)
        if run_id is not None:
            where += " AND run_id = ?"
            args.append(run_id)
        with self._lock:
            rows = self._db().execute(
                f"SELECT * FROM approval_proposals WHERE {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                (*args, limit)).fetchall()  # fmt: skip
        return [dict(r) for r in rows]

    def settle_proposal(self, proposal_id: int, *, to: str, now: datetime, detail: str = "") -> bool:
        """Compare-and-set an open proposal to `done`, `void` or `expired`."""
        if to not in ("done", "void", "expired"):
            raise ValueError(f"not a final proposal state: {to}")
        with self._tx() as conn:
            return bool(conn.execute(
                "UPDATE approval_proposals SET state = ?, decided_at = ?, detail = ? WHERE id = ? AND state = 'open'",
                (to, _iso(now), detail[:200], proposal_id)).rowcount)  # fmt: skip

    def set_job_target(self, job_id: int, target: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE update_jobs SET target = ? WHERE id = ?", (json.dumps(target, default=str), job_id))

    def rollback_history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM update_jobs WHERE state IN ('rolled_back', 'rollback_failed', 'rolling_back')"
                    " ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        return [_update_job(r).to_json() for r in rows]

    def kv_prefix(self, prefix: str, *, limit: int = 200) -> dict[str, str]:
        """Non-empty values whose key starts with `prefix` (a literal prefix, not a pattern)."""
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT key, value FROM kv WHERE substr(key, 1, ?) = ? AND value != '' ORDER BY key LIMIT ?",
                    (len(prefix), prefix, limit),
                )
                .fetchall()
            )
        return {str(r["key"]): str(r["value"]) for r in rows}

    # ------------------------------------------------------------ retention

    def prune(self, *, now: datetime, keep: timedelta = timedelta(days=90)) -> dict[str, int]:
        """Bound the store: expired/used actions, old runs and counters, decided proposals, old activity.

        Runs and counters are kept at least 8 days, so the rolling 7-day
        window and today's counters are never pruned away.
        """
        cutoff = _iso(now - max(keep, MIN_KEEP))
        with self._tx() as conn:
            actions = conn.execute(
                "DELETE FROM issued_actions WHERE expires_at < ? OR (used_at IS NOT NULL AND used_at < ?)",
                (_iso(now), _iso(now - timedelta(days=7))),
            ).rowcount
            conn.execute(
                "DELETE FROM fact_proposals WHERE status != 'pending' AND decided_at < ?",
                (_iso(now - timedelta(days=90)),),
            )
            conn.execute("DELETE FROM activity WHERE at < ?", (_iso(now - timedelta(days=90)),))
            # Resolved items (never one in Needs you) and their events go with the activity they replaced.
            conn.execute("DELETE FROM items WHERE needs = 0 AND resolved_at IS NOT NULL AND resolved_at < ?",
                         (_iso(now - timedelta(days=90)),))  # fmt: skip
            conn.execute("DELETE FROM item_events WHERE item_id NOT IN (SELECT id FROM items) AND at < ?",
                         (_iso(now - timedelta(days=90)),))  # fmt: skip
            conn.execute("DELETE FROM held_pushes WHERE state != 'held' AND settled_at < ?",
                         (_iso(now - timedelta(days=7)),))  # fmt: skip
            conn.execute(
                "DELETE FROM approval_proposals WHERE state != 'open' AND decided_at < ?",
                (_iso(now - timedelta(days=90)),),
            )
            conn.execute(
                "DELETE FROM kv WHERE key LIKE 'investigation.cancel:%' AND updated_at < ?",
                (_iso(now - timedelta(days=7)),),
            )
            runs = conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,)).rowcount
            # Panel confirmations (`confirm:<id>`) expire in minutes; keep a week for the record.
            conn.execute(
                "DELETE FROM kv WHERE key LIKE 'confirm:%' AND updated_at < ?", (_iso(now - timedelta(days=7)),)
            )
            counters = conn.execute("DELETE FROM counters WHERE day < ?", (cutoff[:10],)).rowcount
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
                     "needs_since", "resolved_at", "created_at", "updated_at", "source", "confirm", "derived",
                     "push")  # fmt: skip

    def item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _item(row) if row else None

    def item_by_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM items WHERE key = ?", (key,)).fetchone()
        return _item(row) if row else None

    def items_all(self, *, derived: bool | None = None, open_only: bool = False) -> list[dict[str, Any]]:
        """Items, optionally only derived ones or only unresolved ones (the sync's working set)."""
        where = ["1 = 1"]
        args: list[Any] = []
        if derived is not None:
            where.append("derived = ?")
            args.append(int(derived))
        if open_only:
            where.append("(resolved_at IS NULL OR needs = 1)")
        with self._lock:
            rows = self._db().execute(f"SELECT * FROM items WHERE {' AND '.join(where)}", args).fetchall()  # noqa: S608
        return [_item(r) for r in rows]

    def items_since(self, since: datetime) -> list[dict[str, Any]]:
        """Unresolved items, items in Needs you, and items resolved since `since` (for Home)."""
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM items WHERE resolved_at IS NULL OR needs = 1 OR resolved_at >= ?",
                (_iso(since),)).fetchall()  # fmt: skip
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
            elif name in ("quiet", "derived"):
                value = int(bool(value))
            values.append(value)
        marks = ", ".join("?" * len(self._ITEM_COLUMNS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._ITEM_COLUMNS if c != "id")
        with self._tx() as conn:
            conn.execute(f"INSERT INTO items ({', '.join(self._ITEM_COLUMNS)}) VALUES ({marks})"  # noqa: S608
                         f" ON CONFLICT(id) DO UPDATE SET {updates}", values)  # fmt: skip

    def set_item_fields(self, item_id: str, **fields: Any) -> None:
        """Columns that never change an item's revision (`as_of`, `confirm`, `push`)."""
        allowed = {"as_of", "confirm", "push"}
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
        with self._lock:
            rows = self._db().execute(
                "SELECT at, actor, text, revision FROM item_events WHERE item_id = ? ORDER BY id DESC LIMIT ?",
                (item_id, limit)).fetchall()  # fmt: skip
        return [dict(r) for r in reversed(rows)]

    def needs_count(self) -> int:
        """The Needs you badge: one query, never counted anywhere else."""
        with self._lock:
            return int(self._db().execute("SELECT count(*) FROM items WHERE needs = 1").fetchone()[0])

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
        with self._lock:
            rows = self._db().execute(
                f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT ?",  # noqa: S608
                (*args, limit)).fetchall()  # fmt: skip
        return [_item(r) for r in rows]

    def recent_update_jobs(self, *, since: datetime) -> list[UpdateJob]:
        """Jobs updated since `since` or not final yet, oldest first."""
        final = ",".join(f"'{s}'" for s in sorted(JOB_FINAL))
        with self._lock:
            rows = self._db().execute(
                f"SELECT * FROM update_jobs WHERE updated_at >= ? OR state NOT IN ({final}) ORDER BY id",  # noqa: S608
                (_iso(since),)).fetchall()  # fmt: skip
        return [_update_job(r) for r in rows]

    # ------------------------------------------------------ issued actions

    def issue_action(
        self, action_id: str, *, kind: str, tag: str, thread_id: str | None, now: datetime, expires_at: datetime
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO issued_actions (action_id, kind, tag, thread_id, issued_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (action_id, kind, tag, thread_id, _iso(now), _iso(expires_at)),
            )

    def issued_action(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db().execute("SELECT * FROM issued_actions WHERE action_id = ?", (action_id,)).fetchone()
        return dict(row) if row else None

    def consume_action(self, action_id: str, *, now: datetime) -> dict[str, Any] | None:
        """Single use: the row is marked used in the same transaction that reads it."""
        with self._tx() as conn:
            updated = conn.execute(
                "UPDATE issued_actions SET used_at = ? WHERE action_id = ? AND used_at IS NULL AND expires_at > ?",
                (_iso(now), action_id, _iso(now)),
            ).rowcount
            if not updated:
                return None
            row = conn.execute("SELECT * FROM issued_actions WHERE action_id = ?", (action_id,)).fetchone()
        return dict(row)


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
