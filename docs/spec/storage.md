# Storage and migrations

Implementation: `opsd/store.py` (SQLite), `opsd/jsonstore.py`,
`opsd/state.py`, `opsd/lock.py`, `opsd/logs.py`, `bin/ops-backup-pre`.
Tests: `tests/test_store.py`, `tests/test_infra.py`, `tests/test_usage.py`
(pruning), `tests/test_chat_retention.py`.

## The store

- S1. One SQLite database at `/data/ops.db`, created with mode 0600 before
  SQLite opens it; `-wal` and `-shm` are set to 0600 after connecting.
  Pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
  `trusted_schema=OFF`, and `quick_check` on every open. One connection,
  serialised by a lock; writes run in `BEGIN IMMEDIATE` transactions. Tests
  `test_migrated_private_and_reopenable`,
  `test_store_file_created_private_even_with_open_umask`.
- S2. **Migrations** are an ordered tuple of SQL scripts; the schema version
  is their count (18 in 0.18.0) and is tracked in `PRAGMA user_version`.
  Each pending script runs in one transaction and sets the version; a
  failure rolls back. A database with a newer version than the code MUST be
  refused (`StoreError`) and never quarantined. Tests
  `test_newer_schema_is_refused`, `test_newer_schema_is_never_quarantined`,
  `test_failed_migration_rolls_back`.
- S3. Tables: `audit`, `tasks`, `approvals`, `threads`, `turns`, `spend`,
  `counters`, `issued_actions`, `runs`, `kv`, `fact_proposals`,
  `update_runs`, `update_jobs`, `approval_proposals`, `change_proposals`,
  `held_pushes`, `activity`, `items` (with the generated `needs` column and
  its CHECK), `item_events`.
- S11. **Migration 18** adds the `maintenance` item kind. SQLite cannot
  change a CHECK in place, so it rebuilds `items` as `items_v18` with the
  same columns and the `kind` CHECK extended by `maintenance`, copies every
  row, drops the old table, renames the new one and recreates the
  `items_needs`, `items_at` and `items_state` indexes; the generated
  `needs` column is recomputed. No other table changes. Maintenance state
  lives in `kv` under `maintenance:` (`findings`, `meta`, and per finding
  `plan:`, `dashboard:`, `result:`, `migrated:`, `hidden:`); each scan
  blanks the plan, dashboard-plan and result records of findings that are
  gone. Tests `test_migration_17_backfills_items_and_nothing_old_needs_you`
  (a version-16 store migrated to 18 keeps its items),
  `test_only_deprecations_within_two_releases_need_the_admin_and_push`.
- S4. **Open and recover.** A corrupt file (`SQLITE_CORRUPT`, `SQLITE_NOTADB`
  or a failed quick check) is renamed to `ops.db.corrupt-<UTC stamp>` (with
  its WAL and SHM) and a new store is created; the daemon audits
  `store.recovered`. Busy or locked is retried with delays 0.5, 1, 2, 4
  seconds. Anything else (disk full, I/O error, read-only, cannot open, a
  newer schema) raises `StoreUnavailable` and the daemon runs degraded.
  Tests `test_corrupt_file_is_quarantined_and_recreated`,
  `test_disk_and_io_errors_never_recreate`, `test_busy_is_retried_with_backoff`.
- S5. **Degraded mode** (no store): a degraded-store push, the health check
  still runs on its JSON files, no AI runs (`store_unavailable`), no chat.

## Retention

- S6. `prune` (daily, after each health check; `keep` default 90 days, never
  under 8): expired or used issued actions after 7 days; decided fact and
  approval proposals after 90 days; activity after 90 days; resolved items
  not in Needs you and orphan events after 90 days; settled held pushes
  after 7 days; investigation-cancel and confirm keys after 7 days; runs and
  counters older than the cutoff; finished update runs after 180 days only
  when their backup is gone; review and seen keys after 180 days; finished
  non-system and retry tasks older than the cutoff. Audit rows older than
  90 days and beyond 50 000 are pruned. Threads and turns are never pruned.
  Run directories under `/data/runs` older than 60 days are deleted. Tests
  `test_prune_bounds_the_store`, `test_prune_keeps_the_rolling_week`,
  `test_audit_roundtrip_and_prune`, `test_the_main_chat_is_never_pruned`.

## JSON files and locks

- S7. `write_json_atomic` writes to a temporary file in the same directory,
  fsyncs, sets 0600 and renames; `append_jsonl` fsyncs each line;
  `read_jsonl` skips bad lines. Files: `/data/state.json` (`AgentState`,
  unknown keys ignored), `baseline.json`, `history.jsonl` (the last 100
  lines are read), `notify.json`, `open-items.json`, `reloads.json`,
  `selftest.json`, `secrets/push.json` (the cached relay URL),
  `secrets/claude_oauth_token`. Up to 30 snapshots live under
  `/data/snapshots`. Dashboard backups taken before a dashboard save live
  under `/data/maintenance/dashboards/<url_path>/` (directories 0700, files
  0600 written with `O_EXCL | O_NOFOLLOW`, the last 10 per dashboard kept;
  [maintenance.md](maintenance.md) M22). Test `test_jsonstore`.
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
