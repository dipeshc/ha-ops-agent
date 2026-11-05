# Items, the state machine and the push policy

Everything the agent does is an item on the panel's Home page, with its
history. The daemon writes each item from its own records at the moment the
record changes; the panel never infers state. The wire shape is in
[panel.md](panel.md).

Implementation: `opsd/items.py` (`ItemBook`, `Composer`), the transaction
observer in `opsd/store.py`, `opsd/activity.py`, `opsd/notify.py`,
`opsd/channels.py`, `opsd/quiet.py`, `opsd/live.py`.
Tests: `tests/test_items.py`, `tests/test_needs_and_watches.py`,
`tests/test_notify_triage.py`, `tests/test_quiet.py`, `tests/test_chat_ux.py`.

## Vocabulary

- I1. Kinds: `update`, `reminder`, `investigation`, `proposal`, `check`,
  `backup`, `reload`, `window`, `maintenance` (a store CHECK), and
  `question`, which the store keeps as `investigation` (key `ask:` or
  `chat:earlier`) and the item record serves as `question` (I22). States: `proposed`,
  `waiting`, `scheduled`, `running`, `done`, `failed`, `cancelled`,
  `expired`; the last four are terminal.
- I2. Need kinds: `approval`, `confirm`, `physical`, `answer`, `ack`. The
  code only ever produces `approval` and `ack`. Actors: `you`,
  `daemon`, `agent`. Action ids match `[a-z_]{1,32}`; item ids match
  `[a-z]-[0-9a-z]{1,40}`.
- I3. **Needs you** is exactly `state == waiting`, or `failed` with a need.
  The store enforces it: `need` is non-null iff `waiting`, or the state is
  `failed`. Test `test_the_store_refuses_a_waiting_item_without_a_need`.
- I4. Keys and ids: keys are `updates:<entity>@<version>`, `window:<run>`,
  `change:<id>`, `fact:<id>`, `task:<id>`, `health:<key>`, `maint:<finding>`,
  `activity:<id>`, `fork:<proposal>`, `ask:<token>` (a question) and
  `chat:earlier` (the earlier one chat). The URL id is
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
  verdicts (`verdict:`, [maintenance.md](maintenance.md) M14d). Before that
  transaction commits, the item book composes the items those records show
  and writes each one, with its event, in the same transaction: a job
  writes its update's item and its night's, an update or reboot approval its
  update's item, a run (a new plan, a night's progress) and an update flag
  every update item and night, a change proposed for a maintenance finding
  also that finding's item,
  a change, a fact or a task its own item, a dashboard approval or the
  maintenance state the maintenance items. The health check's save of its
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
  `test_review_5_one_bad_item_does_not_stop_the_others`.
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
  oldest of many open changes stays waiting. Tests
  `test_a_resolved_condition_resolves_its_item`,
  A health `log:<fp>` item that leaves because its fingerprint joined the
  baseline is not called cleared: "not new any more: this error is part of
  the baseline now; it is reported again if it grows tenfold" (test
  `test_a_new_log_error_is_not_cleared_while_it_continues`); a maintenance
  finding resolves with the scan's own reason when it has one
  ([maintenance.md](maintenance.md) M14).
  `test_an_old_record_never_turns_into_cancelled`,
  `test_a_house_fact_item_resolves_with_its_decision`.
