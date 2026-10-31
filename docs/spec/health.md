# The health check, the baseline and triage

The daily check compares the system with an accepted baseline. Nothing new
means no AI run and no push. The daemon decides severities; the AI only
reads and comments.

Implementation: `opsd/health.py` (collection), `opsd/baseline.py` (baseline,
diff, acks), `opsd/triage.py` (prompt and result), `opsd/fingerprint.py`,
`opsd/queries.py`, the health cycle in `opsd/daemon.py`. Tests:
`tests/test_health_baseline.py`, `tests/test_daemon.py`,
`tests/test_notify_triage.py`, `tests/test_needs_and_watches.py`.

## Schedule

- H1. The check runs daily at `health.check_time` (house time zone). At
  start, if the last check is unknown or older than 24 hours it runs after
  the startup delay (trigger `catch_up`), else at the next check time.
  Tests `test_schedule_catch_up_then_daily`, `test_next_run_at`.
- H2. Other triggers (`manual` from the panel, `core_recovered`, `overdue`)
  go through one queue: a cycle already running or queued is not duplicated;
  `manual` is capped at 6 per day. The liveness loop requests `overdue` when
  the last check is 26 hours old (after a 2-hour grace at start, retried at
  most every 6 hours).
- H3. One cycle at a time: an asyncio lock plus a non-blocking `flock` on
  `/data/run.lock`; a second caller gets `skipped_busy`. Test `test_run_lock`.
- H4. Before a cycle the daemon waits for updates to be idle and runs update
  upkeep (`reconcile`); a crash marks the status sensor `failed`; store and
  run-directory retention runs after every check.

## Collection

- H5. Sources, each guarded so a failure is recorded in `snapshot.errors`
  and never fatal: Supervisor `/core/info`, `/host/info`, `/supervisor/info`,
  `/os/info`, `/resolution/info`, `/backups`, `/addons`, the kernel log (500
  lines); Core `/states`; websocket entity registry, config entries, repairs,
  system log, backup info, Assist exposure, HACS repository info for pending
  HACS updates; `/proc/meminfo`; `custom_components/*/manifest.json`; local
  git status when git is on; overlay sources only when enabled. Up to 30
  snapshots are kept as JSON under `/data/snapshots`.
- H6. "Down" means state `unavailable`, or `unknown` only for light, fan,
  cover, lock, climate, water_heater, humidifier, vacuum, lawn_mower, valve,
  alarm_control_panel, media_player and siren. Disabled and user-hidden
  entities are skipped.
- H7. A failed config entry is one in `setup_error`, `setup_retry`,
  `migration_error` or `failed_unload` and not disabled. Ignored and
  dismissed repairs are skipped. Log patterns are ERROR and CRITICAL lines
  only, fingerprinted by logger and normalised message (12 hex characters).
- H8. A backup is "complete" when it holds the config, the database and
  every installed add-on; the Core backup manager is preferred, the
  Supervisor list is the fallback. Access entities are `lock.*`,
  `alarm_control_panel.*` and covers with device class garage or gate (or
  "garage" in the id); they are "exposed" when Assist exposure lists them
  with `conversation: true`; a malformed answer makes exposure unknown.
- H9. A config check (Supervisor `POST /core/check`, 180 seconds) runs in the
  daily cycle only (not for `opsctl health`). A timeout leaves `ok` unknown;
  an answered failure sets it false.

## Items and severities

Keys are stable so acks survive (`entities:<platform>`, `custom:<domain>`,
`entry:<id>`, `repair:<key>`, `supervisor:<issue>`, `log:<fp>`,
`update:<entity>@<latest>`, `assist:<entity>`, ...).

| Item | Severity | Rule |
| --- | --- | --- |
| `core:down` | critical | Core state not running, or the API down. |
| `entities:<platform>` | critical for a custom integration, else warning | Newly down entities not in the baseline or `health.expected_unavailable`; flips younger than 6 hours are skipped unless the platform is custom and the state is hard `unavailable`. |
| `custom:<domain>` | critical | A custom integration that was ok in the baseline now has a failed entry or all entities down. |
| `entry:<id>` | critical for a custom domain, else warning | New failed config entry. |
| `repair:<key>` | critical iff Home Assistant says critical, else warning | New repair. |
| `supervisor:<issue>` | warning | New Supervisor issue; `no_current_backup` is dropped when a complete encrypted backup finished within 26 hours and is critical otherwise; unhealthy/unsupported reasons are warnings every run. |
| `log:<fp>`, `log:<fp>:growth` | warning | Unseen fingerprint; or a count at least 10 times the baseline. |
| `update:<entity>@<latest>` | info | Once per version. |
| `assist:<entity>` | critical | An access entity exposed to Assist and not accepted; `assist:check_failed` is a warning. |
| `backup:complete_stale`, `backup:stale`, `backup:unprotected` | warning | No complete backup or older than 168 hours; any backup older than 26 hours; unencrypted backups exist. |
| `disk:low` | warning | Free space under 15 %. |
| `memory:low`, `memory:swap` | critical, warning | Available memory under 600 MB; swap use over 50 %. |
| `host:reboot:<ts>` | warning | Boot timestamp changed. |
| `host:oom:*` | critical | OOM kills not seen before (reset on reboot). |
| `config:invalid` | critical | Only when the config check answered false. |
| `git:dirty`, `git:diverged` | info | Local config repository state. |
| `power:undervoltage` | warning | The profile's power entity is `on`. |
| `remote:down` | warning | The profile's remote entity has been off for 30 minutes; it waits quietly (no push, no ask) until 6 hours after it went down. |
| `options:invalid` | warning | Options or profile refused. |
| `guard:anomaly` | warning | A guard-hook anomaly in the last 7 days. |
| `backup:run_failed` | warning | A standalone backup failed. |

