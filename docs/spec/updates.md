# Updates

Every night the agent plans the pending updates with a deterministic rule.
In `act` mode with `updates.auto_install: on` it installs the AUTO tier one
at a time in the window, after a backup, with a health diff and a rollback.
Everything else asks.

Implementation: `opsd/policy.py` (`classify`), `opsd/updates.py`
(`UpdateManager`), `opsd/updexec.py` (`Executors`), `opsd/updiff.py`
(probe and compare), `opsd/updatefix.py`, `opsd/backups.py`. Tests:
`tests/test_policy.py`, `tests/test_updates.py`, `tests/test_os_update.py`,
`tests/test_backups.py`, `tests/test_approvals.py`.

## The deterministic rule

- U1. Tiers: `auto`, `approve`, `never`, `pinned`, `manual`. `manual` is
  never offered (the agent itself, the Supervisor, an admin-skipped version,
  nothing newer, an install in progress); `pinned` is a profile pin; `never`
  is reported but never installed. Test `test_update_classify_is_one_resolver`.
- U2. Versions: dotted numerics only, a leading `v` stripped, dates and
  other suffixes refused. Bump: tuples padded to 3 parts; `new <= old` is
  NONE; first part differs MAJOR; second differs MINOR, but MAJOR for a
  `0.x` series; else PATCH; unparsable UNKNOWN. Tests `test_version_bump`,
  `test_parse_version_rejects_dates`.
- U3. Kind: the three Supervisor entities map to core, supervisor and os;
  device class firmware or a firmware platform (esphome, zha, matter,
  shelly, reolink, hue, unifiprotect, ...) is firmware; `hassio` add-on;
  `hacs` HACS; else other.
- U4. Rule order: NONE or self or skipped or in progress is `manual`; in
  `updates.never` is `never`. Core: a `.0` release (patch part 0) is `never`
  ("wait for a patch"); MAJOR or UNKNOWN or in `need_approval` is `approve`;
  else a soak candidate. OS and firmware are always `approve`; the
  Supervisor is `manual`. Then a fragile match from the profile (tier
  approve/pinned/never, optional `bumps` filter, matched by entity,
  repository or domain), `need_approval`, kind other, UNKNOWN bump, HACS
  plugins and themes (always `approve`), MAJOR, and a rollback problem (an
  add-on without a slug; a HACS repository without the specific-version
  feature or outside integration/plugin/theme) all give `approve`.
  Everything left is a soak candidate. Tests `test_core_rules`,
  `test_os_firmware_supervisor_and_self`, `test_addon_and_hacs_tiers`,
  `test_fragile_pieces_from_the_house_profile`,
  `test_hacs_frontend_plugins_always_need_approval`.
- U5. Soak: `max(7, updates.min_age_days)` days for a PATCH, twice that for
  a MINOR. Age unknown or younger than the soak is `approve` with
  `waits=True` (approvable early, shown as proposed, never pushed); else
  `auto`. Age comes from the GitHub release date for Core and HACS, else the
  number of days since the agent first saw the version pending. Tests
  `test_a_core_addon_minor_soaks_14_days_then_installs`,
  `test_every_kind_needs_a_known_age`, `test_first_seen_age_is_remembered`.
- U5a. **Soak-unknown hold.** A candidate whose release age is unknown
  (`age_unknown`) waits like a soaking one (proposed, never pushed,
  approvable early) and every nightly plan checks it again. It asks (an
  approval in Needs you) only once the agent has seen that version pending
  for 30 days (`UNKNOWN_AGE_ASK_DAYS`), or at once when a repair, a
  breaking change in Home Assistant's own Core notes or a removal still in
  use touches it (the U7 checks, run on it as on an AUTO candidate; Core
  notes that could not be read touch nothing here). Tests
  `test_an_unknown_release_age_waits_quietly_then_asks_after_30_days`,
  `test_an_unknown_release_age_asks_at_once_when_a_repair_touches_it`.
