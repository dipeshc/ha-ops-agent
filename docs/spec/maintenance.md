# Proactive maintenance

Home Assistant announces deprecations and removals several releases ahead.
The agent notices the ones that affect this install and keeps the admin out
of the loop until a removal is close, or until a proposal or plan waits for
approval. Nothing fixes itself: the daemon executes only what the admin
approved, and the AI only proposes.

Implementation: `opsd/maintenance.py` (signals, findings, the scan, the
update-rule hook), `opsd/inventory.py`, `opsd/dashboards.py` (dashboard
reads, the edit, `DashboardWriter`), `opsd/evidence.py` (`parse_post`,
`EvidenceSource.posts`, `.archived`, `.core_platforms`), the
`maintenance` derivation in `opsd/items.py`, `opsd/approvals.py` (dashboard
approvals), `opsd/selfguard.py` (`approved_dashboard_write`), the
`maintenance_plan` run profile in `opsd/runner.py` with
`prompts/profiles/maintenance_plan.md` and
`prompts/maintenance-plan.schema.json`. Tests: `tests/test_maintenance.py`,
`tests/test_dashboards.py`.

## Signals

- M1. **Read-only.** A scan only reads: websocket `repairs/list_issues`,
  `system_log/list`, `config_entries/get`, `config/entity_registry/list`,
  `lovelace/dashboards/list`, `lovelace/config`, `lovelace/resources`; Core
  REST `/config`; Supervisor `/core/info` and `/addons`; the readable config
  files on disk; and the allowlisted, cached fetcher of `opsd.evidence`
  (`www.home-assistant.io`, `api.github.com`). Every fetched or local text
  is untrusted data: cleaned, length-capped, matched by name and never
  followed. Test `test_the_inventory_reads_what_the_install_uses`.
- M2. **Repairs** are the primary signal: an issue that is not ignored or
  dismissed, with a valid domain and issue id, becomes a deprecation when
  it has `breaks_in_ha_version` (the removal) or its id or translation key
  says deprecated, removed or removal. Test
  `test_repairs_logs_and_notes_become_deprecations_with_their_removal`.
- M3. **Core log** entries whose message says deprecated, will be removed,
  no longer supported or removed in become deprecations. A log finding
  stays `LOG_STICKY` (7 days) after it was last seen, since the log empties
  on a restart. Any integration can write a log line, so a log finding is a
  quiet, low-priority item: only repairs and release notes
  (`AUTHORITATIVE`) escalate (M12), push (M13) or hold a Core update back
  (M26). Exactly this log data reaches the item and the plan run:
  - the **domain**, only as `[a-z0-9_]{1,64}` from `custom integration
    '<domain>'` in the message or the `homeassistant.components.<d>` /
    `custom_components.<d>` logger name: in the title, the Affected fact
    and the daemon's plan steps (`about`);
  - the **removal**, only as the digits `YYYY.M` next to the removal words:
    in the title, the gist and the releases left;
  - whether the logger or message names a **custom integration**: only the
    choice of the daemon's plan-step wording and the Affected label;
  - the **message text**, cleaned, redacted and cut at 600 characters:
    only in the item's `agent` field ("The Core log says: …", source
    `Core log`), fenced as `<evidence>` in the plan prompt, and in the
    finding's evidence hash; never in a fact, the title, the gist, `about`,
    a need or a push.
  The title says the claim is the log's, "The Core log says <domain> is
  removed in <YYYY.M>" (or "is deprecated"); the gist starts "the Core log
  says" ("the Core log says removed in <YYYY.M>, <n> releases left", or
  "the Core log names no removal date"), and the Source fact is `Core log`.
  Tests `test_a_log_warning_lasts_a_week_after_it_was_last_seen`,
  `test_a_log_line_never_holds_an_update_back_escalates_or_pushes`,
  `test_only_deprecations_within_two_releases_need_the_admin_and_push`.
- M4. **Release posts** of the installed Core minor, the two before it and
  every newer published one, newest first and at most `MAX_STEPS` (6) posts
  per scan (far behind, the newest win): backward-incompatible items, the developer-blog
  list and the farewell list. An item counts only when its integration is in
  use (config entries, loaded components, YAML keys or platforms, custom
  integrations) or a code name it gives (with a `_ . : -` separator) is used
  in the readable YAML or as a dashboard card type. Its removal is the
  version the text names when that is newer than the installed one, else the
  post's release when that is not installed yet; anything else is not a
  finding. A post without a recognised backward-incompatible section is a
  problem, never a guess, and is never cached. Tests
  `test_repairs_logs_and_notes_become_deprecations_with_their_removal`,
  `test_the_post_parser_keeps_headings_and_new_integrations_and_fails_closed`,
  `test_an_unrecognised_post_is_a_problem_and_never_cached`.
