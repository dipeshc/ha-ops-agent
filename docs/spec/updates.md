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
  target that reaches the removal of an unresolved, not hidden deprecation
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
  fix?}`, `summary`.
- U9. **Hold only.** The review can move an AUTO item to `approve` (when
  `hold` is true or risk is high) and never the reverse; a non-AUTO item
  only gets a note. An unassessed candidate is held "not assessed"; beyond 8
  candidates the rest are held "not reviewed yet". Tests
  `test_review_can_only_hold_updates_back`, `test_apply_review_never_lowers`.
- U10. With `updates.hold_if_unreviewed` (default true) a review that did not
  run (AI off, capped, failed) holds every AUTO item; false installs them
  unreviewed. Assessments are cached per entity and version and re-done when
  a fix is involved. Test `test_unreviewed_candidates_are_held_by_default`.

## Execution

- U11. **Gate.** Installs need valid options, `agent.mode: act`,
  `updates.auto_install: on`, no failed-night block and not paused;
  otherwise the run only reports. Executors additionally refuse the agent
  itself and the Supervisor, kinds outside core/addon/hacs/os/firmware, OS
  and firmware without a consumed and proven approval, and anything
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
  the backup at the same version. A changed update is not installed; a
  skipped approved job says so. Test `test_a_changed_update_is_not_installed`.
- U17. **Install.** Core `POST /core/update {version, backup: false}`;
  add-on `POST /store/addons/<slug>/update {backup: false}`; HACS
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
  applied. The update then waits for change `#N`: open or applying waits
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
