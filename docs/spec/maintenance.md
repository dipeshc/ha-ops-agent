# Proactive maintenance

Home Assistant announces deprecations and removals several releases ahead.
The agent notices the ones that affect this install and keeps the admin out
of the loop until a removal is close, or until a proposal or plan waits for
approval. Nothing fixes itself: the daemon executes only what the admin
approved, and the AI only proposes.

Implementation: `opsd/maintenance.py` (signals, findings, the scan, the
update-rule hook), `opsd/inventory.py` (also the read-only dashboard
reads), `opsd/evidence.py` (`parse_post`,
`EvidenceSource.posts`, `.archived`, `.core_platforms`), the
maintenance composition in `opsd/items.py`, the
`maintenance_plan` run profile in `opsd/runner.py` with
`prompts/profiles/maintenance_plan.md` and
`prompts/maintenance-plan.schema.json`. Tests: `tests/test_maintenance.py`.

## Signals

- M1. **Read-only.** A scan only reads: websocket `repairs/list_issues`,
  `system_log/list`, `config_entries/get`, `config/entity_registry/list`,
  `lovelace/dashboards/list`, `lovelace/config`; Core REST `/config`;
  Supervisor `/core/info`; the readable config
  files on disk; and the allowlisted, cached fetcher of `opsd.evidence`
  (`www.home-assistant.io`, `api.github.com`). Every fetched or local text
  is untrusted data: cleaned, length-capped, matched by name and never
  followed. Tests `test_the_inventory_reads_what_the_install_uses`,
  `test_dashboards_are_only_read_through_the_self_guard`.
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
  (`AUTHORITATIVE`) escalate (M11), push (M12) or hold a Core update back
  (M20). Exactly this log data reaches the item and the plan run:
  - the **domain**, only as `[a-z0-9_]{1,64}`, of the integration the line
    is about: the integration Core's own usage report names (`Detected
    that [custom ]integration '<d>'`, `<CONSTANT> was used from <d>`),
    honoured only when Core code logged it (a `homeassistant.` logger),
    else the one whose `homeassistant.components.<d>` /
    `custom_components.<d>` logger logged it. Never a word naming Home
    Assistant or its parts (`core`, `homeassistant`, `home_assistant`,
    `integration`, `component`, `helpers`, `none` and the like,
    `NOT_A_DOMAIN`, in any letter case). A
    line with no reliable domain is dropped (a debug log line records it).
    A log finding without a usable domain (empty, None, or such a word) is
    never emitted, never loaded from the store, never planned and never titled or worded with a stand-in such
    as "Core" or "None" (`Finding.sound`, `usable_domain`). The domain is used in the title, the Affected fact and
    the daemon's plan steps (`about`);
  - the **deprecated thing**, only when the message names it in a safely
    code-like form (an identifier, a dotted name or a call of at most 80
    characters with an underscore, a dot, a capital or `()`: in backticks,
    before `was used from`, after `calls`/`uses`/`accesses`/… or before
    `is deprecated`): in the title, in backticks;
  - the **removal**, only as the digits `YYYY.M` next to the removal words:
    in the title, the gist and the releases left;
  - whether the logger or message names a **custom integration**: only the
    choice of the daemon's plan-step wording and the Affected label;
  - the **message text**, cleaned, redacted and cut at 600 characters:
    only in the item's `agent` field ("The Core log says: …", source
    `Core log`), fenced as `<evidence>` in the plan prompt, and in the
    finding's evidence hash; never in a fact, the title, the gist, `about`,
    a need or a push.
  The title says the claim is the log's and that what is removed is
  something the integration uses, not the integration: "The Core log says
  <domain> uses `<thing>`, which Home Assistant removes in <YYYY.M>", or
  "The Core log says <domain> uses something Home Assistant removes in
  <YYYY.M>" without a code-like thing ("… has deprecated" without a
  removal). The gist starts "the Core log says" ("the Core log says
  removed in <YYYY.M>, <n> releases left", or "the Core log names no
  removal date"), and the Source fact is `Core log`.
  Tests `test_a_log_warning_lasts_a_week_after_it_was_last_seen`,
  `test_a_log_line_never_holds_an_update_back_escalates_or_pushes`,
  `test_only_deprecations_within_30_days_of_their_deadline_need_the_admin_and_push`,
  `test_a_log_removal_names_the_integration_that_uses_it_never_core`,
  `test_log_domain_comes_only_from_a_reliable_source`,
  `test_the_deprecated_thing_is_quoted_only_when_it_is_safely_code_like`,
  `test_an_earlier_core_titled_log_finding_is_dropped`,
  `test_core_deprecation_lines_never_give_core_or_none_as_the_domain`,
  `test_a_log_finding_without_a_usable_domain_is_never_loaded`,
  `test_a_scan_never_plans_with_a_missing_domain`.
- M3a. **A log finding resolves early when the integration changed.** Each
  log finding records, when its warning is seen, a fingerprint of the
  integration's code as the inventory sees it (`detail.code`): for a custom
  integration its manifest version and a hash of its source files (the same
  reads as M13c), for a Core one the Core version. A later scan resolves it
  at once, with the reason "resolved: <domain> changed since the warning
  (…) and the warning has not come back", only when the warning is absent
  from the Core log just read **and** the fingerprint now differs from the
  recorded one. Everything else keeps `LOG_STICKY`: unchanged code, a warning
  still in the log, a finding stored without a fingerprint. Missing evidence
  never resolves it (M6): a log that was not a list, an inventory with
  problems, or code that cannot be read. Tests
  `test_a_log_finding_resolves_at_once_when_the_code_changed_and_the_warning_is_gone`,
  `test_a_log_finding_stays_while_the_code_is_unchanged`,
  `test_a_log_finding_stays_while_the_warning_is_still_in_the_log`,
  `test_a_log_finding_stays_when_the_log_or_the_inventory_is_unavailable`,
  `test_a_log_finding_stored_without_a_fingerprint_keeps_the_seven_day_rule`.
- M4. **Release posts** of the installed Core minor, the two before it and
  every newer published one, newest first and at most `MAX_STEPS` (6) posts
  per scan (far behind, the newest win): backward-incompatible items, the developer-blog
  list and the farewell list. Only items of a post that is not installed yet,
  or that name a removal in a newer release, can be findings. An item counts
  only when it is **relevant to this install** (M4a). The site's glossary
  tooltips (`<span class="terminology-tooltip">`: "Groups are a way to
  organize ...", with a link to the glossary's integration) are stripped
  before anything is read, and a note's integration is its own
  documentation link (else the last integration link it gives), never a
  word or link of a tooltip. A parsed post is cached under a key that
  carries the parser's version (`evidence.POST_PARSER`), so a parser fix
  takes effect at the next scan, not when the cache expires (test
  `test_a_parser_fix_takes_effect_over_a_cached_post`). A post without a
  recognised backward-incompatible section is a problem, never a guess, and
  is never cached. Tests `test_glossary_tooltips_never_reach_a_note_or_its_domain`,
  `test_repairs_logs_and_notes_become_deprecations_with_their_removal`,
  `test_the_post_parser_keeps_new_integrations_and_fails_closed`,
  `test_an_unrecognised_post_is_a_problem_and_never_cached`.
- M4a. **Relevance is deterministic and precise.** An item is relevant only
  when:
  - its integration is **configured** here (`Inventory.configured`: an
    enabled config entry, a top-level YAML integration key or a `platform:`
    name, or a custom integration). Core's loaded components alone never
    count: the entity-platform half of `<platform>.<integration>`
    (`climate`, `event`, `group`, `sensor`, `device_tracker`, ...) is loaded
    on every install;
  - or a code name it gives (with a `_ . : -` separator, at least 4
    characters) is used in the readable YAML **and** names a configured
    integration itself (`<domain>.<x>` or `<domain>:<x>`), or the item's own
    integration is configured;
  - or a code name it gives is a dashboard card type in use.
  Generic option names (`entity_id`, `unique_id`, `device_class`, `name`,
  `friendly_name`, `unit_of_measurement`, `state_class`, `device_id`,
  `area_id`, `value_template` and the like, `GENERIC_TERMS`) never count as
  a reference, so a note for an integration that is not configured is never
  a finding because `entity_id` appears in the YAML. Tests
  `test_a_note_for_an_unconfigured_integration_is_no_finding`,
  `test_entity_platform_domains_alone_never_make_a_finding`.
- M4b. **Only a named removal has a deadline.** A release-notes item is a
  removal (its own finding, `named_removal`, with a deadline, M11) only when
  it is a farewell (`removed`) item of a release not installed yet, or its
  text names the removal version next to the removal words and that version
  is newer than the installed one. Every other relevant breaking change or
  developer-blog notice of a release not installed yet is a fact on **one
  finding per Core minor** ("Core <minor>: breaking changes for <domains>",
  gist "changes with <minor>", never "removed in"), which has no deadline
  and never enters Needs you on its own (M13e). A breaking change of an
  installed release is no finding. Tests
  `test_a_breaking_change_without_a_named_removal_has_no_deadline`,
  `test_a_breaking_change_gist_never_says_removed`,
  `test_one_core_release_is_one_item`.
- M5. **Weekly GitHub checks**, run by a scan when the last ones are at
  least 7 days old or a newer release post appears (no trigger forces
  them): a
  custom integration whose manifest's GitHub repository is archived becomes
  a deprecation without a removal date; a custom integration whose domain
  Core has (its `homeassistant/components/<domain>` listing holds a
  `manifest.json`), or that a read post adds under the same normalised name,
  becomes a replacement candidate. An answer without the expected shape is
  unknown, never a finding. A candidate of the custom integration's own
  domain is checked against the installed Core version, as `core-compare
  <domain> --fork` shows it (`forkrebase.fork_state`, no AI): when the
  fork still carries its own changes (hunks against its origin release,
  the manifest's `version` aside, new files included) that Core's text of
  the file lacks, the candidate is settled and its item resolves as "Fork
  still needed: <summary>" (the daemon's summary, e.g. `fan.py: 2 changes;
  presets.py: new file`). The fingerprint (installed Core minor, the
  fork's file hashes) is kept in `maintenance:fork:<domain>`; while it
  holds no GitHub question is asked, and the candidate comes back only
  when the Core minor or a fork file changes and the fork no longer
  carries changes Core lacks. When GitHub or the fork cannot be read the
  candidate stays as it is, and a stored reason for a fork whose fingerprint
  has changed is cleared. The answer is believed only when at least
  `MIN_SIMILARITY` (60%) of the fork's Python lines match its origin release;
  below that the candidate stays as it is. New files count only when they are
  Python files (a README, `hacs.json` or translations do not). Tests
  `test_the_weekly_checks_find_archived_repositories_and_replacements`,
  `test_github_answers_without_the_expected_shape_are_unknown`,
  `test_a_fork_that_is_still_needed_settles_its_replacement_candidate`,
  `test_an_unreadable_fork_leaves_the_candidate_as_it_is`,
  `test_a_fork_with_changes_core_lacks_is_still_needed`,
  `test_a_fork_whose_changes_core_took_over_is_not_needed`,
  `test_a_fork_far_from_every_release_is_not_judged`,
  `test_only_code_files_of_its_own_make_a_fork_needed`.
- M6. **Missing evidence never resolves a finding.** When a signal could
  not be read in a scan (offline, refused, unrecognised, or the weekly
  checks not due), its earlier findings are kept unchanged. Test
  `test_missing_evidence_never_resolves_a_finding`.

## The inventory

- M7. The inventory holds the enabled config-entry domains, Core's loaded
  components (both halves of `<platform>.<integration>`), per readable YAML
  file its integration keys (`configuration.yaml` and `packages/` only) and
  its `platform:` names, the custom integrations (manifest name, version,
  GitHub repository, entity platforms and entity count here) and every
  dashboard's card types. **Configured** (`configured`, M4a; also the update
  rule's house integrations, [updates.md](updates.md) U7) is the config-entry
  domains, the YAML integration keys and platforms and the custom
  integrations, never the loaded components alone. A `platform:` line in an
  automation, script or blueprint file names a trigger (`event`, `state`),
  never an integration. Test
  `test_the_inventory_reads_what_the_install_uses`,
  `test_yaml_features_name_integration_keys_and_platforms`,
  `test_card_types_count_nested_cards_but_not_views_or_sections`.
- M8. **Readable files only.** The YAML read is the run copy's own list: the
  tracked readable files with git on, else `privsep.disk_files` (never
  secrets, `.storage/`, hidden paths, `www/` or links); each file at most
  256 KB, 8 MB in all, opened without following links. The text stays in
  memory for the scan and is never stored. A config that cannot be listed
  is a problem; the other signals still count. Test
  `test_the_inventory_reads_only_the_files_the_export_may_hold`.

## Findings and items

- M9. Each finding is one `maintenance` item, key `maint:<finding>`, id
  prefix `m`. Title, gist, need, facts and the numbered plan steps
  (`about`) are the daemon's words; the plan run's text is only in `agent`.
  Facts quote the evidence of repairs and release notes, redacted; a log
  line is only in `agent` (M3). Test
  `test_the_plan_is_the_agents_text_and_the_title_and_need_stay_the_daemons`.
- M10. Variants and their kicker: **Deprecation ahead** (what is affected,
  the removal version, the releases left, a plan), **Replacement candidate**
  (parity of entity platforms, entity-id impact, risk `low`/`medium`/`high`;
  never automatic).
- M11. **Escalation: 30 days before the deadline.** A finding's deadline
  is the first day of the month of its removal release (`deadline`; Core
  minors ship in the first week of their month), and only a deprecation
  from a repair with a removal version, or a named removal of the release
  notes (M4b), has one. Such
  a deprecation is in Needs you, with an `ack`, only from `ASK_BEFORE` (30
  days) before its deadline, or once its removal is installed, and only
  when nothing else answers it (no proposal that is open or applying, not
  hidden). Until then it
  is Coming up and silent, with `source.recheck_at` set to the day it may
  ask, so the minute tick moves it then. A finding without a deadline (a
  log line, an archived repository, a deprecation without a removal
  version, a replacement candidate) never enters Needs you,
  except a repair or release-notes finding the agent's check says needs the
  admin, naming what the admin must do (M13d); never a Core-minor finding
  (M13e). A verdict decides only within these bounds (M13d). Its
  `ack` (OK) hides it until one release closer (the ack token holds the releases
  left). Releases left count minors across years and are 0 once the
  removal is installed or due. Tests
  `test_only_deprecations_within_30_days_of_their_deadline_need_the_admin_and_push`,
  `test_a_deadline_is_the_month_of_the_removal_and_only_authoritative_removals_have_one`,
  `test_an_acknowledged_deprecation_asks_again_one_release_closer`,
  `test_releases_left_counts_core_minors_across_years`.
- M11a. **One-tap fixes wait with their finding.** A config change
  proposed for a finding (`requested_by = maintenance:<finding>`) that is
  open is `proposed` (Coming up) with `apply` and `reject` as plain actions
  and its expiry in the footnote, until its finding may ask (M11); then it
  is `waiting` with its approval need like any proposal. It expires no
  earlier than a week after its finding may ask (`fix_expiry`: the later
  of now and `asks_at()`, plus `PROPOSAL_TTL`), or after 90 days
  (`FIX_READY`) for a finding without a deadline, so it stays ready in
  Coming up; files that change meanwhile make it stale as always.
  Maintenance fixes have their own cap of 5 open, apart from other
  proposals. Tests `test_a_fix_stays_ready_until_its_finding_asks`,
  `test_a_maintenance_fix_keeps_its_later_expiry_and_its_own_open_cap`. A change whose
  finding has no deadline, or is gone, never asks. Applying from Coming up is the same approval as from
  Needs you. A scan recomposes the open changes of its findings. Test
  `test_a_findings_config_change_moves_its_item`.
- M12. **Pushes.** A maintenance item can push only when it enters Needs
  you (a deprecation within 30 days of its deadline, a `needs_owner`
  verdict with its owner action, never a Core-minor finding), and then only as the push policy
  says ([items-and-pushes.md](items-and-pushes.md) N1): a deadline within 7
  days pushes on its own, anything else goes into the
  daily digest. It is never pushed again while it waits, and its push is
  withdrawn when it leaves Needs you. Everything else is silent:
  deprecations further away are Coming up; replacement candidates are a
  separate Home section, `suggestions`; all are in
  History. Tests `test_needs_you_pushes_only_urgent_or_daily_digest`,
  `test_a_resolved_item_clears_its_push`.
- M13. **Resolution.** A finding the scan no longer reports is `done`
  ("resolved: the latest maintenance scan no longer finds it"), or with the
  scan's own reason when it dropped the finding on purpose: "resolved:
  <domain> is not configured here" or "resolved: nothing configured here is
  affected" (`maintenance:gone:<finding>`). A stored release-notes finding
  is re-checked against M4a at every scan, also when the posts cannot be
  read (its stored domain and affected places are enough). Test
  `test_a_stored_finding_whose_integration_is_not_configured_resolves_also_offline`. The
  admin may hide a finding that does not ask ("Hide
  until it changes"; never offered while it asks): its item is `cancelled`
  until its evidence changes or it may ask (M11), when it comes back.
  Hiding only quiets the item: a hidden removal still counts for the
  update rule (`removals`, M20). Hiding and plans are bound to the
  finding's **fingerprint** (`Finding.evidence`): its variant, signal,
  domain, removal, the change ids or repair it carries, the files and
  dashboards affected (never line numbers) and where the change lives
  (`yaml` or `dashboard`; only a `yaml` one can be proposed); editing an unrelated line of a file it names changes nothing.
  Tests
  `test_a_hidden_removal_still_holds_core_back_and_asks_near_its_deadline`,
  `test_a_resolved_finding_is_done_and_a_hidden_one_leaves_home`,
  `test_an_unrelated_edit_keeps_the_fingerprint`.
- M13a. **Every finding is actionable.** Besides its conversation (as on
  every item, [panel.md](panel.md) N13), a finding offers **Plan a fix**
  (an investigation on the finding itself that reads the evidence and
  writes a plan:
  the steps, their risk, what the agent can and cannot do; where the fix
  is inside the proposal rules it ends in an ordinary proposal for
  approval; where it is not, for example a custom integration's own code,
  it says that the admin or an engineering session must do it;
  [chat.md](chat.md) T17a), **Remind me** in 30 or 90 days or near the
  deadline (30 days before it, only for a finding with a deadline more
  than 31 days away; an admin reminder task about the item, opening it
  when it fires), and **Hide until it changes** (M13, only while it does
  not ask). Tests
  `test_plan_a_fix_runs_on_the_finding_itself`,
  `test_remind_me_later_sets_a_reminder_about_the_item`.
- M13b. **The plan lives on the finding.** "Plan a fix" makes no item of
  its own. While it runs (its task pending or running) the finding is
  Running, without a need (unless the finding asks by itself, below), gist "planning a fix · step N of 2" (but
  while a cap holds a step it is Scheduled instead, without Stop, as
  [chat.md](chat.md) T17a says), with the phases "Read the evidence" and "Write the plan" (current
  the steps done, `started_at` the start, usually 4 minutes), a text that
  says it cannot change anything, and the one action **Stop** (`stop`,
  which cancels the pending step or kills the running one); a hidden
  finding shows it too. When it is done, the last step's findings (the
  model's words, 3000 characters, with the run's sources) are the finding's
  `agent` text. When it stopped, the gist adds "the plan could not finish:
  <why>" (daemon words) and Plan a fix reads **Retry the plan**. A fork
  rebase or a config change that one of its steps proposed is folded into
  the finding while it is open, approved, running or asks for an
  acknowledgement: the finding takes the proposal's state, gist, need,
  actions, phase, `change` and footnote, adds the proposal's facts, keeps
  its plan as `agent`, and its actions run as the proposal's own (bound to
  the finding's revision and the `change.shown` hash it shows;
  [panel.md](panel.md) N19); an acknowledgement is stored under the
  proposal's key, so both items resolve. Only the finding's latest plan
  counts: a proposal of an older plan is an ordinary item. The finding
  carries the newest such proposal (fork rebases first, then config
  changes); while it does,
  that proposal's own item has no need or action and never pushes. A
  plan-made proposal's own item always follows the finding and is quiet
  (off Home, in History); once it is rejected, expired, applied or done,
  or another proposal is the finding's, it acts for itself again: an
  applied change offers its own **Revert** on that item, and on that item
  only. It is composed again whenever the finding is (a finding the scan
  no longer lists carries nothing). Hiding a finding never splits it from
  its proposal: a finding that carries a proposal or runs a plan is shown
  even when hidden. **A finding that asks by itself carries nothing:** a
  finding with its own need (an acknowledgement) or urgency keeps its need, state, urgency and actions while a
  plan runs (its gist adds the plan's progress and Stop is offered), and
  folds no proposal: the proposal keeps its own need, urgency, actions
  and pushes on its own item (which follows the finding, not quiet), and
  the finding only names it in a fact ("Proposed by the plan"), with none
  of its actions and no link, so each action reaches its own record.
  Rejected, expired or done, the finding is composed as before. Tests
  `test_a_proposal_the_plan_made_is_decided_on_the_finding`,
  `test_a_plan_made_change_offers_its_own_revert_once_applied`,
  `test_a_finding_that_asks_keeps_its_question_and_carries_nothing`,
  `test_an_urgent_change_failure_asks_on_its_own_item_while_the_finding_asks`,
  `test_a_hidden_finding_with_a_proposal_is_still_one_item`,
  `test_a_config_change_the_plan_made_is_applied_from_the_finding`,
  `test_a_plan_that_stopped_says_why_and_offers_a_retry`,
  `test_the_finding_rejects_the_rebase_its_plan_proposed`,
  `test_stop_on_a_running_plan_cancels_it`.
- M13c. **A fix settles its findings at once.** When a fork rebase job
  ends `succeeded`, or a config change proposed for a finding is
  `applied`, `MaintenanceManager.settle(domain)` reads the live repairs and
  log again and replaces that domain's repair and log findings with what
  they report now (a log line no longer there is not kept for
  `LOG_STICKY`); a replacement candidate of the domain is checked as in M5.
  Other domains and signals, and the weekly checks, are untouched. It
  never raises, and it reads the stored findings strictly (`load(strict=True)`):
  a failed read keeps every earlier finding and plan, never replaces them with
  a partial list. A scan and a settle hold one lock from their read to their
  write, so neither overwrites the other and `_prune` never deletes a plan
  made meanwhile. A log line the restart emptied from the log keeps its finding
  while the custom integration's current source still names its deprecated
  thing as a whole identifier, until that is gone from the source or
  `LOG_STICKY` has passed. The daemon runs it as a task of its own, off the
  update job's lane. Tests `test_a_successful_fix_settles_that_domains_findings_at_once`,
  `test_a_fork_is_checked_again_right_after_a_rebase`,
  `test_a_failed_read_of_the_findings_never_wipes_other_domains`,
  `test_a_settle_and_a_scan_never_interleave`,
  `test_a_log_finding_stays_while_the_forks_source_still_uses_the_thing`,
  `test_a_log_finding_is_dropped_when_the_source_is_unreadable_or_the_week_is_over`,
  `test_an_old_fork_reason_is_cleared_when_a_changed_fork_cannot_be_judged`,
  `test_the_pipeline_happy_path`,
  `test_an_applied_change_is_reported_and_a_push_moves_the_tracking_ref`.
- M13d. **Verdicts: the agent checks before it asks.** A repair or
  release-notes deprecation (a verification candidate) is checked by the
  agent before it may enter Needs you: the plan run (M15) or Plan a fix
  ([chat.md](chat.md) T17a) answers, per finding, a verdict `not_affected`,
  `fixable`, `needs_owner` or `unknown` with a reason and the evidence it
  read. `needs_owner` is only for something the admin must do or decide
  and carries `owner_action`: a short imperative naming it ("Set the
  workday country in its options", at most 160 characters). "Nothing
  here needs a change" is `not_affected`; a check it could not complete
  is `unknown`, never `needs_owner`. A `needs_owner` without a usable
  `owner_action` (missing, blank, under 8 characters, or "nothing", "none",
  "no action/change/fix needed") is kept as `unknown` (normal unknown
  handling: it never asks on its own), also when an older stored verdict
  has none. The daemon keeps it under the finding's cause
  (`verdict:maint:<finding>`, `opsd.verdicts`: verdict, reason, evidence,
  owner action, fingerprint, when, by) until the fingerprint changes; a changed
  fingerprint is a new check. Every composer honours it:
  - no verdict yet, while a check is still possible: the finding does not
    ask; its gist adds "checking whether it affects this install";
  - `not_affected`: the finding is `done`, "checked: not affected here:
    <reason>", also while a Plan a fix waits for it; a Plan a fix only
    queued for it (a cap holds it, nothing runs) is cancelled, the item's
    history says "Plan a fix cancelled: checked: not affected here, nothing
    to plan" (test
    `test_a_not_affected_verdict_closes_a_finding_whose_plan_waits`); in History with the reason under The agent says and a
    **Reopen** action (`reopen`: the admin's own verdict, the finding
    behaves as without a verdict until its fingerprint changes);
  - `needs_owner`: it asks as M11 says (a finding without a deadline asks
    now, except a Core-minor finding, M13e), with the agent's reason under
    The agent says. The need is daemon words around the owner action,
    which is redacted, fenced (angle brackets neutralised, cut at 160
    characters) and quoted as the agent's words: 'The agent says you need
    to: "<owner action>". Its reasons are under The agent says. OK hides
    this until its evidence changes.';
  - `fixable`: the proposal path (M16); without a proposal it behaves
    as without a verdict;
  - `unknown`, a check that failed (AI off, capped, failed run), or no
    answer for the finding in two runs: it behaves as without verdicts
    (fail safe).
  - A check that could not run keeps the run's own reason on its verdict
    (`why`, `detail`) and the finding's gist says so in daemon words,
    composed when shown: "checking whether it affects this install: the
    agent's check waits for its daily allowance (next try <local time>)",
    or "AI is off, ...", "... waits for the AI provider's usage limit to
    lift ...", "... failed and will run again by itself ...". It is the
    agent's own check, so it never says "Plan a fix", "failed (error)" or
    "retry when you like", and asks the admin to do nothing: the next scan
    retries by itself (a day later). Tests
    `test_a_capped_check_names_the_cap_and_retries_by_itself`,
    `test_a_check_that_could_not_run_says_the_real_reason`.
  **Guards** (`verdicts.closes`, in code): a verdict never closes a critical
  item, access hardware, a lock or the alarm (domains `lock`,
  `alarm_control_panel` and anything `access.named_like_access` calls access
  hardware), `core:down`, a `custom:` item, or a rollback or escalation. A repair Home Assistant still lists is only quieted
  (`proposed`, "checked: not affected here: <reason>; Home Assistant still
  lists the repair"), never closed; so is a finding whose deadline is within
  14 days, unless a deterministic check agrees (its integration is not
  configured). The removal hold on a Core update (M20) never reads a
  verdict. Tests
  `test_a_not_affected_plan_closes_the_finding_and_is_remembered`,
  `test_a_changed_evidence_fingerprint_reopens_a_remembered_verdict`,
  `test_a_verdict_never_closes_a_critical_or_access_item`,
  `test_a_needs_owner_verdict_without_an_owner_action_is_unknown`,
  `test_a_needs_owner_verdict_shows_its_owner_action_in_the_need`,
  `test_a_failed_check_asks_as_before`,
  `test_the_live_case_makes_no_needs_and_no_push`.
- M13e. **A Core minor's breaking changes are facts for its update.** A
  Core-minor finding (M4b) never enters Needs you and never pushes on its
  own, whatever its verdict: while no Core update into that minor is
  pending it stays in Coming up, a `needs_owner` or `unknown` verdict
  only keeping its reason (and owner action) visible under The agent
  says; `not_affected` closes it as M13d says. While a Core update into
  that minor (or later) is pending, the finding follows the update's
  item (`follows`, quiet, "shown on the Core update to <version>"), whose
  facts list the notes and whose The agent says adds the finding's
  checked verdict, so the update's review and approval are where the
  admin sees them (items-and-pushes.md I19). Named removals with a
  deadline (M4b, M11) keep their deadline-driven asking and M20. Tests
  `test_a_needs_owner_core_minor_stays_in_coming_up`,
  `test_a_core_minors_verdict_is_shown_on_its_core_update`,
  `test_a_no_fix_needed_plan_never_lands_in_needs_you`.
- M14. Stored findings of the wrong shape (key not 16 hex characters,
  unknown variant) are dropped. Test
  `test_stored_findings_of_the_wrong_shape_are_dropped`.

## Plans and proposals

- M15. **The plan run.** For deprecations and replacement candidates
  that are not hidden and have no plan for their current evidence, and for
  verification candidates without a verdict for their fingerprint (M13d),
  at most `MAX_PLAN_ITEMS` (8) per scan, one
  `maintenance_plan` run reads the findings as fenced evidence: the daemon
  matched them, and the run first checks whether each really affects this
  install, then plans. It has the read capabilities for that (entities
  with their platform, unit, device class and a light's effect via
  `opsctl entities --platform`, config entries with their non-secret
  settings ([capabilities.md](capabilities.md) K12a), repairs, logs) and the
  config in its working directory; a Core-minor finding's notes are listed
  one per line and decided one by one, and `unknown` is only for a note
  nothing it can read decides. A note whose integration is not configured
  never reaches it (M4a). Test `test_the_check_gets_what_decides_each_note`. Each answer
  carries `verdict`, `reason`, `evidence` (what it read) and
  `owner_action` (M13d; null unless `needs_owner`); an answer for
  a finding that was not asked about is dropped. Its
  capabilities are the read `ha.core_compare` (`opsctl core-compare
  <domain> [--fork]`, [capabilities.md](capabilities.md) K13), which it
  uses for a finding about a custom integration that shadows or forks a
  Core one and cites as fact, and `fork.propose`
  ([proposals.md](proposals.md) C11): a fork rebase the admin approves,
  only for a domain with a finding from a repair or the release notes
  (`AUTHORITATIVE`); the daemon refuses it for one known only from a log
  line, which any integration can write. Chat and investigations (the
  admin asked) may propose one whatever the signal.
  "Plan a fix" on such a finding is an investigation with the same two;
  a rebase its run proposes is decided on the finding (M13b). An answer is kept only for a finding that was asked about, once
  per finding and evidence; AI off, capped or failed is retried at the next
  scan and never remembered.
- M16. **YAML migrations** become an ordinary one-tap proposal
  (`changes.build_proposal`, `requested_by = maintenance:<finding>`) only
  when git is on, the finding is a deprecation that names a readable YAML
  file, and the answer holds exact-text edits. The update fix's rules apply:
  `changepaths.fix_path`, the content rules, `configuration.yaml` only
  inside the block of the finding's integration. Otherwise the plan is text
  only. Test `test_yaml_edits_become_a_proposal_only_with_git_and_a_yaml_finding`.
- M17. **Dashboards are never edited.** A finding that affects a dashboard
  card gets a text-only plan (the dashboard and card, what to change); the
  admin makes the change by hand. The plan answer has no dashboard edit,
  and the agent has no capability that writes a dashboard
  ([selfguard.md](selfguard.md) G8).
- M18. **Integration replacement** is always a plan the admin carries out;
  the daemon never removes or adds an integration.

## Cadence and the update rule

- M19. A scan runs after every health cycle, whatever its trigger (local
  signals and the cached release index), with the GitHub checks of M5 when
  they are due, and before a deciding update plan (`window`,
  `manual`) when the last scan is older than 26 hours. A scan never raises:
  a failure keeps the earlier findings. Test
  `test_the_scan_is_due_before_an_update_only_when_it_is_old`.
- M20. **A Core update that removes something still in use asks.** An AUTO
  Core candidate whose target minor reaches the removal of an unresolved
  deprecation (hidden or not, whatever its verdict) with a deadline: a
  repair with a removal version or a named removal of the release notes
  (never a log line, never a Core-minor finding of plain breaking changes)
  is held for approval with the reason naming it
  (`maintenance.removal_block`, beside `repair_block` in the update rule).
  Tests `test_a_core_update_that_reaches_a_removal_asks`,
  `test_a_pending_core_update_that_removes_something_in_use_asks`.

## Invariants

- M21. Locks, the alarm, garage doors, gates and anything else that opens
  the house are never operated. The plan prompt forbids edits that touch
  them and the proposal path cannot reach them. Dashboards are only read
  (M17).
- M22. The daemon executes, the AI proposes; no maintenance path changes
  the agent itself, the Supervisor or the host; the hard gates (update
  rule, self-guard, approvals) are unchanged except for the additions above.
