# Storage and migrations

Implementation: `opsd/store.py` (SQLite), `opsd/jsonstore.py`,
`opsd/state.py`, `opsd/logs.py`, `bin/ops-backup-pre`.
Tests: `tests/test_store.py`, `tests/test_store_upgrade.py` (the upgrade from
schema 19 on `tests/fixtures/schema_v19.sql`, a store 0.23.0's own code
wrote, dumped once and frozen), `tests/test_infra.py`, `tests/test_usage.py`
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
  user_version`; this version's is 20. An empty file gets the whole schema
  in one transaction. A store at 19 (add-ons 0.19 to 0.23) is upgraded by
  one migration in one transaction (S11); a failure anywhere in it rolls the
  file back to 19 untouched. A store at any other version below 20, or a
  file with tables but no version, MUST be refused (`StoreError`, "older than
  this agent upgrades": install 0.23 first) and a store newer than 20 MUST be
  refused ("newer": the add-on was downgraded); neither is ever quarantined
  or changed. **One upgrade only:** each release carries the upgrade from
  the previous schema alone, and the next schema change deletes it with its
  fixture and tests. Tests `test_newer_schema_is_refused`,
  `test_newer_schema_is_never_quarantined`, `test_failed_migration_rolls_back`,
  `test_a_store_at_any_other_version_is_refused_untouched`,
  `test_tables_without_a_schema_version_refuse_to_open`,
  `test_a_failure_midway_leaves_the_store_at_19_untouched`.
- S3. **Tables** (ten; the upgrade makes exactly the schema a fresh install
  makes: every statement with its CHECKs, defaults and generated column,
  the columns, foreign keys and index columns; test
  `test_the_upgrade_makes_the_same_schema_as_a_fresh_install`):
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
    action: update, reboot, change apply or revert, fork rebase; id 32 hex,
    `plan_hash`, `requested_by`, `run_when`, `used_by`; states as
    [approvals.md](approvals.md) A4), `change` (a config change proposal,
    [proposals.md](proposals.md); the payload holds the variant `change` or
    `facts`, why, effect, files with their blobs and SHA-256s, summary,
    diff, base and commit SHAs, ref, applied and reverted SHAs; states
    `refused`, `open`, `applying`, `applied`, `reverting`, `reverted`,
    `failed`, `stale`, `rejected`, `expired`) and `fact` (a house fact;
    `pending`, `applied`, `rejected`). A CHECK limits each kind to its own
    states. Every approval read and decision names `kind = 'approval'`, so
    no change or fact can ever be read as an approval. Numbered kinds count up per kind.
  - `turns`: the conversations' turns. A turn's thread is `meta.thread`
    (`item:<item id>` or `ask:<item id>`, [chat.md](chat.md) T1), written
    by the store alone and never moved by a meta update; a thread exists
    only while its item is stored. A conversation's read mark is
    `chat.read:<thread>`. Any other thread id reads as
    missing.
  - `runs`: the usage ledger, one row per AI run with its reservation
    (turns and run time) and actuals ([budget.md](budget.md) B7); it
    records no cost.
  - `kv`: small state, one key each. Daily counters (`count:<day>:<name>`),
    quiet-hours holds (`push.held:<n>`, [items-and-pushes.md](items-and-pushes.md)
    N8), push action ids (`push.action:<id>`, N10), the reload times
    (`reload.last`, [reload.md](reload.md) L4), the read marks (above), the
    standalone entry counter (`activity.seq`), item acknowledgements
    (`item.ack:<key>`), update flags, holds, reviews and first-seen times,
    maintenance state (S4), verdicts (`verdict:<cause>`,
    [maintenance.md](maintenance.md) M13d), the daily push digest
    (`items.digest`, [items-and-pushes.md](items-and-pushes.md) N1a),
    backup and evidence state.
  - `update_runs`: one row per update window or manual plan (plan, review,
    backup, health before, summary). It stays a table of its own: it is
    the update pipeline's compare-and-set record (never two active runs),
    the parent of its jobs, and its retention depends on its backup.
  - `update_jobs`: one row per update with its compare-and-set state machine.
  - `items` (with the generated `needs` column and its CHECK) and
    `item_events`: [items-and-pushes.md](items-and-pushes.md).
- S4. Maintenance state lives in `kv` under `maintenance:` (`findings`,
  `meta`, and per finding `plan:`, `hidden:`, `gone:` (why the scan dropped
  it)); each scan blanks the plan and gone records of findings that are
  gone, and
  their `verdict:maint:` records. Verdicts and the digest are `kv` rows. Test
  `test_only_deprecations_within_30_days_of_their_deadline_need_the_admin_and_push`.
- S5. **Open and recover.** A corrupt file (`SQLITE_CORRUPT`, `SQLITE_NOTADB`
  or a failed quick check) is renamed to `ops.db.corrupt-<UTC stamp>` (with
  its WAL and SHM) and a new store is created; the daemon audits
  `store.recovered`. Busy or locked is retried with delays 0.5, 1, 2, 4
  seconds. Anything else (disk full, I/O error, read-only, cannot open, a
  schema version it does not use) raises `StoreUnavailable` and the daemon
  runs degraded. Tests `test_corrupt_file_is_quarantined_and_recreated`,
  `test_disk_and_io_errors_never_recreate`, `test_busy_is_retried_with_backoff`.
- S6. **Degraded mode** (no store): a degraded-store push, the health check
  still runs on its JSON files, no AI runs (`store_unavailable`), no chat.
  Everything that needs the store (the scheduler, usage guard, evidence
  cache, updates, backups, changes, chat, activity, maintenance and the
  item book) is made together once the store opened and is absent as a
  whole while degraded (`daemon.StoreServices`). Test
  `test_degraded_without_store`.

## Retention

- S7. `prune` (daily, after each health check; `keep` default 90 days, never
  under 8): expired push actions, and used ones after 7 days; decided facts
  after 90 days; resolved items not in Needs you and orphan
  events after 90 days; settled held pushes after 7 days;
  investigation-cancel keys after 7 days; evidence cache entries
  (`evidence:`) not written for 30 days; runs and counters older than the
  cutoff; finished update runs after 180 days only when their backup is
  gone; review and seen keys after 180 days; finished non-system and retry
  tasks older than the cutoff. Audit rows older than 90 days and beyond
  50 000 are pruned. A conversation's turns older than 90 days go once
  their item is gone (resolved items after 90 days). Approvals and changes are never pruned.
  Run directories under `/data/runs` older than 60 days are deleted. Tests
  `test_prune_bounds_the_store`, `test_prune_keeps_the_rolling_week`,
  `test_audit_roundtrip_and_prune`, `test_a_conversation_goes_with_its_item`.

## JSON files and logs

- S8. `write_json_atomic` writes to a temporary file in the same directory,
  fsyncs, sets 0600 and renames; `append_jsonl` fsyncs each line;
  `read_jsonl` skips bad lines. Files are what the health check and pushes
  need without the store (S6): `/data/state.json` (`AgentState`, unknown
  keys ignored), `baseline.json`, `history.jsonl` (the last 100 lines are
  read), `notify.json` (the push budgets, N4), `open-items.json` (the
  last check's open items and its time; acks are the baseline's, H13a),
  `secrets/push.json`
  (the cached relay URL), `secrets/claude_oauth_token`. Up to 30 snapshots
  live under `/data/snapshots`. Home Assistant
  Core's source for `core-compare` and fork rebases lives under
  `/data/core-source/` (listings by tag, files by blob SHA;
  [capabilities.md](capabilities.md) K13), and each fork rebase proposal's
  rebased files and the snapshot of the files it replaced under
  `/data/fork-rebase/<id>/` (`new/`, `old/`; [proposals.md](proposals.md)
  C14); both root-only, directories 0700 and files 0600. Proposals
  themselves are key-value records (`fork:proposal:<id>`). Test
  `test_jsonstore`.
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

## The upgrade from schema 19

- S11. **The upgrade from 19.** One transaction: `approvals`, `runs` and
  `items` (a CHECK or the columns changed) are renamed aside, created anew,
  their rows copied and the old tables dropped; then the indexes are
  created. Every row is kept as it was except:
  - `approvals`: the chat approval requests (kind `request`) and every
    approval of a dashboard change are dropped (0.24.0 has neither); an
    approval still `pending` (never confirmed) is `void`, "never confirmed
    (0.24.0 has no confirmation step)", never granted.
  - `runs`: the unused columns `week`, `month`, `reserved_tokens`,
    `reserved_share`, `share_5h`, `share_7d`, `share_estimated` go.
  - `items`: a question (key `ask:`) gets the kind `question`; the earlier
    one chat's item (`chat:earlier`) goes with its events. An item push
    0.23.0 held for quiet hours (`push.held:*` whose push's tag is the
    item's id) is dropped and the item's `push` cleared, so the item book
    decides it again ([items-and-pushes.md](items-and-pushes.md) N8); the
    daemon's own held pushes stay.
  - `turns`: the earlier one chat's turns (no `meta.thread`) go; its
    pending retry tasks are cancelled; the `status.refresh` system task
    goes (nothing runs it).
  - `kv`: `chat.main_thread`, `chat.created_at`, `chat.read`,
    `maintenance:dashboard:*`, `maintenance:result:*`,
    `maintenance:migrated:*` and the counters `chat_turns`,
    `chat_cap_notice`, `followup_runs`, `cap:plan_fix`,
    `cap:approval.request`, `cap:dashboard.save`, `cap:baseline.ack` go;
    `maintenance:meta` loses `trigger`, `problems` and `inventory`; stored
    maintenance findings of the removed `suggestion` variant
    (`marketplace` signal) go with their plan and hiding; a hiding or plan
    recorded under 0.22's evidence hash is rewritten to the finding's own
    fingerprint (it stays hidden, its plan reused); a stored finding the
    upgrade cannot read (a Python error over it, not a database error) goes
    with its plan and hiding, logged, so that it can never wedge the start
    (the next scan raises it again if it still applies). Audit rows are
    history and are all kept.
  After it commits, the files 0.23.0 left beside the store that nothing
  reads are deleted: `ops.db.v18.bak` (the 0.19 upgrade copy), `run.lock`
  and `selftest.json`; `/data/maintenance/dashboards/` (backups of
  dashboards 0.23.0 changed) is left for the admin. No copy is taken
  before the upgrade: the add-on's own backup before an update is the way
  back. Tests in `tests/test_store_upgrade.py`:
  `test_every_kept_row_is_unchanged_and_only_what_s11_names_changed`,
  `test_the_dropped_rows_columns_and_keys_are_gone`,
  `test_a_finding_hidden_or_planned_under_the_0_22_hash_stays_hidden_and_planned`,
  `test_a_held_item_push_goes_back_to_the_item_book_and_daemon_pushes_stay_held`,
  `test_open_records_still_work_through_the_current_api` (an approved
  change and update still bind and are used once; the running job, the
  reminder, facts, conversations, the ledger and push actions carry on),
  `test_a_second_open_changes_nothing`,
  `test_the_files_0_23_0_left_beside_the_store_are_deleted`.