- U6. `updates.need_approval` is the union of the option and the profile;
  options only tighten. Test `test_option_schema_is_tighten_only`.

## Hard gates on AUTO candidates (plan time)

- U7. A held version (rolled back, escalated or failed before) is `approve`
  with `held=True`; a profile version gate holds Core at or past `core_from`
  as `never` until the profile changes (or the gate's `patch` is present or
  superseded, with the `localpatches` overlay); a repair that breaks in the
  target Core version, or raised by the HACS integration's domain, asks; a
  Core breaking or removed change for a domain the house uses (or with no
  domain), a developer-blog notice while a fragile custom domain is loaded,
  unreadable notes, or an empty house-domain set all ask; so does a Core
  target that reaches the removal of an unresolved deprecation (hidden or not)
  this install still uses, named by a repair or the release notes (never a
  log line) (`maintenance.removal_block`; a maintenance scan
  older than 26 hours runs first, [maintenance.md](maintenance.md)
  M25–M26). Tests
  `test_a_repair_that_breaks_in_the_target_core_version_asks`,
  `test_a_core_update_that_reaches_a_removal_asks`,
  `test_a_core_breaking_change_for_an_integration_the_house_uses_asks`,
  `test_a_core_wide_breaking_change_asks`,
  `test_unreadable_core_notes_or_house_integrations_ask`.

## The AI release-note review

- U8. Runs only on the nightly trigger when `updates.review` is true and
  there are AUTO or asking candidates; a refresh applies cached assessments;
  a manual plan runs none. The prompt holds each candidate's release notes
  fenced (4000 characters, redacted); the profile has no capabilities and a
  10-minute wait. Schema: `assessments[<=20]{entity_id, risk, hold, reason,
  fix?}`, `summary`. A candidate with a `<needs_change>` block (U23) is
  reviewed in a second run of its own, whose prompt holds only Home
  Assistant's own Core notes and that block; `fix` is read only from that
  run's answer, never from the run that holds third-party notes. Test
  `test_a_config_change_is_written_only_by_its_own_run_over_home_assistants_notes`.
- U9. **Hold only.** The review can move an AUTO item to `approve` (when
  `hold` is true or risk is high) and never the reverse; a non-AUTO item
  only gets a note. An unassessed candidate is held "not assessed"; beyond 8
  candidates the rest are held "not reviewed yet". Tests
  `test_review_can_only_hold_updates_back`, `test_apply_review_never_lowers`.
- U10. With `updates.hold_if_unreviewed` (default true) a review that did not
  run (AI off, capped, failed, or `updates.review` false) holds every AUTO
  item that has no earlier assessment of that version; false installs them
  unreviewed. Assessments are cached per entity and version and re-done when
  a fix is involved. Tests `test_unreviewed_candidates_are_held_by_default`,
  `test_with_the_review_off_unreviewed_candidates_are_still_held`.

## Execution

- U11. **Gate.** Installs need valid options, `agent.mode: act`,
  `updates.auto_install: on`, no failed-night block and not paused;
  otherwise the run only reports. Executors additionally refuse the agent
  itself and the Supervisor, kinds outside core/addon/hacs/os/firmware, OS
  and firmware without a consumed and proven approval, a `hassio`-platform
  (Supervisor-managed) entity through `update.install`, and anything
  `capabilities.never` names. Tests
  `test_observe_mode_plans_and_reports_without_backup_or_install`,
  `test_auto_updates_option_gates_execution`,
  `test_executors_follow_the_update_rule_and_hard_limits`.
- U12. **Window.** Daily at `updates.window`, once per window day, at most
  90 minutes, shortened to 20 minutes before the next automatic backup (45
  minutes when the schedule is unreadable). Nothing starts while a
  Supervisor update/backup/restore job or the Core backup manager is busy
  (waits up to 20 minutes); a busy or late abort is quiet. Tests
  `test_nothing_starts_too_close_to_the_automatic_backup`,
  `test_never_overlaps_a_supervisor_job_or_the_automatic_backup`.
