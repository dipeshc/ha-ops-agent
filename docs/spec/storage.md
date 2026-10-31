# Storage and migrations

Implementation: `opsd/store.py` (SQLite), `opsd/jsonstore.py`,
`opsd/state.py`, `opsd/lock.py`, `opsd/logs.py`, `bin/ops-backup-pre`.
Tests: `tests/test_store.py`, `tests/test_store_upgrade.py` (the upgrade from
schema 18 on a fixture database built with the frozen 0.18 migrations in
`tests/fixtures/schema_v18.py`), `tests/test_infra.py`, `tests/test_usage.py`
(pruning), `tests/test_chat_retention.py`.

## The store

- S1. One SQLite database at `/data/ops.db`, created with mode 0600 before
  SQLite opens it; `-wal` and `-shm` are set to 0600 after connecting.
  Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
  `trusted_schema=OFF`, and `quick_check` on every open. One connection,
  serialised by a lock; writes run in `BEGIN IMMEDIATE` transactions, and a
  write made inside another one joins it (a savepoint), so a caller can group
  several writes into one transaction (`Store.atomic`). Each write records
  what it touched; right before the transaction commits, the observer (the
  item book) writes the items those records show inside it, in a savepoint
  of its own that a failure rolls back alone
  ([items-and-pushes.md](items-and-pushes.md) I5). Tests
  `test_migrated_private_and_reopenable`,
  `test_store_file_created_private_even_with_open_umask`.