- I5b. **Time.** An item whose words depend on the clock carries
  `source.recheck_at`: a health item whose `needs_after` is ahead (I10), an
  update rejected within the last 7 days (I6), a maintenance finding or its
  config change whose deadline is more than 30 days away ([maintenance.md](maintenance.md)
  M12, M12a). The minute tick
  ([overview.md](overview.md) D2) recomposes such an item (health, update,
  maintenance and change items) once that time passes and forgets a recheck its source did not renew (a resolved item
  carries none), then decides the pushes time made due (N1). The add-on options
  are a source too: at each start the daemon recomposes the update items
  and nights (the gate and the window), the maintenance items (act mode),
  the health items (from `open-items.json`, so one that was watching
  under 0.18, which had no `recheck_at`, asks on time), and the open or
  asking change and fact items (so a need an earlier version composed
  follows this version's rule, I20). Tests
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
  `test_review_1_a_silent_night_does_not_swallow_a_later_needs_you_push`.
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
  M12a); approved is `running`; applied is `done` with a `revert` action
  (on its own item, also for a change a finding's plan proposed,
  [maintenance.md](maintenance.md) M14b);
  failed is `failed` without a need when the daemon undid it cleanly (the
  files restored and, after a reload, reloaded again); a failure that may
  have left the house half applied (the restore or its reload failed, an
  error stopped the pipeline, a restart interrupted it; kv
  `change.unclean:<id>`) is `failed` with an urgent `ack` need and is
  pushed;
  an open change whose diff cannot be shown in full is `proposed` with
  `reject` only (it cannot be applied and expires by itself); rejected or
  stale is `cancelled`; expired is `expired`; refused rows produce no item.
  Tests `test_review_4_a_failed_change_is_one_failed_item_that_does_not_ask`,
  `test_a_change_not_shown_in_full_cannot_be_approved`,
  `test_a_change_that_may_be_half_applied_asks_and_a_clean_failure_does_not`,
  `test_an_error_that_stops_the_pipeline_never_leaves_a_change_applying`. The proposer's words go in
  `agent`, never in the need or the push. Test
  `test_a_change_proposal_keeps_the_proposers_words_out_of_the_need_and_the_push`.