- M5. **Weekly GitHub checks**, run by a scan when the last ones are at
  least 7 days old or a newer release post appears (no trigger forces
  them): a
  custom integration whose manifest's GitHub repository is archived becomes
  a deprecation without a removal date; a custom integration whose domain
  Core has (its `homeassistant/components/<domain>` listing holds a
  `manifest.json`), or that a read post adds under the same normalised name,
  becomes a replacement candidate. An answer without the expected shape is
  unknown, never a finding. Tests
  `test_the_weekly_checks_find_archived_repositories_and_replacements`,
  `test_github_answers_without_the_expected_shape_are_unknown`.
- M6. **Marketplace.** A read post with a heading naming a Marketplace,
  while HACS is installed, becomes one suggestion. Without HACS there is
  none. Test `test_without_hacs_there_is_no_marketplace_suggestion`.
- M7. **Missing evidence never resolves a finding.** When a signal could
  not be read in a scan (offline, refused, unrecognised, or the weekly
  checks not due), its earlier findings are kept unchanged. Test
  `test_missing_evidence_never_resolves_a_finding`.

## The inventory

- M8. The inventory holds the enabled config-entry domains, Core's loaded
  components (both halves of `<platform>.<integration>`), per readable YAML
  file its integration keys (`configuration.yaml` and `packages/` only) and
  its `platform:` names, the custom integrations (manifest name, version,
  GitHub repository, entity platforms and entity count here), the frontend
  plugins (dashboard resources), every dashboard's mode (`storage` only when
  Home Assistant lists it so, else `yaml`) and card types, and
  the installed add-ons. Test `test_the_inventory_reads_what_the_install_uses`,
  `test_yaml_features_name_integration_keys_and_platforms`.
- M9. **Readable files only.** The YAML read is the run copy's own list: the
  tracked readable files with git on, else `privsep.disk_files` (never
  secrets, `.storage/`, hidden paths, `www/` or links); each file at most
  256 KB, 8 MB in all, opened without following links. The text stays in
  memory for the scan and is never stored. A config that cannot be listed
  is a problem; the other signals still count. Test
  `test_the_inventory_reads_only_the_files_the_export_may_hold`.

## Findings and items

- M10. Each finding is one `maintenance` item, key `maint:<finding>`, id
  prefix `m`. Title, gist, need, facts and the numbered plan steps
  (`about`) are the daemon's words; the plan run's text is only in `agent`.
  Facts quote the evidence of repairs and release notes, redacted; a log
  line is only in `agent` (M3). Test
  `test_the_plan_is_the_agents_text_and_the_title_and_need_stay_the_daemons`.
- M11. Variants and their kicker: **Deprecation ahead** (what is affected,
  the removal version, the releases left, a plan), **Replacement candidate**
  (parity of entity platforms, entity-id impact, risk `low`/`medium`/`high`;
  never automatic) and **Suggestion**.
- M12. **Escalation.** A deprecation from a repair or the release notes
  is in Needs you, with an `ack`, only when its removal is at most `ESCALATE_RELEASES` (2) Core minors away and
  nothing else answers it (no proposal that is open or applying, no
  dashboard plan, no applied dashboard change, not hidden). A failed
  dashboard change to acknowledge takes precedence over all of these.
  OK hides it until one release closer (the ack token holds the releases
  left). Releases left count minors across years and are 0 once the removal
  is installed or due. Tests
  `test_only_deprecations_within_two_releases_need_the_admin_and_push`,
  `test_an_acknowledged_deprecation_asks_again_one_release_closer`,
  `test_releases_left_counts_core_minors_across_years`.
- M13. **Pushes.** A maintenance item pushes only when it enters Needs you:
  a deprecation within two releases, a dashboard plan waiting for approval,
  or a failed dashboard change to acknowledge (time-sensitive when the
  restore failed). Everything else is silent: deprecations further away are
  Coming up; replacement candidates and suggestions are a separate Home
  section, `suggestions`; all are in History.
- M14. **Resolution.** A finding the scan no longer reports is `done`
  ("resolved"). The admin may hide any finding ("Not relevant here"): it is
  `cancelled` until its evidence changes. Test
  `test_a_resolved_finding_is_done_and_a_hidden_one_leaves_home`.