- U13. **Order.** One job at a time: add-ons, HACS plugins, HACS
  integrations, Core, firmware, OS. Test
  `test_act_mode_backs_up_then_installs_one_at_a_time`.
- U14. **Backup.** Exactly one per run, before the first job and never per
  job, through the websocket
  `backup/generate_with_automatic_settings` (the user's own automatic
  settings; the backup configuration itself is never read). The daemon
  waits up to 40 minutes for a new backup that is automatic, holds the
  config and database, is protected (encrypted) and includes every installed
  add-on; otherwise the run aborts before any install and the failure is an
  item. Tests `test_backups_must_be_encrypted_and_complete`,
  `test_no_code_reads_the_backup_config`, `test_backup_failure_aborts_before_any_install`.
- U15. **Baseline.** A pre-run probe (core state, states, registry, config
  entries, repairs, watched entities) must have no errors with Core running;
  an unreadable health fails closed. Test `test_unreadable_health_fails_closed`.
- U16. **Pre-check per job.** The gate again, the job's budget against the
  deadline (core 30, add-on and HACS 15, firmware 20, OS 40 minutes), quiet,
  still AUTO in the plan or a proven approval, the update entity still `on`
  with the same installed and latest versions, not in progress, an add-on in
  the backup at the same version and the Supervisor's `version_latest`
  (`/addons/<slug>/info`) equal to the planned version. A changed update is
  not installed; a skipped approved job says so. Tests
  `test_a_changed_update_is_not_installed`,
  `test_an_addon_installs_only_when_the_supervisor_offers_the_planned_version`.
- U17. **Install.** Core `POST /core/update {version, backup: false}`;
  add-on `POST /store/addons/<slug>/update {backup: false}`, which takes no
  version, so the executor reads `/addons/<slug>/info` again right before
  the call and refuses unless `version` and `version_latest` are still the
  planned ones (the job is then `skipped`, nothing called; test
  `test_an_addon_store_version_that_moves_right_before_the_call_is_not_installed`); HACS
  `update.install` with the version; firmware `update.install` (version only
  with the specific-version feature); OS `POST /os/update {version}` inside
  the approved OS flow. A timeout or dropped connection is "unknown" and is
  verified, never retried. Polling every 15 seconds until the version
  matches; install timeouts core 35, add-on 20, HACS 15, firmware 25, OS 45
  minutes. A HACS integration install runs `/core/check` then
  `/core/restart`; a failed check rolls back without restarting. Tests
  `test_live_ops_use_the_native_endpoints`,
  `test_an_unknown_outcome_is_verified_not_retried`,
  `test_config_check_failure_after_a_hacs_install_rolls_back`.
- U18. **Verify.** Settle at least 2 and at most 10 minutes, then compare
  with the pre-run probe. A regression is: probe errors; Core not running or
  API down; a custom integration gone, more failed entries, more unavailable
  or all entities gone; a new failed config entry; one or more newly
  unavailable entities in lock, alarm_control_panel, climate, cover, fan,
  light, switch, water_heater, valve or siren, or 5 or more elsewhere; a new
  error or critical repair (HACS `restart_required` ignored); a watched
  entity that was up and is now unavailable, unknown or missing; an add-on
  that was started and is not. Healthy at the target is success and becomes
  the next job's baseline; healthy at the old version is a failure. Tests
  `test_compare_regressions_and_notes`, `test_settles_before_declaring_success`.
