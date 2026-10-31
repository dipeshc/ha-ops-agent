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
  `backup`, `reload`, `window`, `maintenance` (a store CHECK). States: `proposed`,
  `waiting`, `scheduled`, `running`, `done`, `failed`, `cancelled`,
  `expired`; the last four are terminal.
- I2. Need kinds: `approval`, `confirm`, `physical`, `answer`, `ack`. The
  0.19.0 code only ever produces `approval` and `ack`. Actors: `you`,
  `daemon`, `agent`. Action ids match `[a-z_]{1,32}`; item ids match
  `[a-z]-[0-9a-z]{1,40}`.
- I3. **Needs you** is exactly `state == waiting`, or `failed` with a need.
  The store enforces it: `need` is non-null iff `waiting`, or the state is
  `failed`. Test `test_the_store_refuses_a_waiting_item_without_a_need`.
- I4. Keys and ids: keys are `updates:<entity>@<version>`, `window:<run>`,
  `change:<id>`, `fact:<id>`, `task:<id>`, `health:<key>`, `maint:<finding>`,
  `activity:<id>`. The URL id is `<prefix>-<sha256(key)[:12]>` with prefixes
  `u` (updates), `w` (window), `p` (change, fact), `r` (task), `c` (health),
  `m` (maintenance), `x` (other);
  activity items are `a-<n>`, numbered by `kv` `activity.seq` so a number is
  never reused after a prune.

## Written at the source