- M15. Stored findings of the wrong shape (key not 16 hex characters,
  unknown variant) are dropped. Test
  `test_stored_findings_of_the_wrong_shape_are_dropped`.

## Plans and proposals

- M16. **The plan run.** For deprecations and replacement candidates
  that are not hidden and have no plan for their current evidence, at most
  4 per scan, one
  capability-free `maintenance_plan` run reads the findings as fenced
  evidence. An answer is kept only for a finding that was asked about, once
  per finding and evidence; AI off, capped or failed is retried at the next
  scan and never remembered.
- M17. **YAML migrations** become an ordinary one-tap proposal
  (`changes.build_proposal`, `requested_by = maintenance:<finding>`) only
  when git is on, the finding is a deprecation that names a readable YAML
  file, and the answer holds exact-text edits. The update fix's rules apply:
  `changepaths.fix_path`, the content rules, `configuration.yaml` only
  inside the block of the finding's integration. Otherwise the plan is text
  only. Test `test_yaml_edits_become_a_proposal_only_with_git_and_a_yaml_finding`.
- M18. **Dashboard migrations** are a plan only for a dashboard the finding
  itself names, never a YAML-mode, default or generated one. **Only core
  display-only cards with no tap actions are ever edited; every other card
  gets a text-only plan.** The edit is deterministic and narrow
  (`dashboards.Edit`): on every card of one type, rename option keys (at
  most 8, never `type`) and optionally change the card type; a key whose
  new name exists is left alone. It is executable only when:
  - its source type and any new type are in `DISPLAY_CARD_TYPES`
    (`history-graph`, `statistics-graph`, `logbook`, `calendar`, `map`,
    `clock`), compared exactly and case-sensitively, so never a `custom:`
    card;
  - every card of that type is plain (`editable`): no `card`, `cards` or
    `elements` key, no nested mapping that carries a `type`, and no action
    key (`action` or any `*_action`);
  - no such card is protected (`touches_protected`), the defence in depth
    over every key and value in the card: a `lock.`,
    `alarm_control_panel.` or `cover.` reference, also in the Jinja form
    `states.<domain>.<object>`; any text that the shared classification
    `opsd/access.py` `named_like_access` (the reload never-list's,
    [reload.md](reload.md)) calls access hardware, names and titles
    included; a `*`, a `/regex/` value or `entity_id: all`; or any of the
    filter and target keys `integration`, `device`, `area`, `label`,
    `attributes`, `domain`, `device_id`, `area_id`, `floor_id`,
    `label_id`. All of these fail closed;
  - no rename goes from or to `tap_action`, `hold_action`,
    `double_tap_action`, `icon_tap_action`, `icon_hold_action`,
    `icon_double_tap_action`, `confirmation`, `url`, `image`, `content`,
    `navigation_path`, `action`, `service`, `service_data`,
    `perform_action`, `data`, `target`, `entity`, `entities`, `card`,
    `cards`, `visibility` or `conditions` (`DENY_OPTIONS`).
  These are checked when the plan is made, at the approval request and
  again at execution, where a card that no longer qualifies voids the
  approval and nothing is written. The item shows the changed paths as its
  diff and waits for approval in act mode; in observe mode it shows the
  plan without the action. Tests
  `test_a_dashboard_plan_waits_for_approval_and_never_targets_another_dashboard`,
  `test_a_dashboard_not_named_by_the_finding_is_never_planned`,
  `test_parse_edit_refuses_anything_off_the_narrow_shape`,
  `test_only_a_display_card_type_is_ever_the_source_of_an_edit`,
  `test_a_display_card_that_holds_cards_or_actions_is_never_edited`,
  `test_every_string_of_a_display_card_is_checked_for_access_hardware`,
  `test_an_edit_never_touches_a_card_that_names_a_lock_alarm_or_cover`,
  `test_a_card_that_names_filters_or_targets_access_hardware_is_protected`,
  `test_the_health_check_reloads_and_dashboard_edits_share_one_access_classification`,
  `test_a_card_that_names_a_lock_after_the_approval_is_never_written`.
- M19. **Integration replacement** is always a plan the admin carries out;
  the daemon never removes or adds an integration.

## The dashboard capability

- M20. `dashboard.save` is a daemon-internal capability (tier APPROVE,
  effect `mutate_ha`, origin agent-self): never on the socket, the CLI, the
  guard or a prompt. Test `test_dashboard_save_is_an_approval_only_daemon_capability`.