- S2. **Schema versions.** The schema version is tracked in `PRAGMA
  user_version`; this version's is 19. An empty file gets the whole schema
  in one transaction. A store at 18 (add-on 0.18) is upgraded by one
  migration in one transaction (S11); a failure anywhere in it rolls the
  file back to 18 untouched. A store at any other version below 19, or a
  file with tables but no version, MUST be refused (`StoreError`, "older than
  this agent upgrades": install 0.18 first) and a store newer than 19 MUST be
  refused ("newer": the add-on was downgraded); neither is ever quarantined
  or changed. Tests `test_newer_schema_is_refused`,
  `test_newer_schema_is_never_quarantined`, `test_failed_migration_rolls_back`,
  `test_a_store_older_than_18_refuses_to_open`,
  `test_tables_without_a_schema_version_refuse_to_open`,
  `test_a_failure_midway_leaves_the_store_at_18_untouched`.
- S3. **Tables** (ten; the upgrade makes exactly the schema a fresh install
  makes: every statement with its CHECKs, defaults and generated column,
  the columns, foreign keys and index columns; tests
  `test_the_upgrade_makes_the_same_schema_as_a_fresh_install`,
  `test_a_store_from_schema_1_ends_with_exactly_a_fresh_installs_schema`):
  - `audit`: every capability request and decision.
  - `tasks`: the scheduler. Kinds `reminder`, `followup` (investigations
    are follow-ups), `recurring` (system tasks) and `retry` (chat retries);
    states `pending`, `running`, `done`, `cancelled`, `failed`;
    compare-and-set claims, idempotency keys and the fired-once mark. It
    stays a table of its own because one scheduler serves all four kinds;
    a reminder's or investigation's item is written with each transition.
  - `approvals`: one record for every approval and proposal, keyed by
    `(kind, id)`, with a `subject`, a `state`, a JSON `payload` and the
    decision columns. Kinds: `approval` (the admin's consent bound to one
    action: update, reboot, change apply or revert, dashboard; id 32 hex,
    `plan_hash`, `requested_by`, `run_when`, `used_by`; states as
    [approvals.md](approvals.md) A5), `request` (a chat asked the admin to
    approve an update, [chat.md](chat.md) T9; `open`, `done`, `void`,
    `expired`), `change` (a config change proposal,
    [proposals.md](proposals.md); the payload holds the variant `change` or
    `facts`, why, effect, files with their blobs and SHA-256s, summary,
    diff, base and commit SHAs, ref, applied and reverted SHAs; states
    `refused`, `open`, `applying`, `applied`, `reverting`, `reverted`,
    `failed`, `stale`, `rejected`, `expired`) and `fact` (a house fact;
    `pending`, `applied`, `rejected`). A CHECK limits each kind to its own
    states. Every approval read and decision names `kind = 'approval'`, so
    no request, change or fact can ever be read as a pending or approved
    approval. Numbered kinds count up per kind.
  - `turns`: the one chat's turns. There is one implicit thread; its id,
    start time and read mark are in `kv` (`chat.main_thread`,
    `chat.created_at`, `chat.read`). Any other thread id reads as missing.
  - `runs`: the usage ledger, one row per AI run with its reservation and
    actuals ([budget.md](budget.md) B7); it records no cost.
  - `kv`: small state, one key each. Daily counters (`count:<day>:<name>`),
    quiet-hours holds (`push.held:<n>`, [items-and-pushes.md](items-and-pushes.md)
    N8), push action ids (`push.action:<id>`, N10), the reload times
    (`reload.last`, [reload.md](reload.md) L4), the chat (above), the
    standalone entry counter (`activity.seq`), item acknowledgements
    (`item.ack:<key>`), update flags, holds, reviews and first-seen times,
    maintenance state (S11a), backup and evidence state.
  - `update_runs`: one row per update window or manual plan (plan, review,
    backup, health before, summary). It stays a table of its own: it is
    the update pipeline's compare-and-set record (never two active runs),
    the parent of its jobs, and its retention depends on its backup.
  - `update_jobs`: one row per update with its compare-and-set state machine.
  - `items` (with the generated `needs` column and its CHECK) and
    `item_events`: [items-and-pushes.md](items-and-pushes.md).
- S11. **The upgrade from 18.** One transaction: the five tables whose shape
  changes (`tasks`, `approvals`, `turns`, `runs`, `items`) are renamed
  aside and created anew; every row is copied; 18's other tables are
  dropped; the indexes are created. It copies: every task of a kind this
  version runs (the never-used `condition` and `expires_at` columns go; the
  never-run state `waiting_approval` becomes `pending`; a task of a kind no
  version ran, `watch`, `window` or `intent`, is dropped and an item shown
  for it is resolved `cancelled` "not run by this version (an unused task
  kind)" with an event); every approval, approval
  proposal, change proposal and fact proposal into `approvals` (approvals
  with their plan, hash, state, decider and the job that used them,
  unchanged); the main chat's turns (a store without a recorded main chat
  has no reachable turns) with its start time and read mark; every run (the
  cost columns go); every item (the unused `confirm` and `derived` columns
  go) and every item event; counters, held pushes and issued push actions
  into `kv`; the highest activity number, of the log and of the
  standalone items, into `activity.seq`. A change row whose files or
  summary is not valid JSON is copied with an empty list. The activity
  log and the unused `spend` table are dropped: every activity entry
  already was an item or an item event, and an entry an older version never
  made an item for becomes a resolved standalone item (`done`, or `failed`
  when its title says so; never in Needs you) with one event, as migration
  17 did. `kv` keeps every key except the
  item backfill marker and panel confirmations (`confirm:*`), which nothing
  reads. 0.18's `/data/reloads.json` is copied verbatim into `reload.last`
  in the same transaction and deleted after it commits. Tests in
  `tests/test_store_upgrade.py` (an open approval stays bound and usable
  once; a pending request, change and fact stay actionable; a running job
  and its night carry on; held pushes, push actions, reminders, retries,
  the chat, the ledger, counters, kv, items, events, the audit log and the
  reload times survive), and `tests/test_store_history.py`, which replays
  a store from schema 1 through every frozen migration with rows written in
  each version's shape and upgrades it (nothing open is lost, no legacy row
  fails the upgrade).
- S12. **The copy before the upgrade.** Before the upgrade from 18 starts,
  the store is copied with SQLite's backup API (a consistent snapshot of
  the live WAL database, never a raw file copy) to `/data/ops.db.v18.bak`:
  created 0600 before anything is written to it, written as a temporary
  file, checked to have the store's mode and owner, fsynced, renamed into
  place and its directory fsynced. One `store copied aside before the upgrade` line is logged. If the
  copy cannot be made, the upgrade MUST NOT start: `StoreUnavailable`, the
  store untouched, the daemon degraded. The copy is deleted 14 days after
  it was made, at a start or the daily prune. The self-test checks it like
  the store (root, 0600) and the AI's account can never reach it
  ([privsep.md](privsep.md) V4, V7). **Manual rollback** (for the admin;
  nothing downgrades by itself), in this order: (1) stop the add-on; (2)
  delete `/data/ops.db-wal` and `/data/ops.db-shm`, because a WAL left by
  0.19.0 applied to the restored file makes it "database disk image is
  malformed"; (3) copy, never move, `/data/ops.db.v18.bak` to
  `/data/ops.db`, so the copy survives a failed attempt; (4) install
  0.18.x and start it. What happened after the upgrade is not in the copy.
  Tests `test_the_documented_rollback_gives_a_healthy_0_18_store`,
  `test_the_store_is_copied_aside_before_the_upgrade`,
  `test_a_copy_that_cannot_be_made_refuses_the_upgrade`,
  `test_the_copy_is_deleted_after_14_days`.
