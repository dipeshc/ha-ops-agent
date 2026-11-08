# Items, the state machine and the push policy

Everything the agent does is an item on the panel's Home page, with its
history. The daemon writes each item from its own records at the moment the
record changes; the panel never infers state. The wire shape is in
[panel.md](panel.md).

Implementation: `opsd/items.py` (`ItemBook`, `Composer`), the escalation
gate `opsd/escalation.py` (I22), the transaction observer in
`opsd/store.py`, `opsd/activity.py`, `opsd/notify.py`,
`opsd/channels.py`, `opsd/quiet.py`, `opsd/live.py`.
Tests: `tests/test_items.py`, `tests/test_escalation.py`, `tests/test_needs_and_watches.py`,
`tests/test_notify_triage.py`, `tests/test_quiet.py`, `tests/test_chat_ux.py`.

## Vocabulary

- I1. Kinds (a store CHECK): `update`, `reminder`, `investigation`,
  `question` (key `ask:`, I20), `proposal`, `check`, `backup`, `reload`,
  `window`, `maintenance`. States: `proposed`,
  `waiting`, `scheduled`, `running`, `done`, `failed`, `cancelled`,
  `expired`; the last four are terminal.
- I2. Need kinds: `approval` and `ack`. Every need also carries `why`, one
  reason of the closed list in I22, and `because`, the daemon's words for
  it. Actors: `you`,
  `daemon`, `agent`. Action ids match `[a-z_]{1,32}`; item ids match
  `[a-z]-[0-9a-z]{1,40}`.
- I3. **Needs you** is exactly `state == waiting`, or `failed` with a need.
  The store enforces it: `need` is non-null iff `waiting`, or the state is
  `failed`. Test `test_the_store_refuses_a_waiting_item_without_a_need`.
  What may have a need is decided by one gate only (I22).
- I4. Keys and ids: keys are `updates:<entity>@<version>`, `window:<run>`,
  `change:<id>`, `fact:<id>`, `task:<id>`, `health:<key>`, `maint:<finding>`,
  `activity:<id>`, `fork:<proposal>`, and `ask:<token>` (a
  question). The URL id is
  `<prefix>-<sha256(key)[:12]>` with prefixes `u` (updates), `w` (window),
  `p` (change, fact, fork), `r` (task), `c` (health), `m` (maintenance), `q`
  (questions), `x` (other);
  activity items are `a-<n>`, numbered by `kv` `activity.seq` so a number is
  never reused after a prune.

## Written at the source

- I5. **Items are written at their source.** Every store write records what
  it touched: an update job or run, an approval, a change proposal, a house
  fact, a task, an item written directly (I16), or a `kv` key among the
  update flags (paused, a failed night and its acknowledgement, a failed
  backup or check before a night), the item acknowledgements
  (`item.ack:<key>`), the maintenance state (`maintenance:`) and the
  verdicts (`verdict:`, [maintenance.md](maintenance.md) M13d). Before that
  transaction commits, the item book composes the items those records show
  and writes each one, with its event, in the same transaction: a job
  writes its update's item and its night's, an update or reboot approval its
  update's item, a run (a new plan, a night's progress) and an update flag
  every update item and night, a change proposed for a maintenance finding
  also that finding's item,
  a change, a fact or a task its own item, the maintenance state the
  maintenance items. The health check's save of its
  open items writes the health items in one transaction, and a maintenance
  scan writes its findings, its metadata and the records it blanks in one,
  so its items are composed once from its whole result (test
  `test_a_scan_writes_its_items_once`). An item and its
  source therefore never disagree, even after a crash. A failure to compose
  or write one item is logged and blocks neither the source's write nor the
  other items. Tests `test_an_item_is_written_in_its_sources_transaction`,
  `test_one_source_that_cannot_be_composed_leaves_the_others`,
  `test_a_findings_config_change_moves_its_item`,
  `test_an_item_that_cannot_be_composed_never_blocks_its_source`,
  `test_one_bad_item_does_not_stop_the_others`.