- M21. **Approval.** A dashboard approval (`kind: dashboard`, subject
  `dashboard:<finding>`) binds the finding, the url_path, the SHA-256 of the
  dashboard JSON before and after, the edit, and the number and paths (at
  most 40) of the changes. It shares the pending cap of
  [approvals.md](approvals.md), and a new request voids any live one for the
  same subject. It is requested and
  confirmed by the admin's one panel request for the revision shown, by the
  admin who asked; it is recomputed from the dashboard as Home Assistant
  serves it at confirmation and again right before the save; single use;
  72-hour expiry. Update, change and reboot paths never accept it. Test
  `test_confirmation_needs_the_admin_who_asked_and_an_unchanged_dashboard`.
- M22. **Execution** (`DashboardWriter.execute`): refused unless approved,
  in act mode, not tightened to `never` by `capabilities.never`, and the
  dashboard is writable and listed by Home Assistant right now with mode
  `storage` (`mode_problem`, also checked when the admin requests the
  approval; any other mode, an unlisted dashboard or an unreadable list is
  refused with the reason); a dashboard that changed since the approval voids
  it and nothing is written; the current JSON is backed up to
  `<data>/maintenance/dashboards/<url_path>/` (directory 0700, file 0600,
  read back, the last 10 kept) before the approval is consumed; the save is
  read back, and any difference or error restores the backup the same way;
  a restore that does not read back is escalated with the backup's name.
  Tests `test_an_approved_plan_is_backed_up_saved_and_read_back`,
  `test_a_dashboard_that_changed_after_the_approval_is_never_written`,
  `test_a_failed_read_back_restores_the_backup`,
  `test_a_failed_save_and_a_failed_restore_escalate`,
  `test_observe_mode_and_a_never_option_refuse_even_an_approved_plan`,
  `test_a_dashboard_not_in_storage_mode_is_never_written`,
  `test_the_inventory_counts_only_listed_storage_dashboards_as_writable`,
  `test_the_panel_applies_an_approved_dashboard_plan_end_to_end`.
- M23. **Self-guard.** `lovelace/config/save` is refused unless it runs
  inside `selfguard.approved_dashboard_write(url_path, generated=...)` with
  exactly `{url_path, config}` and that url_path: one save per scope, closed
  when the block ends. The scope is never opened for the default dashboard,
  a url_path that is not lower case with a hyphen, or one listed in the
  profile's `dashboards.generated`. Every other `lovelace/...` write is
  refused; `lovelace/config` takes only `url_path`, and the dashboard list
  and resources take no arguments. Tests
  `test_the_save_is_refused_outside_an_approved_scope`,
  `test_the_scope_lets_exactly_one_save_of_its_own_dashboard_through`,
  `test_the_scope_is_never_opened_for_the_default_a_generated_or_a_malformed_dashboard`,
  `test_a_generated_dashboard_is_refused_even_with_a_forged_approval`.
- M24. **Profile.** `dashboards.generated` lists url_paths (lower case,
  with a hyphen; strict like every key) that something else writes; empty
  by default. Test `test_profile_key_dashboards_generated_is_strict`.

## Cadence and the update rule

- M25. A scan runs after every health cycle, whatever its trigger (local
  signals and the cached release index), with the GitHub checks of M5 when
  they are due, and before a deciding update plan (`window`,
  `manual`) when the last scan is older than 26 hours. A scan never raises:
  a failure keeps the earlier findings. Test
  `test_the_scan_is_due_before_an_update_only_when_it_is_old`.
- M26. **A Core update that removes something still in use asks.** An AUTO
  Core candidate whose target minor reaches the removal of an unresolved,
  not hidden deprecation from a repair or the release notes (never a log
  line) is held for approval with the reason naming it
  (`maintenance.removal_block`, beside `repair_block` in the update rule).
  Tests `test_a_core_update_that_reaches_a_removal_asks`,
  `test_a_pending_core_update_that_removes_something_in_use_asks`.

## Invariants

- M27. Locks, the alarm, garage doors, gates and anything else that opens
  the house are never operated. The plan prompt forbids edits that touch
  them and the proposal path cannot reach them. Dashboards: only core
  display-only cards with no tap actions are ever edited, and anything
  else is text-only (M18); such a card is still refused when anything in
  it names, matches or could select access hardware.
- M28. The daemon executes, the AI proposes; no maintenance path changes
  the agent itself, the Supervisor or the host; the hard gates (update
  rule, self-guard, approvals) are unchanged except for the additions above.