- S11a. Maintenance state lives in `kv` under `maintenance:` (`findings`,
  `meta`, and per finding `plan:`, `dashboard:`, `result:`, `migrated:`,
  `hidden:`); each scan blanks the plan, dashboard-plan and result records
  of findings that are gone. Test
  `test_only_deprecations_within_two_releases_need_the_admin_and_push`.
- S4. **Open and recover.** A corrupt file (`SQLITE_CORRUPT`, `SQLITE_NOTADB`
  or a failed quick check) is renamed to `ops.db.corrupt-<UTC stamp>` (with
  its WAL and SHM) and a new store is created; the daemon audits
  `store.recovered`. Busy or locked is retried with delays 0.5, 1, 2, 4
  seconds. Anything else (disk full, I/O error, read-only, cannot open, a
  schema version it does not use) raises `StoreUnavailable` and the daemon
  runs degraded. Tests `test_corrupt_file_is_quarantined_and_recreated`,
  `test_disk_and_io_errors_never_recreate`, `test_busy_is_retried_with_backoff`.
- S5. **Degraded mode** (no store): a degraded-store push, the health check
  still runs on its JSON files, no AI runs (`store_unavailable`), no chat.

## Retention

- S6. `prune` (daily, after each health check; `keep` default 90 days, never
  under 8): expired push actions, and used ones after 7 days; decided facts
  and requests after 90 days; resolved items not in Needs you and orphan
  events after 90 days; settled held pushes after 7 days;
  investigation-cancel keys after 7 days; runs and counters older than the
  cutoff; finished update runs after 180 days only when their backup is
  gone; review and seen keys after 180 days; finished non-system and retry
  tasks older than the cutoff. Audit rows older than 90 days and beyond
  50 000 are pruned. Approvals, changes and the chat's turns are never
  pruned. Run directories under `/data/runs` older than 60 days are
  deleted. Tests `test_prune_bounds_the_store`,
  `test_prune_keeps_the_rolling_week`, `test_audit_roundtrip_and_prune`,
  `test_the_main_chat_is_never_pruned`.

## JSON files and locks

- S7. `write_json_atomic` writes to a temporary file in the same directory,
  fsyncs, sets 0600 and renames; `append_jsonl` fsyncs each line;
  `read_jsonl` skips bad lines. Files are what the health check and pushes
  need without the store (S5): `/data/state.json` (`AgentState`, unknown
  keys ignored), `baseline.json`, `history.jsonl` (the last 100 lines are
  read), `notify.json` (the push budgets, N4), `open-items.json`; and
  `selftest.json` (the self-test's result, written only), `ops.db.v18.bak`
  (S12, for 14 days after the upgrade), `secrets/push.json`
  (the cached relay URL), `secrets/claude_oauth_token`. Up to 30 snapshots
  live under `/data/snapshots`. Dashboard backups taken before a dashboard
  save live under `/data/maintenance/dashboards/<url_path>/` (directories
  0700, files 0600 written with `O_EXCL | O_NOFOLLOW`, the last 10 per
  dashboard kept; [maintenance.md](maintenance.md) M22). Test
  `test_jsonstore`.
- S8. `/data/run.lock` is an exclusive non-blocking `flock` (0600, holds
  the pid), released by the kernel on a crash. Test `test_run_lock`.
- S9. Logs are one JSON object per line on stderr (`ts`, `level`, `logger`,
  `msg`, extra fields, `exc`), and every line passes the redactor built from
  `secrets.yaml` values, the provider token and the Supervisor token.

## Backups of the add-on

- S10. The add-on is backed up hot. Before a Supervisor backup,
  `ops-backup-pre` runs `opsd checkpoint`: a `PRAGMA
  wal_checkpoint(TRUNCATE)` with a 60-second busy timeout that always exits
  0 so a busy or broken store never fails the backup. `runs/*` and
  `snapshots/*` are excluded. Tests `test_checkpoint_main`,
  `test_checkpoint_never_fails_the_backup`.