- I5a. **Nothing re-derives.** A group that lists its items resolves the ones
  it no longer lists, with its own reason: the health check `done`
  "cleared: not reported by the latest health check", the maintenance scan
  `done` "resolved: the latest maintenance scan no longer finds it", the
  update plan `cancelled` "not offered now: <reason>", "superseded: Home
  Assistant now offers <version>" or "Home Assistant no longer lists it as
  pending" (never "installed": that is the job's own item). A house fact
  resolves with its decision (`cancelled` "rejected", `done` "applied", or
  `cancelled` "proposed as config change #<id>"). Every other item changes
  only when its source does: a reminder done weeks ago stays done, the
  oldest of many open changes stays waiting. A health `log:<fp>` item that
  leaves because its fingerprint joined the baseline is not called cleared:
  "not new any more: this error is part of the baseline now; it is reported
  again if it grows tenfold"; a maintenance finding resolves with the
  scan's own reason when it has one ([maintenance.md](maintenance.md) M13).
  Tests `test_a_resolved_condition_resolves_its_item`,
  `test_a_new_log_error_is_not_cleared_while_it_continues`,
  `test_an_old_record_never_turns_into_cancelled`,
  `test_a_house_fact_item_resolves_with_its_decision`.
- I5b. **Time.** An item whose words depend on the clock carries
  `source.recheck_at`: a health item whose `needs_after` is ahead (I10), an
  update rejected within the last 7 days (I6), a maintenance finding or its
  config change whose deadline is more than 30 days away ([maintenance.md](maintenance.md)
  M11, M11a). The minute tick
  ([overview.md](overview.md) D2) recomposes such an item (health, update,
  maintenance and change items) once that time passes and forgets a recheck its source did not renew (a resolved item
  carries none), then decides the pushes time made due (N1). The add-on options
  are a source too: at each start the daemon recomposes the update items
  and nights (the gate and the window), the maintenance items (act mode),
  and the open or asking change and fact items (so a need an earlier
  version composed follows this version's rule, I22). Tests
  `test_a_need_an_earlier_version_composed_is_recomposed_at_start`,
  `test_the_items_engine_asks_only_once_remote_access_stays_down`,
  `test_a_rejected_update_asks_again_once_the_week_is_over`,
  `test_a_cleared_watch_is_not_composed_again`.
- I6. Update items: AUTO and not held is `scheduled` (or `proposed` while a
  gate blocks the window); approved is `scheduled` with a `withdraw` action
  and a footnote naming the approval, versions and plan; an APPROVE item
  still soaking is `proposed` (actions approve, approve_now, reject) and is
  never pushed; otherwise `waiting` with an `approval` need (primary
  `approve` "Approve for tonight", secondary `approve_now` except for OS,
  and `reject`). A running job is `running` with phases; success is `done`;
  installed-but-reboot-pending is `waiting` with primary `reboot_window`
  (never `failed`); rolled back, rollback-failed, an OS update that did not
  activate after its reboot, or a failed install that nothing retries by
  itself is `failed` with an `ack` need, urgent for the two rollback
  outcomes and the OS fallback, and hidden once acked. A failed install is
  retried by itself, and so is no question (the item follows the plan
  again, its gist saying "not installed last time"), only when all hold
  (`Composer._retried`): it failed from `pending` or `installing` (recorded
  in the job's `target.failed_from`; failing from verifying, rolling back
  or an unknown state asks the first time), nobody approved it, the latest
  plan still offers that version as an automatic update, automatic updates
  run (no gate, not paused), and the entity has failed fewer than
  `REPEATED_FAILURES` (2) times in the 14 days of jobs read, any version.
  An approved job that was skipped while Home Assistant no longer offers
  the version is `cancelled` without a need (still offered, the update
  asks for approval again). Tests
  `test_a_failed_install_asks_only_when_it_failed_twice`,
  `test_a_silent_night_does_not_swallow_a_later_needs_you_push`.
  A rolled-back version the latest plan still offers asks no separate
  acknowledgement: the rollback is a fact ("Rolled back") on the held
  version's approval, which is the one question; a rolled-back version the
  plan no longer offers is `failed` without a need (History). Rollback
  failed and the OS fallback still ask with an urgent `ack`. Test
  `test_a_rollback_is_a_fact_on_the_held_versions_approval`.
- I7. Phases: `[Backup, Install, Reboot, Verify]` for OS, else
  `[Backup, Install, Verify]`; `usual_s` is the median of succeeded jobs of
  that kind, else 600 (core), 240 (add-on), 120 (HACS), 600 (firmware),
  900 (OS).
- I8. Window items exist only in `act` mode; done and aborted nights are
  `quiet`; a failed night is `failed` with an urgent `ack` unless a job
  already escalated. A night aborted by a failure asks until a later night
  in act mode finishes `done`: that superseding success resolves it ("a later
  night ran normally"), without an acknowledgement. Test
  `test_a_later_good_night_resolves_an_aborted_night`.
- I9. Proposal items: `open` is `waiting` (`apply` / `reject`, with the row's
  expiry), except a maintenance finding's change before its finding may ask,
  which is `proposed` with the same actions ([maintenance.md](maintenance.md)
  M11a); approved is `running`; applied is `done` with a `revert` action
  (on its own item, also for a change a finding's plan proposed,
  [maintenance.md](maintenance.md) M13b);
  failed is `failed` without a need when the daemon undid it cleanly (the
  files restored and, after a reload, reloaded again); a failure that may
  have left the house half applied (the restore or its reload failed, an
  error stopped the pipeline, a restart interrupted it; kv
  `change.unclean:<id>`) is `failed` with an urgent `ack` need and is
  pushed;
  an open change whose diff cannot be shown in full is `proposed` with
  `reject` only (it cannot be applied and expires by itself); rejected or
  stale is `cancelled`; expired is `expired`; refused rows produce no item.
  Tests `test_a_failed_change_is_one_failed_item_that_does_not_ask`,
  `test_a_change_not_shown_in_full_cannot_be_approved`,
  `test_a_change_that_may_be_half_applied_asks_and_a_clean_failure_does_not`,
  `test_an_error_that_stops_the_pipeline_never_leaves_a_change_applying`. The proposer's words go in
  `agent`, never in the need or the push. Test
  `test_a_change_proposal_keeps_the_proposers_words_out_of_the_need_and_the_push`.
- I10. A fact noted without a change proposal is `done` ("kept in the
  agent's store, not proposed as a change"), never in Needs you; its later
  decision still resolves it (I5a). Task items are
  `scheduled` (investigations `running`) with `cancel` or `stop`. Health
  items carry `snooze` (7 days) and `accept` (30 days) and are not pushed
  by the item book (the health check pushes its own critical items, H17),
  except what asks as `grounded_needs_owner`, `stuck` or `physical`, which
  goes out as N1 says. A health item asks only for a reason of I22:
  - critical: `waiting` with an urgent `ack` (`critical`), whatever any
    verdict says; a grounded `needs_owner` only adds its owner action to
    the need text;
  - a warning with a `needs_owner` verdict that is grounded (H16a): `waiting`
    with an `ack` quoting the owner action (`grounded_needs_owner`);
  - a warning HA itself says needs hands (H16c), or the vacuums overlay's
    room drift: `waiting` with an `ack` (`physical`); the drift is pushed
    on its own (`push_now`, push title "Ops Agent: vacuum rooms changed",
    the drift as its gist; triage never sees it);
  - a warning still reported and undecided `STUCK_DAYS` (7) after it
    opened, or past its own watch window (`needs_after`): `waiting` with an
    `ack` (`stuck`), its text saying how long it has been undecided and why;
  - every other warning (no verdict, `unknown`, a check that could not run,
    an ungrounded or hedged answer): `proposed` in Coming up, never a need,
    its gist the state in daemon words ("could not decide: <why> (next try
    <when>)", "watching; asks you if it lasts past <time>") and, while it
    is open, the day it would become `stuck`;
  - a Core-log error with no observable effect (H16b): `proposed`, "no
    observable effect", a quiet fact with its count; never triaged;
  - info: `proposed` without a need. Test
    `test_an_info_health_item_is_shown_and_never_asks`.
  A health item acked
  since the check ran (a baseline ack, which keeps the item it acked: by
  the daemon for a triage verdict, or by the admin) is `done` without a
  need, actor `agent` or `you`, with a gist that says who acked it, until
  when and why ([health.md](health.md) H13a); it never enters Needs you.
  One item per cause: a `repair:` item whose repair a maintenance finding
  carries follows that finding (no need, "tracked on its maintenance
  item"); a `backup:run_failed` item follows the failed backup's own item
  while that one asks. A triage verdict ([health.md](health.md) H16a) puts
  the agent's reason under The agent says; `not_affected` or `fixed`
  on an item that is not guarded snoozes it for at most 7 days (H16a), so
  it shows resolved by the agent. Every log-derived item carries the
  effect check as a fact ("Effect: none observed", "Effect: 3 entities
  unavailable since the baseline: …", H16b). Tests `test_health_items_wait_and_clear`,
  `test_a_deprecation_repair_is_one_item`,
  `test_a_triage_verdict_anchors_the_reason_on_its_item`,
  `test_the_items_engine_asks_only_once_remote_access_stays_down`,
  `test_a_not_affected_verdict_snoozes_a_warning_for_at_most_a_week`.
- I11. Follows: the host-reboot health item follows the OS update whose job
  rebooted the host, and the `reboot_required` repair follows the OS update
  pending its reboot; the follower has no need and resolves with its parent.
- I12. **Revision.** The material fields (kind, title, state, gist, kicker,
  ref, need, about, actions, phase, follows, agent, change, footnote, quiet)
  decide the revision: unchanged material updates only `as_of`, source and
  facts silently; any change bumps the revision by one and writes one event.
  `needs_since` persists while the item stays in Needs you; `resolved_at`
  while the state is unchanged. Tests `test_as_of_and_bindings_never_move_the_revision`,
  `test_a_new_health_check_does_not_move_the_revision`.
- I13. `agent.text` is the only model-written field, set from the release-note
  review, a proposal's why/effect, or agent-text activity kinds (`notice`,
  `followup`, `investigation`). Model text never titles a need or a push.
  Tests `test_the_review_reason_stays_out_of_the_need`,
  `test_an_agent_written_entry_never_titles_a_need_or_a_push`.
- I14. Until a plan made after the current process start exists, plan-only
  update items neither enter nor leave Needs you and are not pushed. Tests
  `test_a_plan_from_before_the_start_never_flashes_needs_you`,
  `test_an_update_already_waiting_before_the_start_stays_and_is_not_pushed_again`.
- I15. Every item written publishes, once its transaction commits (on the
  daemon's loop, whichever thread wrote it), a live `item {id, revision,
  state, needs_count}` event; a write that rolled back publishes nothing.
  `needs_count` is one SQL count and the same number on `api/home`,
  `api/history`, `api/state` and the status sensor. Test
  `test_an_item_written_from_a_worker_thread_is_pushed_on_the_loop`.
- I16. Activity entries are items too (there is no separate log): with an
  item key they become events of that item, otherwise standalone
  `activity:<n>` items, whose push is decided at the write like any other's. Quiet kinds
  (`check`, `suppressed`, `updates`, `reminder`, `investigation`, `note`)
  fold in History and never show on Home unless they need the admin; an
  entry that needs the admin is a failure of something the daemon did (a
  backup, a reload) and becomes `failed` (or `waiting`) with an `ack`
  (`failed`, I22). No entry
  posts into a conversation: the agent's own notes are items and History
  entries only. A config change's outcome is an event on its item. Tests `test_activity_entries_become_items_and_events`,
  `test_a_standalone_entry_is_pushed_when_it_is_written`. A failed backup's
  item stops asking when a later backup succeeds ("resolved: a later
  backup succeeded"). Test `test_a_later_good_backup_resolves_a_failed_one`.
- I17. Home sections (questions are on Ask and never on Home; a quiet
  item that follows another, such as a proposal folded into its finding,
  is shown on that item and not as a row of its own, unless it needs the
  admin): `needs` oldest `needs_since` first; `running`;
  `coming` (proposed, scheduled) by `at`; `suggestions` (proposed
  maintenance replacement candidates) by `at`; `today` (terminal, not quiet,
  resolved since local midnight) newest first. History: 50 per page, keyset
  cursor on `(at, id)`, filters `all`, `needs`, `changes` (proposal, update,
  reload), `checks` (check, backup); day grouping and folding of quiet items
  is the panel's job. Test `test_history_pages_newest_first`.
- I18. Resolved items not in Needs you, their events and their
  conversations' turns are deleted after 90 days.

## Maintenance items

- I19. Maintenance items are written from the stored findings, their plans
  and their verdicts at each change of the maintenance
  state or a `verdict:` record (I5); the composition, escalation, verdicts,
  pushes and actions are in [maintenance.md](maintenance.md) M9–M17 and
  the wire shape in [panel.md](panel.md) N12. Only entering Needs you can
  push, and only as N1 says (a deadline within 7 days on its own, anything else in the daily digest); every other
  maintenance item is silent. A config change proposed for a finding
  follows its finding's deadline (maintenance.md M11a). A Core-minor
  finding (maintenance.md M4b) never asks or pushes on its own
  (maintenance.md M13e): it is Coming up, and follows the pending Core
  update into that minor while there is one: the update's item lists the
  notes as facts, adds the finding's checked verdict under The agent says,
  and is the one item.

## Questions

- I20. A question ([chat.md](chat.md) T1a) is an item with key
  `ask:<token>`, id `q-…`, kicker "Question", the admin's text as its
  title, Running / Done / Failed as its conversation stands, never a need
  and never pushed by the book; History's `questions` filter lists it.
  Test `test_a_question_is_an_item_that_opens_at_once`.

## Fork rebase items

- I21. Each fork rebase proposal ([proposals.md](proposals.md) C14) is an
  item with key `fork:<proposal>`, kind `proposal`, kicker "Rebase onto
  Core", title "Rebase <domain> onto Home Assistant Core <version>" (daemon
  words: the domain is an identifier) and push title "Rebase onto Core".
  Its `change` is the diff from the fork as it is to the rebased files,
  the files and, as `shown`, the result's hash; its facts are the Core
  version, the fork's origin, the changes kept and dropped (ids with their
  line counts), the files rebased, the files left as they are and the kept
  changes' text; the proposer's reason is only the `agent` text. Open in
  act mode it waits with an `approval` need: primary `approve_rebase`
  ("Rebase tonight"), secondary `approve_rebase_now` ("Rebase now") and
  `reject`; in observe mode it is Proposed with `reject` only. Approved it
  is Scheduled (gist "approved; runs …", plus why it waits when the update
  gate is shut) with `withdraw`; while its job runs it is Running with the
  phases Backup, Write, Restart, Verify; applied is Done; rolled back or
  failed is Failed with an `ack` need (urgent for `rollback_failed`),
  pushed by the item book; rejected is Cancelled; stale is Expired. A
  rebase a finding's "Plan a fix" proposed is decided on the finding
  ([maintenance.md](maintenance.md) M13b) while the finding carries it: its
  own item then follows the finding, is quiet, has no need or action and
  never pushes; settled, it follows the finding and keeps its own actions. A fork
  rebase job never makes an update item, and its run's item is titled
  "Approved fork rebase <day>". Its gist ends with the commit's status;
  "not pushed yet" becomes "pushed" once origin holds the commit
  ([updates.md](updates.md) U29). Tests `test_the_pipeline_happy_path`,
  `test_a_restart_failure_restores_the_fork_and_pushes`.

## The Needs-you rule

- I22. **One escalation gate: unknown never asks.** Needs you holds only
  what cannot happen or be resolved without the admin, and only for one
  reason of a closed list (`escalation.Why`). A need is made only by
  `escalation.need(why, …)`, and one function decides whether a record may
  be in Needs you: `escalation.gate(state, need)`. The item book applies it
  to every item it composes and the store applies it again to every record
  it writes (`Store.put_item`), so nothing reaches Needs you without a
  reason from the list, whatever composed it: a `waiting` record without
  one is written `proposed` without its need, a `failed` one without its
  need, and the daemon logs "need refused by the escalation gate" with
  the item key. At start the book recomposes every source (health
  included) and passes every stored record in Needs you through the gate
  again (a stored `activity:` entry that needs the admin is `failed`).
  The list:

  | `why` | Asks you because | Only from |
  | --- | --- | --- |
  | `critical` | the safety net found something critical | a critical health item ([health.md](health.md) H9: Core down, config invalid, access hardware exposed or down, a broken custom integration, low memory, an OOM kill, no current backup, a critical repair); never AI-gated, never closed by a verdict |
  | `approval` | it needs your approval | an update to approve (I6: APPROVE tier, not soaking, not in the soak-unknown hold, [updates.md](updates.md) U5, U5a; a soaking one asks when a labelled urgent fix lifts its wait, U9b), a reboot pending (I6), a complete open config change (I9, a maintenance fix only once its finding may ask, [maintenance.md](maintenance.md) M11a), a fork rebase in act mode (I21); bound to the action, with its expiry |
  | `deadline` | its removal deadline is close | a maintenance deprecation within `ASK_BEFORE` (30 days) of its named removal, or with the removal installed, not checked "not affected" ([maintenance.md](maintenance.md) M11) |
  | `grounded_needs_owner` | the agent checked it and found something you must do | a `needs_owner` verdict that passed `verdicts.grounded` (an unhedged reason and owner action, a concrete read among its evidence, an owner action that does more than keep things as they are), on a warning health item (H16a) or a maintenance finding without a deadline ([maintenance.md](maintenance.md) M13d), never a Core-minor finding (M13e); its need quotes the owner action. The only way in for model text |
  | `stuck` | it has stayed undecided for 7 days | a warning health item still reported with no settled verdict `STUCK_DAYS` (7) after it opened, or past its own watch window (`needs_after`, [health.md](health.md) H16c); never an info item |
  | `physical` | only you can do this, by hand | a warning health item that Home Assistant itself says needs hands (a repair or a failed config entry whose issue id, translation key or reason names re-authentication or re-pairing, or the host's under-voltage sensor, [health.md](health.md) H16c), or the vacuums overlay's room drift (I10) |
  | `failed` | something the daemon did failed and nothing retries it | an update whose rollback failed, an OS update not activated after its reboot, a failed install nothing retries (I6); a night aborted by a failure that repeats, or a whole-night regression (I8); a change that may be half applied (I9); a fork rebase that did not stay (I21); a failed backup or reload (I16) |

  Not on the list, so never in Needs you: no verdict, a verdict
  `unknown`, a check that could not run (AI off, capped, quota out, a
  failed run), an answer the daemon rejected or dropped, an ungrounded or
  hedged `needs_owner`, soaking updates, updates without a known release
  age (until U5a asks), an update failure the next window retries by
  itself, a skipped approved update no longer offered, a config change
  that failed and was cleanly undone or cannot be shown in full, a
  maintenance fix before its finding may ask, a finding without a deadline
  (unless `grounded_needs_owner`), a finding checked "not affected", a
  Core-minor finding (whatever its verdict), a noted fact, information,
  questions, investigations, reminders and the agent's own notes and
  messages. Such an item stays in Coming up (or History) with its state in
  daemon words ("checking whether it affects this install", "could not
  decide: <why> (next try <when>)") and the daily cycle checks it again.
  No capability, chat card, overlay hook or agent run sets a need: the
  composers do, through `escalation.need`, and the gate admits it.

  Every need carries `why`, `because` and `tried` (I22a); the panel shows
  "Asks you because: <because>. Tried: <tried>." above its text, so any
  escape is diagnosable at a glance. Tests
  `test_the_store_refuses_a_need_without_a_reason`,
  `test_no_need_is_made_outside_the_gate` (every call site that makes a
  need, with its reason, is listed: a new one fails until it is), the
  exhaustive path audit
  `test_every_composed_health_need_names_a_reason_from_the_list`,
  `test_every_composed_maintenance_need_names_a_reason_from_the_list`,
  `test_every_composed_update_need_names_a_reason_from_the_list` and
  `test_every_composed_change_fork_window_and_entry_need_names_a_reason_from_the_list`
  (every composer over the product of its kinds, severities, verdict
  states, deadline states and ages; each item in Needs you is checked
  against the one reason it must give, and the gate must refuse nothing),
  `test_each_reason_on_the_list_asks`, and the regression corpus in
  `tests/test_escalation.py` (the incidents of 2026-11-04 to 2026-11-07,
  each with Needs you empty).

- I22a. **The self-resolve ladder.** Before an item may ask for any
  reason but `critical` and `approval`, the agent climbs one ladder, and
  the need records each rung as it went (`need.tried`, daemon words, at
  most 8, e.g. "reload example_hub: refused (… cannot be unloaded …)",
  "effect check: none observed", "check: not decided"). The gate refuses a
  `deadline`, `grounded_needs_owner`, `stuck`, `physical` or `failed` need
  without it, exactly like one without a reason. The rungs, in order:
  1. deterministic checks: configured here (maintenance.md M4a, M13),
     the effect check ([health.md](health.md) H16b), a real deadline
     (maintenance.md M11), a reading held over two checks (H16c);
  2. the daemon's own permitted repairs where they apply to the item's
     kind: a failed config entry's integration is reloaded once before
     anything about it may ask (H16d, within the reload allowlist,
     never-list, rate and quota of [reload.md](reload.md)), a finding is
     settled after a fix (maintenance.md M13c), stale evidence is read
     again (M5h); an entry that came back leaves the check;
  3. a grounded verdict of the agent's check (H16a, maintenance.md M13g);
  4. a `fixable` verdict whose fix is within the proposal rules becomes a
     proposal, which asks only as `approval` once its finding may ask
     (maintenance.md M11a, M16), never as `grounded_needs_owner`; a finding
     with an open or applying proposal never asks on its own;
  5. only then `grounded_needs_owner` or `stuck`.
  The exceptions stay deterministic: `critical` asks at once (after the
  daemon's permitted reload of a stuck entry, whose outcome its need also
  records), `approval` is the prepared action waiting only for the yes,
  `physical` verifies its reading (two checks in a row for a reading,
  Home Assistant's own repair or entry state otherwise) and then asks, and
  `failed` records what the daemon attempted (the install, the rollback,
  the restore, the backup). Tests
  `test_a_fixable_finding_never_produces_needs_owner`,
  `test_a_reloadable_entry_in_setup_retry_is_reloaded_before_any_ask`,
  `test_every_needs_you_item_but_critical_and_approval_says_what_was_tried`.

- I23. **Every item shown is actionable** ([panel.md](panel.md) N13):
  every item in Needs you, In progress, Coming up or Suggestions has a
  need, an action or is Running, and every item has its own
  conversation ([chat.md](chat.md) T1).
  Maintenance findings offer Plan a fix, Remind me and Hide until it
  changes ([maintenance.md](maintenance.md) M13a). Test
  `test_every_item_the_panel_shows_offers_an_action`.

## Pushes

- N1. **Item pushes: only what is time-sensitive.** For each open item in
  Needs you, the push key is `state:need.kind:primary.id`. After each write
  and on the minute tick the daemon decides: a new need (the key differs
  from the last pushed key) of a **time-sensitive** item pushes on its own;
  time-sensitive is urgent, a deadline within 7 days (`source.deadline`),
  or an approval that gates tonight's update window (`source.push_now`: an
  update, a reboot or a fork rebase to approve). Every other new need goes
  into the **daily digest** (N1a). Only an urgent item is pushed again
  while it waits (after a day, resurface); nothing else resurfaces. A
  failed delivery is retried after 15 minutes; a need whose `expires_at`
  passed, or a push already pending, is skipped. Sources with
  `pushes=False` are marked pushed silently. Leaving and re-entering Needs
  you is a new need. During quiet hours (N8) the book itself keeps a
  non-urgent item's push and the digest back (still owed, decided again
  after the window); an urgent one goes out. Only `sent` and `relayed`
  count as delivered. Tests
  `test_entering_needs_pushes_once_and_coming_back_pushes_again`,
  `test_a_push_counts_only_once_it_was_delivered`,
  `test_a_push_decided_for_a_need_that_cleared_is_not_sent`,
  `test_needs_you_pushes_only_urgent_or_daily_digest`,
  `test_an_item_push_waits_through_quiet_hours_and_is_retried_if_the_morning_send_is_limited`.
- N1a. **The daily digest.** At most one digest push a local day (kv
  `items.digest`): the first decision of a day with new needs that are not
  time-sensitive sends one push for all of them. One item alone is that
  item's own push (N2); several are one push titled "Ops Agent: <n> things
  need you", the body their titles (daemon words), tag `ha-ops-digest`,
  opening Home. Each item it covers is recorded as pushed for its need; a
  new need later that day waits for the next day's digest. It waits
  through quiet hours like any item push (N1) and the agent budget counts
  it once. A push or digest the daily budget refuses (`rate_limited`) is
  recorded once and tried again when the budget renews (the next UTC day),
  never every cycle; any other failed delivery is retried after 15 minutes.
  A `grounded_needs_owner`, `stuck` or `physical` health item is not
  time-sensitive (except the vacuums drift, `push_now`): it goes out in the
  digest. Test
  `test_a_rate_limited_need_waits_for_the_next_budget_not_every_cycle`.
- N1b. **Withdrawn when resolved.** When an item whose push was delivered
  leaves Needs you, the daemon withdraws it: a Companion
  `clear_notification` with `tag` the item id (the tag its push carried),
  sent through the same notify service, outside the budgets and quiet
  hours; a digest is withdrawn once none of its items is in Needs you.
  Test `test_a_resolved_item_clears_its_push`.
- N2. Item push content: title is the source's push title or the item title
  cut at 70; body `"<Word>: <gist>. Open it to <primary label>."`, with the
  need text appended when urgent, cut at 400 characters; tag is the item id;
  the link is the panel's `/t/<item id>`; budget class SAFETY when urgent,
  else AGENT. Its one button is Reply, a message on the item's conversation
  (recorded with the action id, [chat.md](chat.md) T14a); nothing executes
  from a push. Test `test_approvals_ask_once_with_a_link_to_the_panel`.
- N3. **Other push triggers.** The baseline seed ("the agent is watching"),
  a critical health result (deterministic, urgent, whatever the AI said;
  a warning is never pushed for a triage that could not run, I22), Core down (after 5 minutes;
  20 in the update window; 45 while Supervisor jobs are busy; sent through
  the relay) and "Core is back" when the down
  push went out, a degraded store, kill-switch and usage notices (once per
  key per day), chat replies, follow-ups and investigation results when the
  admin is not watching, and reminders.
- N4. **Budgets.** One daily bucket per budget class: `agent` capped by
  `notify.max_pushes_per_day` (default 3), `safety` fixed at 8, `user` fixed
  at 40; an urgent AGENT push is time-sensitive but still counts as
  `agent`. Tests `test_rate_limits`, `test_budget_classes_are_the_buckets`.
- N5. **Target and delivery.** The target is the `notify.service` option,
  else the profile's `notify_service`; none means every push fails. Delivery
  is the Core `notify.<service>` service with tag, group `ha-ops`, the panel
  URL (any other URL is replaced by the panel URL), `time-sensitive`
  interruption level when urgent, and actions; when Core is down or the call
  fails, the cached mobile-app push URL (stored 0600 under `/data/secrets`)
  is used as a relay. The cache holds only the verified admin's
  registration (the one the admin identity chose, by user and device id),
  never another registration with the same device name; without a
  verified admin it is not refreshed. Tests `test_relay_fallback`,
  `test_extract_registration_matches_service_name`.
- N6. When the push device's user is not an active administrator, the agent
  fails closed and sends nothing, safety pushes included
  ([configuration.md](configuration.md) C5).
- N7. **Agent text pushes** are cut at 180 characters with an ellipsis, titled
  `Agent: <title>` (70 characters), tagged `ha-ops-agent-…` (or, for an
  answer in an item's conversation, the item's id unless its push is
  urgent, [chat.md](chat.md) T10), and are never SAFETY class; the full
  text stays in the panel. An answer in a conversation takes its item's id
  as its tag, so it replaces the push the admin replied to, except for an
  item whose push is urgent (SAFETY class): its answer is tagged
  `ha-ops-agent-<item id>`, so model text never replaces a daemon safety
  push. Test `test_an_answer_never_replaces_an_urgent_items_push`. A push
  from the agent's own run (`notify.admin`)
  is its own item (the words under `agent`) and opens it, but carries no
  Reply button: model text never invites an answer. It goes out
  only with a reason class (`needs_action`, `physical_task`, `security`);
  `note` makes it a quiet History note, and no reason is refused
  ([capabilities.md](capabilities.md) K11). The agent's own reminders and
  follow-ups follow the same rule ([chat.md](chat.md) T16): no Reply
  button, no need from model text, and a push only with a push class. Test
  `test_notify_one_per_run_and_urgent_only_with_critical`.
- N8. **Quiet hours.** Non-bypassing pushes that are not an item's (N1 keeps
  those back in the item book) are held in the store and sent on the first
  liveness pass after the window; urgent, quiet-ok and SAFETY pushes
  bypass. A held push that then hits the daily cap stays listed in
  the panel as undelivered for a day; a failed one is retried next pass.
  Admin-set reminders are quiet-ok; agent-set ones wait. Tests
  `test_what_is_never_held`, `test_non_urgent_pushes_are_held_and_urgent_ones_are_not`,
  `test_reminders_the_admin_set_ring_but_agent_ones_wait`,
  `test_the_push_cap_leaves_overflow_in_the_panel`,
  `test_kill_switch_notices_are_safety_pushes_never_held`.
- N9. **Presence.** Conversation output is not pushed while the admin
  watches that item's conversation (a 20-second presence lease, at most 8
  leases); reminders and safety
  pushes ignore presence. Tests `test_reply_is_not_pushed_while_the_admin_watches`,
  `test_reminders_ignore_presence`.
- N10. **Push actions.** The Companion push (`notify.Notifier`) is the only channel. Each action
  button gets an id `OPS_<KIND>_<32 hex>` recorded with a 3-day expiry; the
  kinds are REPLY (text input), SNOOZE and DONE. Without a store no buttons
  are sent. Inbound handling is in [chat.md](chat.md).