- I10. A fact noted without a change proposal is `done` ("kept in the
  agent's store, not proposed as a change"), never in Needs you; its later
  decision still resolves it (I5a). Task items are
  `scheduled` (investigations `running`) with `cancel` or `stop`. Health
  items are `waiting` with an `ack` (`snooze` 7 days, `accept` 30 days),
  urgent when critical, and never pushed by the item book (the health check
  pushes its own); a health item whose `needs_after` is in the future, or
  whose severity is `info`, is `proposed` without a need (with `snooze` and
  `accept` as actions). Test `test_an_info_health_item_is_shown_and_never_asks`. A health item acked since the check ran (by
  the agent in its run, or by the admin) is `done` without a need, actor
  `agent` or `you`, with a gist that says who acked it, until when and why
  ([health.md](health.md) H13a); it never enters Needs you. Tests
  `test_health_items_wait_and_clear`,
  One item per cause: an `update:` info item is no health item (the update
  has its own); a `repair:` item whose repair a maintenance finding carries
  follows that finding (no need, "tracked on its maintenance item"); a
  `backup:run_failed` item follows the failed backup's own item while that
  one asks. A triage verdict ([health.md](health.md) H16a) puts the agent's
  reason under The agent says: `needs_owner` keeps the item in Needs you
  and lets the item book push it (N1); `not_affected` or `fixed` quiets a
  warning that is not guarded to `proposed` with the reason. Tests
  `test_a_deprecation_repair_is_one_item`,
  `test_a_triage_verdict_anchors_the_reason_on_its_item`,
  `test_the_items_engine_asks_only_once_remote_access_stays_down`,
  `test_in_run_effects`.
- I11. Follows: the host-reboot health item follows the OS update whose job
  rebooted the host, and the `reboot_required` repair follows the OS update
  pending its reboot; the follower has no need and resolves with its parent.
- I12. **Revision.** The material fields (kind, title, state, gist, kicker,
  ref, need, about, actions, phase, follows, agent, change, footnote, quiet)
  decide the revision: unchanged material updates only `as_of`, source and
  facts silently; any change bumps the revision by one and writes one event.
  `needs_since` persists while the item stays in Needs you; `resolved_at`
  while the state is unchanged. Tests `test_as_of_and_bindings_never_move_the_revision`,
  `test_review_8_a_new_health_check_does_not_move_the_revision`.
- I13. `agent.text` is the only model-written field, set from the release-note
  review, a proposal's why/effect, or agent-text activity kinds (`notice`,
  `followup`, `investigation`). Model text never titles a need or a push.
  Tests `test_review_3_the_review_reason_stays_out_of_the_need`,
  `test_review_3_an_agent_written_entry_never_titles_a_need_or_a_push`.
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
  entry that needs the admin becomes `waiting` with an `ack`. No entry
  posts into a conversation (0.22.0): the agent's own notes are items and
  History entries only. A config change's outcome is an event on its item. Tests `test_activity_entries_become_items_and_events`,
  `test_a_standalone_entry_is_pushed_when_it_is_written`. A failed backup's
  item stops asking when a later backup succeeds ("resolved: a later
  backup succeeded"). Test `test_a_later_good_backup_resolves_a_failed_one`.
- I17. Home sections (questions are on Ask and never on Home; a quiet
  item that follows another, such as a proposal folded into its finding,
  is shown on that item and not as a row of its own, unless it needs the
  admin): `needs` oldest `needs_since` first; `running`;
  `coming` (proposed, scheduled) by `at`; `suggestions` (proposed
  maintenance replacement candidates and suggestions) by `at`; `today` (terminal, not quiet,
  resolved since local midnight) newest first. History: 50 per page, keyset
  cursor on `(at, id)`, filters `all`, `needs`, `changes` (proposal, update,
  reload), `checks` (check, backup); day grouping and folding of quiet items
  is the panel's job. Test `test_history_pages_newest_first`.
- I18. Resolved items not in Needs you, their events and their
  conversations' turns are deleted after 90 days; the earlier one chat's
  item and turns are never deleted ([chat.md](chat.md) T1a).

- I20. **The Needs-you rule.** Needs you holds only what cannot happen or
  be resolved without the admin: an approval only the admin can give, a
  physical task, a decision, or a failure the agent cannot recover from by
  itself. Everything else is Coming up, History or the agent's own
  handling. The paths that set a need:

  | Source | Need | In Needs you when |
  | --- | --- | --- |
  | Update to approve (I6) | `approval` | APPROVE tier, not soaking and not in the soak-unknown hold ([updates.md](updates.md) U5, U5a) |
  | Update installed, reboot pending (I6) | `approval` | always (the host reboot is the admin's) |
  | Update job failed (I6) | `ack` | rolled back, rollback failed, OS fallback, or a failed install nothing retries by itself (approved, no longer offered, gate off or paused, failed while verifying or in an unknown state, or the entity's second failure in 14 days) |
  | Update night (I8) | `ack` | a failure before the install that repeats until fixed, or a whole-night regression (updates paused) |
  | Config change (I9) | `approval` / `ack` | open and complete, except a maintenance fix before its finding may ask ([maintenance.md](maintenance.md) M12a); a failure that may be half applied |
  | Maintenance finding (I19) | `ack` / `approval` | within 30 days of its deadline and not checked "not affected", a `needs_owner` verdict, or a dashboard restore that failed ([maintenance.md](maintenance.md) M12, M14d) |
  | Health item (I10) | `ack` | warning or critical, not handled by the agent in its run, not quieted by a verdict, not following another item, and not waiting quietly |
  | Activity entry (I16) | `ack` | a failed backup (self-heal or Back up now) that no later backup has superseded |

  Never in Needs you: soaking updates, updates without a known release
  age (until U5a asks), an update failure the next window retries by
  itself, a skipped approved update no longer offered, a config change
  that failed and was cleanly undone or cannot be shown in full, a maintenance fix before its 30 days, a
  finding without a deadline (unless checked `needs_owner`), a finding the agent checked "not affected", a
  release-note breaking change without a named removal, a refused or restored dashboard change, a
  noted fact, info health items, items the agent acked, and the agent's
  own notes and messages.

- I21. **Every item shown is actionable** ([panel.md](panel.md) N21):
  every item in Needs you, In progress, Coming up or Suggestions has a
  need, an action or is Running, and every item but the earlier one chat
  has its own conversation ([chat.md](chat.md) T1).
  Maintenance findings offer Plan a fix, Remind me and Hide until it
  changes ([maintenance.md](maintenance.md) M14a). Test
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
  you is a new need. Tests
  `test_entering_needs_pushes_once_and_coming_back_pushes_again`,
  `test_a_push_counts_only_once_it_was_delivered`,
  `test_a_push_decided_for_a_need_that_cleared_is_not_sent`,
  `test_needs_you_pushes_only_urgent_or_daily_digest`.
- N1a. **The daily digest.** At most one digest push a local day (kv
  `items.digest`): the first decision of a day with new needs that are not
  time-sensitive sends one push for all of them. One item alone is that
  item's own push (N2); several are one push titled "Ops Agent: <n> things
  need you", the body their titles (daemon words), tag `ha-ops-digest`,
  opening Home. Each item it covers is recorded as pushed for its need; a
  new need later that day waits for the next day's digest. It is
  delivered like any push (quiet hours hold it, the normal budget counts
  it once).
- N1b. **Withdrawn when resolved.** When an item whose push was delivered
  leaves Needs you, the daemon withdraws it: a Companion
  `clear_notification` with `tag` the item id (the tag its push carried),
  sent through the same notify service, outside the budgets and quiet
  hours; a digest is withdrawn once none of its items is in Needs you. A
  push held for after quiet hours whose item has left Needs you is
  dropped, not sent. Test `test_a_resolved_item_clears_its_push`.
- N2. Item push content: title is the source's push title or the item title
  cut at 70; body `"<Word>: <gist>. Open it to <primary label>."`, with the
  need text appended when urgent, cut at 400 characters; tag is the item id;
  the link is the panel's `/t/<item id>`; budget class SAFETY when urgent,
  else AGENT. Its one button is Reply, a message on the item's conversation
  (recorded with the action id, [chat.md](chat.md) T14a); nothing executes
  from a push. Test `test_approvals_ask_once_with_a_link_to_the_panel`.
- N3. **Other push triggers.** The baseline seed ("the agent is watching"),
  a critical health result (deterministic, urgent, whatever the AI said), a
  warning summary when triage is unavailable, Core down (after 5 minutes;
  20 while Core is starting or in the update window; 45 while Supervisor
  jobs are busy; sent through the relay) and "Core is back" when the down
  push went out, a degraded store, kill-switch and usage notices (once per
  key per day), chat replies, follow-ups and investigation results when the
  admin is not watching, and reminders.
- N4. **Budgets.** Three daily buckets: `normal` capped by
  `notify.max_pushes_per_day` (default 3), `safety` fixed at 8, `user` fixed
  at 40. SAFETY-class pushes use `safety`, USER-class `user`, everything
  else `normal`; an urgent AGENT push is time-sensitive but still `normal`.
  Test `test_rate_limits`.
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
  `test_a_triage_push_links_to_the_chat_without_a_reply_button`.
- N8. **Quiet hours.** Non-bypassing pushes are held in the store and sent on
  the first liveness pass after the window; urgent, quiet-ok and SAFETY
  pushes bypass. A held push that then hits the daily cap stays listed in
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
- N10. **Push actions.** The Companion push is the only channel. Each action
  button gets an id `OPS_<KIND>_<32 hex>` recorded with a 3-day expiry; the
  kinds are REPLY (text input), SNOOZE and DONE. Without a store no buttons
  are sent. Inbound handling is in [chat.md](chat.md).

## Questions

- I22. A question ([chat.md](chat.md) T1b) is an item with key
  `ask:<token>`, id `q-…`, kicker "Question", the admin's text as its
  title, Running / Done / Failed as its conversation stands, never a need
  and never pushed by the book; History's `questions` filter lists it with
  the earlier one chat (`chat:earlier`, Done, read only). Tests
  `test_a_question_is_an_item_that_opens_at_once`,
  `test_the_earlier_one_chat_becomes_one_read_only_history_item`.

## Maintenance items

- I19. Maintenance items are written from the stored findings, their plans
  and their dashboard plans and verdicts at each change of the maintenance
  state or a `verdict:` record (I5); the composition, escalation, verdicts,
  pushes and actions are in [maintenance.md](maintenance.md) M10–M18 and
  the wire shape in [panel.md](panel.md) N20. Only entering Needs you can
  push, and only as N1 says (a deadline within 7 days or a failed dashboard
  restore on its own, anything else in the daily digest); every other
  maintenance item is silent. A config change proposed for a finding
  follows its finding's deadline (maintenance.md M12a). A Core-minor
  finding (maintenance.md M4b) follows the pending Core update into that
  minor while there is one: the update's item lists the notes as facts and
  is the one item.

## Fork rebase items

- I20. Each fork rebase proposal ([proposals.md](proposals.md) C14) is an
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
  ([maintenance.md](maintenance.md) M14b) while the finding carries it: its
  own item then follows the finding, is quiet, has no need or action and
  never pushes; settled, it follows the finding and keeps its own actions. A fork
  rebase job never makes an update item, and its run's item is titled
  "Approved fork rebase <day>". Its gist ends with the commit's status;
  "not pushed yet" becomes "pushed" once origin holds the commit
  ([updates.md](updates.md) U29). Tests `test_the_pipeline_happy_path`,
  `test_a_restart_failure_restores_the_fork_and_pushes`.