- U19. **Rollback.** Core: reinstall the previous version only when the
  recorder schema version is readable and unchanged, else escalate. HACS:
  install the previous version (and restart Core if the new code loaded).
  Add-ons, OS and firmware MUST NOT be rolled back by the agent: a
  regression (a watched entity included) escalates to the admin, naming
  the backup to restore (the add-on restore would need the backup key the
  agent never has). One rollback attempt per job; after it
  the health must settle healthy or the job is `rollback_failed`. Tests
  `test_core_and_hacs_roll_back_by_version`,
  `test_core_is_not_downgraded_over_a_migrated_database`,
  `test_an_addon_regression_is_escalated_held_and_pauses_updates`.
- U20. **Escalation.** A failed rollback or an escalation holds the version,
  skips the remaining jobs, pauses updates with a failed-night block and
  pushes a Needs-you item naming the backup to restore (safety budget). A
  successful rollback holds the version and pushes, without pausing. A
  regression at the end of the night against the first baseline pauses too.
  Tests `test_failed_rollback_stops_everything`,
  `test_a_slow_regression_across_the_night_is_escalated`.
- U21. **Resume after a restart.** An active run resumes: a backup in
  progress is re-verified or the run aborts; a job that was installing and
  is still at the old version is `failed` ("not retried"); one at the new
  version is verified. Tests `test_resume_never_reinstalls_an_unconfirmed_install`,
  `test_resume_verifies_an_install_that_finished`.

## Holds

- U22. A version hold (`update_hold:<entity>@<version>`) is set on a
  rollback, a failed rollback, an escalation, an OS fallback after reboot
  and a reconcile regression. It makes that exact version `approve` with
  `held=True`; a newer version is unaffected. Approving that version in the
  panel is the only way to release a hold: there is no separate "clear"
  action, no code path deletes a hold key, and the hold MUST persist across
  restarts and plans until that approval. Other holds: a pause
  (panel, or set by a failure until the panel resumes), the review hold, the
  unreviewed hold, repair and breaking-change holds (re-evaluated each
  plan), version gates, fragile pinned/never, `updates.never`, an admin
  rejection (not asked again for 7 days), the soak wait, and the one-tap fix
  wait.

## The one-tap fix proposal

- U23. For a Core AUTO candidate held by a repair or a breaking change, with
  git on and no open fix, the review may answer `fix{why, edits}`: at most 6
  edits, each `{path, old, new}` with `old` non-empty and unique in the file,
  at most 2000 characters, at most 3 files, paths limited to
  `configuration.yaml`, `automations/**`, `alerts/*`, `templates/**.yaml`,
  `custom_templates/**.jinja` (never docs or self paths), all proposal rules
  applied. In `configuration.yaml`, every mapping of a changed block that
  the old block does not hold verbatim passes the proposal call allowlist:
  literal allowed actions, literal targets in the entity registry, no
  templated `action`, `service`, `entity_id` or `target`, no device, area,
  event or scene keys. A macro (`.jinja`) changes only when no `*.yaml` in
  the config directory picks an action or its target with a template, or
  does more than a proposal may ([proposals.md](proposals.md) C5) while
  holding any template (conditions and triggers included), and every one
  of them parses (fail closed). The scan skips hidden directories and
  `secrets.yaml`, walks at most 20 000 directory entries and 3000 YAML
  files, opens each file without following a link, and reads only a
  regular file of at most 1 MiB; anything else refuses the macro change.
  The same rules apply to a maintenance YAML migration
  ([maintenance.md](maintenance.md) M17). Tests
  `test_an_updates_configuration_change_is_checked_like_a_proposal`,
  `test_a_macro_changes_only_when_no_file_picks_an_action_with_a_template`,
  `test_a_macro_change_counts_any_templated_file_that_does_more`,
  `test_the_macro_scan_never_follows_links_or_opens_special_files`.
  The update then waits for change `#N`: open or applying waits
  quietly, applied lets it go ahead (re-reviewed), rejected asks again.
  Nothing is applied automatically; the admin applies the proposal on its
  item ([proposals.md](proposals.md)). Tests
  `test_a_held_core_update_waits_for_its_one_tap_config_change_then_installs`,
  `test_a_held_core_update_without_a_fix_asks_and_a_rejected_fix_asks_again`.

