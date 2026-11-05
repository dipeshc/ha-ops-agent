# The health check, the baseline and triage

The daily check compares the system with an accepted baseline. Nothing new
means no AI run and no push. The daemon decides severities; the AI only
reads and comments.

Implementation: `opsd/health.py` (collection), `opsd/access.py`
(`opens_the_house`, shared with reloads), `opsd/baseline.py` (baseline,
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
- H3. One cycle at a time (the daemon is one process): a second caller
  gets `skipped_busy`. Test `test_one_health_cycle_at_a_time`.
- H4. Before a cycle the daemon waits for updates to be idle and runs update
  upkeep (`reconcile`); a crash marks the status sensor `failed`; store and
  run-directory retention runs after every check, then the maintenance scan
  ([maintenance.md](maintenance.md) M19).

## Collection

- H5. Sources, each guarded so a failure is recorded in `snapshot.errors`
  and never fatal: Supervisor `/core/info`, `/host/info`, `/supervisor/info`,
  `/os/info`, `/resolution/info`, `/backups`, `/addons`, the kernel log (500
  lines); Core `/states`; websocket entity registry, config entries, repairs,
  system log, backup info, Assist exposure; `/proc/meminfo`;
  `custom_components/*/manifest.json`; local git status when git is on;
  overlay sources only when enabled. The daily check lists the pending
  updates unclassified (no network call; the update planner classifies
  them with HACS repository info and release ages, [updates.md](updates.md),
  and the check only tells whether the plan is stale). Up to 30 snapshots
  are kept as JSON under `/data/snapshots`. Test
  `test_a_new_update_is_no_health_item`.
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
  Supervisor list is the fallback. Access hardware
  (`opsd.access.acts_on_the_house`) is a lock or an alarm panel by domain, a
  cover that is not positively a blind, shade, curtain, awning or shutter,
  or a switch, button, script, scene, input boolean, valve or cover whose id,
  friendly name or registry names say access. Only domains that act count by
  name: a binary sensor, sensor, camera, light, event or device tracker
  named like a door observes it and is never access hardware here. (The
  expected list, H11a, and reloads, [reload.md](reload.md) L3, keep the
  wider `opens_the_house`, which counts names in any domain.) It is
  "exposed" when Assist exposure lists it with `conversation: true`; a
  malformed answer makes exposure unknown. Tests
  `test_assist_exposure_covers_everything_that_opens_the_house`,
  `test_only_entities_that_act_are_access_hardware_by_name`.
- H9. A config check (Supervisor `POST /core/check`, 180 seconds) runs in the
  daily cycle only (not for `opsctl health`). A timeout leaves `ok` unknown;
  an answered failure sets it false.

## Items and severities

Keys are stable so acks survive (`entities:<platform>`, `custom:<domain>`,
`entry:<id>`, `repair:<key>`, `supervisor:<issue>`, `log:<fp>`,
`assist:<entity>`, `access:<entity>`, ...).

| Item | Severity | Rule |
| --- | --- | --- |
| `core:down` | critical | Core's API does not answer. |
| `entities:<platform>` | critical for a custom integration, else warning | Newly down entities not in the baseline or `health.expected_unavailable`; flips younger than 6 hours are skipped unless the platform is custom and the state is hard `unavailable`. Access hardware (H8) is never part of it. |
| `access:<entity>` | critical | A piece of access hardware (H8) that is down and that the admin has not accepted as down; one item per entity, the same flap rule as `entities:`. The baseline's `unavailable` list never silences it, so an ack or accept of an integration's item cannot hide a lock or alarm panel that goes down later. Test `test_an_integration_ack_never_hides_access_hardware`. |
| `custom:<domain>` | critical | A custom integration that was ok in the baseline now has a failed entry or all entities down. An entity in `health.expected_unavailable` MUST NOT count here, neither as down nor in the entity total, so the integration is still broken when every other entity is down (the snapshot still lists it as unavailable). Test `test_expected_unavailable_entities_do_not_break_a_custom_integration`. |
| `entry:<id>` | critical for a custom domain, else warning | New failed config entry. |
| `repair:<key>` | critical iff Home Assistant says critical, else warning | New repair. |
| `supervisor:<issue>` | warning | New Supervisor issue; `no_current_backup` is dropped when a complete encrypted backup finished within 26 hours and is critical otherwise; unhealthy/unsupported reasons are warnings every run. |
| `log:<fp>`, `log:<fp>:growth` | warning | Unseen fingerprint; or a count at least 10 times the baseline. |
| `assist:<entity>` | critical | Access hardware (H8) exposed to Assist and not accepted; `assist:check_failed` is a warning. |
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
  fingerprints) are relative to the baseline. Active acks are
  removed last. Tests `test_health_baseline.py` (diff and thresholds),
  `test_the_items_engine_asks_only_once_remote_access_stays_down`.