- I5. **Items are written at their source.** Every store write records what
  it touched: an update job or run, an approval, a change proposal, a house
  fact, a task, an item written directly (I16), or a `kv` key among the
  update flags (paused, a failed night and its acknowledgement, a failed
  backup or check before a night), the item acknowledgements
  (`item.ack:<key>`) and the maintenance state (`maintenance:`). Before that
  transaction commits, the item book composes the items those records show
  and writes each one, with its event, in the same transaction: a job or an
  update approval writes its update's item and its night's, a run (a new
  plan, a night's progress) and an update flag every update item and night,
  a change, a fact or a task its own item, a dashboard approval or the
  maintenance state the maintenance items. The health check's save of its
  open items writes the health items in one transaction. An item and its
  source therefore never disagree, even after a crash. A failure to compose
  or write one item is logged and blocks neither the source's write nor the
  other items. Tests `test_an_item_is_written_in_its_sources_transaction`,
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
  `test_an_old_record_never_turns_into_cancelled`,
  `test_a_house_fact_item_resolves_with_its_decision`.
- I5b. **Time.** An item whose words depend on the clock carries
  `source.recheck_at`: a health item whose `needs_after` is ahead (I10), an
  update rejected within the last 7 days (I6). The minute tick
  ([overview.md](overview.md) D2) recomposes such an item once that time
  passes, then decides the pushes time made due (N1). The add-on options
  are a source too: at each start the daemon recomposes the update items
  and nights (the gate and the window) and the maintenance items (act
  mode). Tests `test_the_items_engine_asks_only_once_remote_access_stays_down`,
  `test_a_rejected_update_asks_again_once_the_week_is_over`.
- I6. Update items: AUTO and not held is `scheduled` (or `proposed` while a
  gate blocks the window); approved is `scheduled` with a `withdraw` action
  and a footnote naming the approval, versions and plan; an APPROVE item
  still soaking is `proposed` (actions approve, approve_now, reject) and is
  never pushed; otherwise `waiting` with an `approval` need (primary
  `approve` "Approve for tonight", secondary `approve_now` except for OS,
  and `reject`). A running job is `running` with phases; success is `done`;
  installed-but-reboot-pending is `waiting` with primary `reboot_window`
  (never `failed`); failed, rolled back or rollback-failed is `failed` with
  an `ack` need, urgent for the two rollback outcomes, and hidden once acked.
- I7. Phases: `[Backup, Install, Reboot, Verify]` for OS, else
  `[Backup, Install, Verify]`; `usual_s` is the median of succeeded jobs of
  that kind, else 600 (core), 240 (add-on), 120 (HACS), 600 (firmware),
  900 (OS).
- I8. Window items exist only in `act` mode; done and aborted nights are
  `quiet`; a failed night is `failed` with an urgent `ack` unless a job
  already escalated.
- I9. Proposal items: `open` is `waiting` (`apply` / `reject`, with the row's
  expiry); approved is `running`; applied is `done` with a `revert` action;
  failed is `failed` with an `ack`; rejected or stale is `cancelled`; expired
  is `expired`; refused rows produce no item. The proposer's words go in
  `agent`, never in the need or the push. Test
  `test_a_change_proposal_keeps_the_proposers_words_out_of_the_need_and_the_push`.
- I10. Fact items are `waiting` with an `ack` (`dismiss`). Task items are
  `scheduled` (investigations `running`) with `cancel` or `stop`. Health
  items are `waiting` with an `ack` (`snooze` 7 days, `accept` 30 days),
  urgent when critical, and never pushed by the item book (the health check
  pushes its own); a health item whose `needs_after` is in the future is
  `proposed` without a need. Tests `test_health_items_wait_and_clear`,
  `test_the_items_engine_asks_only_once_remote_access_stays_down`.
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
  Test `test_an_item_written_from_a_worker_thread_is_pushed_on_the_loop`. `needs_count` is one SQL count and the same number on
  `api/home`, `api/history`, `api/state` and the status sensor.
- I16. Activity entries are items too (there is no separate log): with an
  item key they become events of that item, otherwise standalone
  `activity:<n>` items, whose push is decided at the write like any other's. Quiet kinds
  (`check`, `suppressed`, `updates`, `reminder`, `investigation`) fold in
  History and never show on Home unless they need the admin; an entry that
  needs the admin becomes `waiting` with an `ack` and also posts one chat
  message naming it. Tests `test_activity_entries_become_items_and_events`,
  `test_a_standalone_entry_is_pushed_when_it_is_written`.
- I17. Home sections: `needs` oldest `needs_since` first; `running`;
  `coming` (proposed, scheduled) by `at`; `suggestions` (proposed
  maintenance replacement candidates and suggestions) by `at`; `today` (terminal, not quiet,
  resolved since local midnight) newest first. History: 50 per page, keyset
  cursor on `(at, id)`, filters `all`, `needs`, `changes` (proposal, update,
  reload), `checks` (check, backup); day grouping and folding of quiet items
  is the panel's job. Test `test_history_pages_newest_first`.
- I18. Resolved items not in Needs you, and their events, are deleted after
  90 days.

## Pushes

- N1. **Item pushes.** For each open item in Needs you, the push key is
  `state:need.kind:primary.id`. After each write and on the minute tick the
  daemon pushes when the key differs from the last pushed key, or when the
  last delivery is at least 1 day old (resurface); a failed delivery is
  retried after 15 minutes; a need whose `expires_at` passed, or a push
  already pending, is skipped. Sources with `pushes=False` are marked pushed
  silently. Leaving and re-entering Needs you pushes again. Tests
  `test_entering_needs_pushes_once_and_coming_back_pushes_again`,
  `test_a_push_counts_only_once_it_was_delivered`,
  `test_a_push_decided_for_a_need_that_cleared_is_not_sent`.
- N2. Item push content: title is the source's push title or the item title
  cut at 70; body `"<Word>: <gist>. Open it to <primary label>."`, with the
  need text appended when urgent, cut at 400 characters; tag is the item id;
  the link is the panel's `/t/<item id>`; budget class SAFETY when urgent,
  else AGENT.
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
  is used as a relay. Test `test_relay_fallback`.
- N6. When the push device's user is not an active administrator, the agent
  fails closed and sends nothing, safety pushes included
  ([configuration.md](configuration.md) C5).
- N7. **Agent text pushes** are cut at 180 characters with an ellipsis, titled
  `Agent: <title>` (70 characters), tagged `ha-ops-agent`, and are never
  SAFETY class; the full text stays in the panel.
- N8. **Quiet hours.** Non-bypassing pushes are held in the store and sent on
  the first liveness pass after the window; urgent, quiet-ok and SAFETY
  pushes bypass. A held push that then hits the daily cap stays listed in
  the panel as undelivered for a day; a failed one is retried next pass.
  Admin-set reminders are quiet-ok; agent-set ones wait. Tests
  `test_what_is_never_held`, `test_non_urgent_pushes_are_held_and_urgent_ones_are_not`,
  `test_reminders_the_admin_set_ring_but_agent_ones_wait`,
  `test_the_push_cap_leaves_overflow_in_the_panel`,
  `test_kill_switch_notices_are_safety_pushes_never_held`.
- N9. **Presence.** Chat output is not pushed while the admin watches the
  thread (a 20-second presence lease, at most 8 leases); reminders and safety
  pushes ignore presence. Tests `test_reply_is_not_pushed_while_the_admin_watches`,
  `test_reminders_ignore_presence`.
- N10. **Push actions.** The Companion push is the only channel. Each action
  button gets an id `OPS_<KIND>_<32 hex>` recorded with a 3-day expiry; the
  kinds are REPLY (text input), SNOOZE and DONE. Without a store no buttons
  are sent. Inbound handling is in [chat.md](chat.md).

## Maintenance items

- I19. Maintenance items are written from the stored findings, their plans
  and their dashboard plans at each change of the maintenance state (I5);
  the composition, escalation, pushes and actions are
  in [maintenance.md](maintenance.md) M10–M18 and the wire shape in
  [panel.md](panel.md) N20. Only entering Needs you pushes (a deprecation
  within two releases, a dashboard plan waiting for approval, a failed
  dashboard change); every other maintenance item is silent.