- H10. Thresholds and watches are stateless: they are raised every run until
  fixed or acked. Membership checks (entities, entries, repairs, issues,
  fingerprints, updates) are relative to the baseline. Active acks are
  removed last. Tests `test_health_baseline.py` (diff and thresholds),
  `test_the_items_engine_asks_only_once_remote_access_stays_down`.

## Baseline

- H11. The first run seeds the baseline with the current unavailable
  entities, failed entries, repairs, Supervisor issues, log fingerprints,
  pending update versions, boot and OOM state. It MUST NOT accept custom
  integration entities, custom-domain failed entries or critical repairs;
  those are reported again until accepted in the panel. `custom_ok` starts
  true for every custom domain; accepted exposures start empty. The run
  sends one push ("the agent is watching") that is urgent and safety-class
  iff something was not accepted. Tests `test_health_baseline.py:36-55`,
  `test_daemon.py` baseline tests.
- H12. After every processed run the baseline advances: recovered entries
  drop out (a recurrence is new again), fingerprints keep their maximum
  count and expire after 30 days, `custom_ok` becomes true when healthy and
  is never set false by advancing, accepted exposures are intersected with
  the current ones, expired acks are removed.
- H13. Acks: days are clamped to 1..30. A critical item acked by anyone but
  the admin is a snooze of at most 3 days, never an accept; a warning snooze
  by the AI is at most 7 days. The panel's `accept` (30 days) and `snooze`
  (7 days) act as the admin and are the only way to accept a critical item;
  accept folds the item into the baseline by kind. The AI's acks (`baseline.ack`)
  are applied after the run, only for keys handed to that run, and survive a
  failed run. Tests `test_health_baseline.py:123-153`,
  `test_daemon.py:498-538`.

## Triage

- H14. The AI runs only when items remain after suppression and after
  removing kinds the daemon pushes itself. No items: the baseline advances,
  the result is "No changes since baseline.", no push. Test
  `test_daemon.py:114-124`.
- H15. The prompt holds at most 40 items (critical first), each with key,
  summary and detail (700 characters) fenced as data, plus versions, memory,
  newest complete backup, unavailable count, git state, failed sources and
  the last 5 run summaries; the whole prompt passes the secrets redactor.
  Test `test_triage_fences_every_derived_field`.
- H16. The triage profile gets the read capabilities plus `task.create`,
  `task.list`, `baseline.ack`, `notify.admin`, `suppress.propose` and
  `integration.reload` (see [capabilities.md](capabilities.md)). It MUST NOT
  change a severity. Its result is `{result: ok|escalated|failed, summary}`;
  an unknown result is `failed`. At most 6 triage runs a day.
- H17. **Safety net.** Whatever the AI did or whether it ran: any critical
  item sends an urgent safety-class push (tag `ha-ops-fallback`); when
  triage did not run, warnings send one non-urgent push; info-only sends
  nothing; quietly waiting items are excluded. A preflight refusal (invalid
  options, no provider, no token, no store, AI off, low memory) makes the
  cycle result that status and the fallback applies. Tests
  `test_daemon.py:127-178`, `test_without_token_sends_deterministic_fallback`.
- H18. Each cycle is recorded in `history.jsonl`, `state.json`, the status
  sensor (`attention` while anything is unacked) and one activity line.

## Evidence and the config check

- H19. `opsd/evidence.py` fetches release notes for the update rule only:
  HTTPS to `www.home-assistant.io` and `api.github.com` only, same-host
  redirects, 3 MiB, 20 seconds; results cached 12 hours in the store; a
  failed or unrecognised answer is never cached and marks the evidence
  unavailable; GitHub 403/429 backs off 30 minutes. Parsers fail closed.
  Tests `tests/test_evidence.py`, `test_the_hardened_fetch_refuses_any_other_host`.
- H20. `opsd/configcheck.py` is the fail-closed allowlist over proposed YAML
  (see [proposals.md](proposals.md)); it is not part of the health check.