## Baseline

- H11. The first run seeds the baseline with the current unavailable
  entities, failed entries, repairs, Supervisor issues, log fingerprints,
  boot and OOM state. It MUST NOT accept custom
  integration entities, access hardware that is down (H8), custom-domain
  failed entries or critical repairs; those are reported again until
  accepted in the panel. A custom
  integration entity in `health.expected_unavailable` is accepted and does
  not count toward a broken custom integration (as in `custom:<domain>`). `custom_ok` starts
  true for every custom domain; accepted exposures start empty. The run
  sends one push ("the agent is watching") that is urgent and safety-class
  iff something was not accepted. Tests `test_seed_then_no_changes`,
  `test_baseline_of_many_unavailable_is_quiet`,
  `test_the_seed_accepts_expected_unavailable_custom_entities`,
  `test_the_first_run_never_accepts_access_hardware_that_is_down`,
  `test_daemon.py` baseline tests.
- H11a. **Access hardware is never expected.** `health.expected_unavailable`
  is read only through `baseline.honoured_expected`, which drops what opens
  the house (`opsd.access.opens_the_house`, the same rule as
  [reload.md](reload.md) L3): a lock or an alarm panel; a cover whose device
  class is not `blind`, `shade`, `curtain`, `awning` or `shutter` (no class,
  `door`, `garage`, `gate` or any other counts: fail closed); anything
  whose id, friendly name or registry name matches the access-word list. A
  dropped entry is reported as if it were not listed, with the same
  severity, everywhere the list is read: the `entities:` and `custom:`
  checks and the seed of the daily check, `opsctl health --diff`, the
  health compare after an update install (a listed lock going down is a
  regression and rolls the update back) and the compare after an OS update
  is confirmed past a reboot. Where only an entity id is known (the
  update compares), every cover counts. The daemon logs one warning per
  such entry per start and starts normally. Tests
  `test_access_hardware_is_never_expected_unavailable`,
  `test_a_listed_lock_is_still_reported_and_warned_about_once`,
  `test_health_diff_never_silences_a_listed_lock`,
  `test_a_listed_lock_going_down_after_an_update_still_rolls_it_back`,
  `test_a_listed_lock_going_down_after_the_reboot_is_still_worse_health`.
- H12. After every processed run the baseline advances: recovered entries
  drop out (a recurrence is new again), fingerprints keep their maximum
  count and expire after 30 days, `custom_ok` becomes true when healthy and
  is never set false by advancing, accepted exposures and accepted access
  hardware are intersected with the current ones (access hardware that
  recovers and goes down again is reported again), expired acks are removed.
  A new log fingerprint is therefore reported once: its item then leaves
  with "not new any more: this error is part of the baseline now; it is
  reported again if it grows tenfold", never "cleared", because the error
  may continue ([items-and-pushes.md](items-and-pushes.md) I5a). Test
  `test_a_new_log_error_is_not_cleared_while_it_continues`.
- H13. Acks: days are clamped to 1..30. A critical item acked by anyone but
  the admin is a snooze of at most 3 days, never an accept; a warning snooze
  by anyone but the admin is at most 7 days. The panel's `accept` (30 days)
  and `snooze` (7 days) act as the admin and are the only way to accept a
  critical item; accept folds the item into the baseline by kind (an
  `access:` item into the accepted access hardware, until it recovers). An
  ack keeps the item it acked. The AI never acks: the daemon snoozes for a
  triage verdict (H16a). Tests `test_ack_expiry_and_accept`,
  `test_access_hardware_is_its_own_critical_item_the_agent_cannot_accept`.
