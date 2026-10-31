"""The store's schema at version 18 (add-on 0.18.x), frozen for the upgrade test.

A verbatim copy of `opsd.store.MIGRATIONS` as 0.18.0 shipped them. `tests/test_store_upgrade.py` applies
them in order, as every real install did, to build a version-18 store and then upgrades it with the
current code. Never edit these scripts: they are what existing stores were made by.
"""

MIGRATIONS_V18: tuple[str, ...] = (
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
    # 18: maintenance items (`opsd.maintenance`). SQLite cannot change a CHECK in place, so the table is
    # rebuilt with the same columns and the new kind; the generated `needs` column is recomputed.
    """
    CREATE TABLE items_v18 (
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
        confirm TEXT,
        derived INTEGER NOT NULL DEFAULT 1,
        push TEXT NOT NULL DEFAULT '{}',
        needs INTEGER GENERATED ALWAYS AS (state = 'waiting' OR (state = 'failed' AND need IS NOT NULL)) VIRTUAL,
        CHECK ((state = 'waiting') = (need IS NOT NULL) OR (state = 'failed'))
    );
    INSERT INTO items_v18 (id, key, kind, kicker, ref, title, state, gist, need, about, actions, phase, follows,
            facts, agent, change, footnote, quiet, revision, as_of, at, needs_since, resolved_at, created_at,
            updated_at, source, confirm, derived, push)
        SELECT id, key, kind, kicker, ref, title, state, gist, need, about, actions, phase, follows, facts, agent,
            change, footnote, quiet, revision, as_of, at, needs_since, resolved_at, created_at, updated_at, source,
            confirm, derived, push
        FROM items;
    DROP TABLE items;
    ALTER TABLE items_v18 RENAME TO items;
    CREATE INDEX items_needs ON items(needs, needs_since);
    CREATE INDEX items_at ON items(at);
    CREATE INDEX items_state ON items(state, resolved_at);
    """,
)