## Standalone backups

- U24. "Back up now" (`POST api/backups/now`) and the self-heal use the same
  executor, verifier and 40-minute wait as U14; `updates.auto_install` does
  not gate them and the admin's request works in `observe` mode. It is
  refused while one runs, after 3 standalone starts in a day, when a
  complete backup is under 3 hours old, or within 1 hour of the last start.
  Success is not pushed; failure is a Needs-you item. Tests
  `test_back_up_now_works_in_observe_mode_and_is_verified`,
  `test_at_most_three_standalone_backups_a_day`.
- U25. Self-heal: in `act` mode only, when Core has an automatic schedule,
  the newest complete backup is older than 26 hours (or none), not tried in
  the last 24 hours and under the daily cap. Chat can only ask for a backup.
  Tests `test_self_heal_is_due_only_when_everything_says_so`,
  `test_observe_mode_never_heals`, `test_chat_can_ask_for_a_backup_but_never_runs_one`.

## Fork rebases

A custom integration that forks a Core one (`custom_components/<domain>`
with the domain of a Core integration) can be rebased onto the installed
Core version. The AI only proposes it ([proposals.md](proposals.md) C11);
the update pipeline applies it after the admin's approval.
Implementation: `opsd/forkrebase.py`, `UpdateManager._approved_rebases`
and `_fork_step` in `opsd/updates.py`, `Executors.fork_apply`,
`fork_check`, `fork_restore`, `fork_restart` in `opsd/updexec.py`. Tests
`tests/test_forkrebase.py`.

- U26. **When.** An approved fork rebase ([approvals.md](approvals.md)
  A18) runs in the update window after the night's plan and installs and
  before an approved reboot, or at once when the admin chose "now" (the
  `approved` trigger). It needs the same gate as installs (U11: valid
  options, act mode, `updates.auto_install: on`, not paused, no failed
  night), its 25-minute budget inside the window (U12), and a quiet
  Supervisor; otherwise it stays approved and waits until its approval
  expires. It gets an update run of its own (trigger `fork_rebase`, the
  latest plan copied with nothing to execute) with one job: kind
  `fork_rebase`, entity `fork.<domain>`, from the fork's origin tag to the
  Core version, naming the proposal and the approval.
- U27. **Consume, then back up.** The job's creation consumes the approval
  (single use) only while its binding, recomputed from the files on disk
  and the live Core version, still matches; otherwise the job is
  `skipped` and the proposal `stale`. Then the encrypted backup of U14 and
  the baseline of U15; when either fails the job is `skipped` and the
  proposal is open again ("approve it again").
- U28. **Steps.** `pending` → `installing`: the executor refuses unless the
  job consumed the approval and it still binds; it snapshots the files
  the proposal replaces (bytes, mode and owner, root-only under
  `/data/fork-rebase/<id>/old/`), then writes the approved bytes (each
  checked against its recorded SHA-256). Every read and write of the
  fork goes through one descriptor of `custom_components/<domain>`,
  reached from the config directory by opening `custom_components` and
  then `<domain>`, each with `O_DIRECTORY | O_NOFOLLOW`: a link at either
  level is refused, and a directory swapped for a link after it was
  opened does not redirect anything (the writes land in the directory
  that was opened). Inside it, each target must be a regular file
  (`stat` without following links); the bytes go to a new temporary file
  with a random name (`.ops-<16 hex>.tmp`, `O_CREAT | O_EXCL | O_NOFOLLOW`
  relative to the descriptor), written in full, given the old file's mode
  and owner, fsynced and renamed over the old file relative to the same
  descriptor, then the directory is fsynced; a failed write removes its
  temporary file, and a leftover temporary file never blocks a write.
  Then Core's config check (`POST /core/check`); a failure puts the old
  files back (Core has not loaded the new ones) and the job `failed`. Then
  one Core restart inside `approved_core_restart(<job>)`
  ([selfguard.md](selfguard.md) G9), only when the files on disk are
  exactly the approved result; then `verifying`. A refused or failed
  restart, or Core not back within 15 minutes, is `rolling_back`; a
  refused write whose files on disk still match the proposal's "before"
  hashes is `failed` ("nothing was written"). A job found `installing`
  after a daemon restart is `rolling_back` (what is on disk is unknown).