- H13a. An item acked since the check ran (the baseline's acks made since
  `open-items.json`'s `at`: by the daemon for a triage verdict, or by the
  admin in the panel) is handled, not open: it leaves the open items (the
  panel's health list, the status sensor's count, Needs you) and its item is
  shown resolved with who acked it, until when and why ("snoozed until … by
  the agent: <reason>", "accepted as normal by you"). An item the daemon
  snoozed for the agent keeps the panel's `snooze` and `accept` actions
  there, so the admin can override it without it entering Needs you.
  `open-items.json` holds only the check's open items and its time. Tests
  `test_a_not_affected_verdict_snoozes_a_warning_for_at_most_a_week`,
  `test_user_ack_from_panel_can_accept`.

## Triage

- H14. The AI runs only when warning or critical items remain after
  suppression; info items (`git:`) and the vacuums overlay's room drift
  (pushed by the item book, [items-and-pushes.md](items-and-pushes.md) I10)
  never start a run and are not in its prompt. No such items: the baseline
  advances, the open items are saved, the result is "No changes since
  baseline." (or the drift's summary), no push. Tests
  `test_first_run_seeds_and_notifies_once`,
  `test_a_new_update_version_is_no_health_item`.
- H15. The prompt holds at most 40 items (critical first), each with key,
  summary and detail (700 characters) fenced as data, plus versions, memory,
  newest complete backup, unavailable count, git state, failed sources and
  the last 5 run summaries; the whole prompt passes the secrets redactor.
  Test `test_triage_fences_every_derived_field`.
- H16. The triage profile gets the read capabilities plus `task.create`,
  `task.list`, `notify.admin`, `suppress.propose` and
  `integration.reload` (see [capabilities.md](capabilities.md)). It MUST NOT
  change a severity. It handles what it can itself and pushes only when the
  admin must act or decide: something it cannot handle, a physical task or
  a security-relevant change, given as the push's `reason` class; anything
  for information is a History note (`--reason note`), and a call without
  a valid reason is refused (capabilities.md K11). Its result is `{result:
  ok|escalated|failed, summary, verdicts}`; an unknown result is `failed`.
  At most 6 triage runs a day.
- H16a. **The conclusion lives on the item.** `verdicts` holds, per item it
  investigated, `{key, verdict: not_affected|fixed|needs_owner, reason}`
  (at most 40, only keys handed to the run, the reason cleaned and cut at
  300 characters), and for `needs_owner` an `owner_action` (what the
  admin must do or decide, maintenance.md M13d); a `needs_owner` without a
  usable one is dropped (no verdict). The daemon keeps each one under
  `verdict:health:<key>` for that item's current summary (`opsd.verdicts`).
  `needs_owner` keeps the item in Needs you with the reason under The
  agent says and the owner action quoted in the need, and the item
  book pushes it ([items-and-pushes.md](items-and-pushes.md) N1, the digest
  unless time-sensitive); the run does not
  push it. For `not_affected` and `fixed` the daemon snoozes the item for
  at most 7 days (`WARNING_SNOOZE_MAX_DAYS`) with the reason (H13a); never
  a critical item, `access:`, `assist:`, `core:down` or `custom:` (the
  verdict guards, [maintenance.md](maintenance.md) M13d), which stay with
  the admin. Without a verdict the item stays as the check raised it. Tests
  `test_a_triage_verdict_anchors_the_reason_on_its_item`,
  `test_a_not_affected_verdict_snoozes_a_warning_for_at_most_a_week`,
  `test_non_urgent_claude_push_does_not_suppress_critical`.
- H17. **Safety net.** Whatever the AI did or whether it ran: any critical
  item sends an urgent safety-class push (tag `ha-ops-fallback`); when
  triage did not run, warnings send one non-urgent push; info-only sends
  nothing; quietly waiting items are excluded. A preflight refusal (invalid
  options, no token, no store, AI off, low memory) makes the
  cycle result that status and the fallback applies. Tests
  `test_silent_claude_on_critical_item_still_alerts`,
  `test_without_token_sends_deterministic_fallback`.
- H18. Each cycle is recorded in `history.jsonl`, `state.json`, the status
  sensor (`attention` while anything is unacked) and one activity line. The
  status sensor (`sensor.ops_agent`) is readable by every Home Assistant
  user and add-on, so its attributes carry daemon-written fields only: never
  the triage summary or an update night's summary (the panel's `api/state`
  keeps them for the admin). Test
  `test_broken_custom_integration_runs_claude_and_notifies`.

## Evidence and the config check

- H19. `opsd/evidence.py` fetches release notes for the update rule and,
  for proactive maintenance, release posts, archived flags of custom
  integrations' repositories and Core's integration listings
  ([maintenance.md](maintenance.md) M4–M5):
  HTTPS to `www.home-assistant.io` and `api.github.com` only, same-host
  redirects, 3 MiB, 20 seconds; results cached 12 hours in the store; a
  failed or unrecognised answer is never cached and marks the evidence
  unavailable; GitHub 403/429 backs off 30 minutes (one `GithubBackoff`
  per key: the release-note fetches and Core's source back off apart).
  Cache entries not written for 30 days are pruned with the store
  ([storage.md](storage.md) S7).
  Parsers fail closed.
  The same fetch, narrowed to one host per request, also reads Home
  Assistant Core's own source for `opsctl core-compare`
  (`api.github.com` and `raw.githubusercontent.com` only,
  [capabilities.md](capabilities.md) K13); a caller can only narrow the
  hosts, never add one. Tests `tests/test_evidence.py`,
  `test_the_hardened_fetch_refuses_any_other_host`,
  `test_the_fetch_allows_only_the_two_hosts_and_never_another_on_redirect`.
- H20. `opsd/configcheck.py` is the fail-closed allowlist over proposed YAML
  (see [proposals.md](proposals.md)); it is not part of the health check.
