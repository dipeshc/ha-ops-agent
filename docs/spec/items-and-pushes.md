# Items, the state machine and the push policy

Everything the agent does is an item on the panel's Home page, with its
history. The daemon derives items from its own records; the panel never
infers state. The wire shape is in [`docs/panel-api.md`](../panel-api.md).

Implementation: `opsd/items.py` (`ItemBook`), `opsd/activity.py`,
`opsd/notify.py`, `opsd/channels.py`, `opsd/quiet.py`, `opsd/live.py`.
Tests: `tests/test_items.py`, `tests/test_needs_and_watches.py`,
`tests/test_notify_triage.py`, `tests/test_quiet.py`, `tests/test_chat_ux.py`.

## Vocabulary

- I1. Kinds: `update`, `reminder`, `investigation`, `proposal`, `check`,
  `backup`, `reload`, `window` (a store CHECK). States: `proposed`,
  `waiting`, `scheduled`, `running`, `done`, `failed`, `cancelled`,
  `expired`; the last four are terminal.
- I2. Need kinds: `approval`, `confirm`, `physical`, `answer`, `ack`. The
  0.17.0 code only ever produces `approval` and `ack`. Actors: `you`,
  `daemon`, `agent`. Action ids match `[a-z_]{1,32}`; item ids match
  `[a-z]-[0-9a-z]{1,40}`.
- I3. **Needs you** is exactly `state == waiting`, or `failed` with a need.
  The store enforces it: `need` is non-null iff `waiting`, or the state is
  `failed`. Tests `test_the_store_refuses_a_waiting_item_without_a_need`,
  `test_migration_17_backfills_items_and_nothing_old_needs_you`.
- I4. Keys and ids: keys are `updates:<entity>@<version>`, `window:<run>`,
  `change:<id>`, `fact:<id>`, `task:<id>`, `health:<key>`, `activity:<id>`.
  The URL id is `<prefix>-<sha256(key)[:12]>` with prefixes `u` (updates),
  `w` (window), `p` (change, fact), `r` (task), `c` (health), `x` (other);
  activity items are `a-<n>`.

## Derivation

- I5. There is no stored transition table: every pass, `ItemBook.sync`
  recomputes each derived item from its source record. A source that
  disappears resolves its item (`cancelled` "no longer pending"; a health
  item becomes `done` "cleared"; an update gets a reasoned gist); an item is
  never left waiting for a vanished source. Test
  `test_a_resolved_condition_resolves_its_item`.
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
- I15. Every changed record publishes a live `item {id, revision, state,
  needs_count}` event. `needs_count` is one SQL count and the same number on
  `api/home`, `api/history`, `api/state` and the status sensor.
- I16. Activity entries are items too: with an item key they become events of
  that item, otherwise standalone `activity:<id>` items. Quiet kinds
  (`check`, `suppressed`, `updates`, `reminder`, `investigation`) fold in
  History and never show on Home unless they need the admin; an entry that
  needs the admin becomes `waiting` with an `ack` and also posts one chat
  message naming it. Test `test_activity_entries_become_items_and_events`.
- I17. Home sections: `needs` oldest `needs_since` first; `running`;
  `coming` (proposed, scheduled) by `at`; `today` (terminal, not quiet,
  resolved since local midnight) newest first. History: 50 per page, keyset
  cursor on `(at, id)`, filters `all`, `needs`, `changes` (proposal, update,
  reload), `checks` (check, backup); day grouping and folding of quiet items
  is the panel's job. Test `test_history_pages_newest_first`.
- I18. Resolved items not in Needs you, and their events, are deleted after
  90 days.

## Pushes

- N1. **Item pushes.** For each open item in Needs you, the push key is
  `state:need.kind:primary.id`. The daemon pushes when the key differs from
  the last pushed key, or when the last delivery is at least 1 day old
  (resurface); a failed delivery is retried after 15 minutes; a need whose
  `expires_at` passed, or a push already pending, is skipped. Sources with
  `pushes=False` and the first sync after the item migration are marked
  pushed silently. Leaving and re-entering Needs you pushes again. Tests
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