- U29. **Verify.** The settle and comparison of U18 (the access-hardware
  rules included: a watched lock or the alarm that goes down is a
  regression) plus: more unavailable entities of the fork's domain than
  before is a regression. Healthy: `succeeded`, the proposal `applied`,
  then the files git already tracks under `custom_components/<domain>/`
  are committed with the plumbing (`commit_files`, `chore(custom-components):
  rebase <domain> onto core <version>`) and pushed when `limits.git_push`
  is on; untracked files, or git off, are not committed (said on the
  item). A commit failure never undoes the rebase.
- U30. **Rollback.** With no snapshot and the fork's files on disk
  matching the proposal's "before" hashes, nothing was written and Core
  was not restarted: the job is `failed` ("nothing was written"), never
  `rollback_failed`. Otherwise `rolling_back` restores the snapshot (only from a
  snapshot whose files match their own SHA-256s), restarts Core inside a
  new scope for the same job, and settles again: healthy is
  `rolled_back` and the proposal `rolled_back`; a failed restore or
  restart, or health still worse, is `rollback_failed`, which pauses
  automatic updates (U20) and keeps the snapshot. Every outcome but
  success puts the proposal's item in Needs you with an `ack` need and
  the item book pushes it (`rollback_failed` urgent).
- U31. **Never otherwise.** A `fork_rebase` job is never installed or
  rolled back by the update executors (`_check_job` refuses its kind and
  entity), and the fork executors refuse any other job, an entity other
  than `fork.<domain>` for the job's own domain, a missing proposal id, or
  a domain that names the agent (`self_path`) or is on the never-reload
  list or the profile's `reload.deny`. `fork.apply` is
  daemon-internal ([capabilities.md](capabilities.md) K3), tier APPROVE,
  and `capabilities.never` can refuse it.
- U32. **Never access hardware.** A fork rebase is refused for a domain on
  the never-reload list (`policy.NEVER_RELOAD_DOMAINS`: locks, the alarm,
  access, radios, core plumbing) or the profile's `reload.deny`, and for
  any domain that provides access hardware: an entity of its platform or
  its config entries (enabled or not) that `reload` counts as opening the
  house (a lock, an alarm panel, a cover not positively a blind, shade,
  curtain, awning or shutter, or a switch, button, valve or cover named
  like access). The lists are checked when it is proposed, when the admin
  approves it and by the executor; the live access check (Core's config
  entries, registry and states) when it is proposed and approved, and an
  unreadable answer refuses. Tests
  `test_access_hardware_and_never_reload_domains_are_never_rebased`,
  `test_the_executor_refuses_a_never_reload_domain`.
- U33. **Never stuck.** At start, before each health check and every
  minute (never while a pipeline runs), a proposal left `applying` whose
  job is not in an active update run is settled from its job: `succeeded`
  is `applied`; `rolled_back` is `rolled_back`; `failed` and
  `rollback_failed` are `failed`; `skipped`, or a job that never started
  with the fork's files untouched, is `open` again with "the approval was
  used without installing, approve it again" and an activity entry that
  needs the admin (the item is back in Needs you, and pushed); anything
  else is `failed` ("stopped before anything was written" when the files
  are untouched, otherwise "stopped mid-way; check custom_components/
  <domain> against the backup"), with an `ack` need. A job in an active
  run is left to `resume`. Test
  `test_a_rebase_left_applying_outside_a_run_is_settled`.

