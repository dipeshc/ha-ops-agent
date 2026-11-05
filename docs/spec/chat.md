# Conversations, push replies, reminders and investigations

Conversations live on items. Every item has its own conversation
with the agent, and a question about anything without an item is an item of
its own (a question). The agent answers, sets reminders, starts
investigations and can recommend what to approve. It never
approves anything, or asks for an approval. There is no general chat.

Implementation: `opsd/assistant.py`, `opsd/conversation.py`,
`opsd/inbound.py`, `opsd/tasks.py`, `opsd/chatcards.py`, `opsd/events.py`. Tests:
`tests/test_assistant.py`, `tests/test_full_chat.py`, `tests/test_one_chat.py`,
`tests/test_chat_ux.py`, `tests/test_chat_retention.py`, `tests/test_inbound.py`,
`tests/test_item_actions.py`.

## Conversations

- T1. **A conversation belongs to an item.** Its thread is `item:<item id>`
  for any item, or `ask:<item id>` for a question (T1a); the daemon derives
  it from the stored item record (`items.thread_of`) and never takes a
  thread id from a client or an event. The store accepts a turn only for
  the thread of an item it holds (a question's only as `ask:`, any other
  item's only as `item:`); the thread is kept
  in the turn's `meta.thread`, written by the store alone (no caller's meta
  can set or move it). The panel reaches a conversation only through its
  item's id (`api/items/<id>/thread|messages|...`, [panel.md](panel.md));
  any other thread is unreachable. Tests
  `test_only_conversations_of_stored_items_are_reachable`,
  `test_asking_about_an_item_runs_in_its_own_conversation`.
- T1a. **Questions.** `POST api/ask {text, client_id}` (Ask in the panel) or
  a push reply about nothing that still exists creates a question item (key
  `ask:<token>`, the token derived from the client id, so the same
  `client_id` is the same question) and sends the text as its first
  message. Its title is the admin's text on one line (120 characters, shown
  as text only); it is Running while its last message waits, Done with the
  answer (gist "answered HH:MM · N sources" in the daemon's words, the
  reply under `agent`) and Failed when the message went unanswered (gist
  "not answered: <the card's text>"); a follow-up in its conversation runs
  it again. It never needs the admin and is never pushed by the item book.
  Before a question is made, the client id is checked: a client id already
  used for a message in another conversation answers 409 `client_id_used`
  (also on `messages`) and leaves no question behind.
  Tests `test_a_question_is_an_item_that_opens_at_once`,
  `test_ask_checks_its_limits_before_it_makes_a_question`.
- T2. Senders: the admin in the panel (admin header and CSRF), or a push
  reply from the admin's device (below). A conversation's run (a "chat"
  run) always carries the
  origin `admin-chat`, which policy limits to the effects READ, SCHEDULE,
  NOTIFY, PROPOSE and RELOAD ([capabilities.md](capabilities.md) K5).
- T3. Limits: a message is at most 2000 characters (413 above; empty is
  400 and audited); 20 chat runs a day ([budget.md](budget.md) B1: a
  message past it is stored and gets a `run_cap_reached` card that retries
  it automatically, T5); one message in flight per thread from the panel (409
  `in_flight`); a job queue of 10 with a `busy` card on overflow. The run
  waits up to 25 minutes for the AI slot. Tests `test_chat_flood_is_capped`,
  `test_chat_budget_and_failure_reply`, `test_empty_and_long_messages`,
  `test_client_id_makes_sending_idempotent_and_one_message_in_flight`.
- T4. `client_id` (`[A-Za-z0-9_-]{8,64}`) makes a send idempotent per
  thread; a push reply derives it from the action id.
- T5. A blocked or failed run never goes silent: a `refused` or `failed`
  system card with daemon-written text. Refusals for usage limits, run-time
  caps, run caps, the daily cost cap, a busy account, a busy slot or the reserve auto-retry
  with a 15-minute backoff doubling up to 6 times, giving up when relief is
  more than 24 hours away; the admin may switch auto-retry off and on per
  card (re-enabling never extends the horizon). Manual retries are capped
  at 30 a day. A daemon restart marks queued and running turns
  `interrupted`. Tests `test_every_refusal_is_a_card_never_silence`,
  `test_run_failure_card_and_daily_cap_card`,
  `test_automatic_retry_runs_exactly_once`,
  `test_manual_and_automatic_retry_cannot_both_run`,
  `test_retry_backs_off_updates_one_card_then_gives_up`,
  `test_the_admin_can_turn_automatic_retry_off_and_on`,
  `test_retries_go_through_the_real_caps_and_kill_switch`.
- T6. Context for the AI: the conversation's last 8 turns (fenced, 1000 characters each),
  up to 20 open tasks, the daemon's context block (approved approvals,
  holds, open health items), 7 days of
  activity (`chat.activity_days`, at most 40 entries of the items'
  timelines: item id, time, kind, a needs-admin mark, title and the event's
  text, or a standalone entry's own words, all fenced) and the latest
  summary; angle brackets in untrusted text are replaced. Every 30 turns a
  capability-free `summary` run condenses all but the last 8 into a system
  turn; old turns are kept while the item is. A conversation's turns go
  with its item (a resolved item is pruned after 90 days,
  [items-and-pushes.md](items-and-pushes.md) I18). Tests
  `test_chat_prompt_lists_what_is_open_and_replies_cite_sources`,
  `test_a_long_thread_is_summarised_fenced_and_continues`,
  `test_the_summary_run_has_no_capabilities`,
  `test_a_conversation_goes_with_its_item`.
- T6a. **Ask about this item.** A message in an item's conversation
  (`POST api/items/<id>/messages`, or a push reply about it) keeps the
  item's id and title in the turn's meta, taken from the stored record
  (never from the request body), and that turn's run gets the item's record
  fenced in an `<item>` block (`items.item_context`: id, kind, state,
  title, gist, what it needs, about, up to 12 facts, the agent's earlier
  text marked as model output, the admin's actions on it; angle brackets
  replaced, the whole prompt redacted). The context turns are that
  conversation's only. It is an ordinary chat run: the `chat` profile, the
  admin-chat origin and every chat cap; it grants nothing new. A question's
  run gets no `<item>` block (the question is the message). Test
  `test_asking_about_an_item_runs_in_its_own_conversation`.
- T7. The reply schema is `{reply: null|<=1500, summary<=300}`; control
  characters are stripped and the strings `null`/`none` count as no reply.
  A null reply in chat is a `no_reply` failed card; in a follow-up it means
  no push. Actions are not in the JSON: they are `opsctl` calls during the
  run (`remind`, `tasks`, `cancel`, `investigate`,
  `propose-change`, `propose-fact`, `propose-rebase`). After the run the
  conversation gets one link card (a `system` turn, status `link`, `meta.item`
  the item's id, text "<kind>: <title>" in the daemon's words) per
  investigation, config change and fork rebase the run
  created; each is an item of its own and is decided there, never on the
  card. An investigation started
  from an item's or a question's conversation links back to it (its item's
  `follows`, [panel.md](panel.md)). The agent's own notes (activity
  entries, pushes from its own runs) never post into a conversation: they
  are items and History entries ([items-and-pushes.md](items-and-pushes.md)
  I16). Test `test_an_investigation_reports_back_in_its_thread`.
- T8. **Sources** are built from the run's audit rows, never from model
  text: at most 6 named sources (each read capability's fixed `source`
  name) with one validated detail each, plus the refused verbs; a footer
  of at most 240 characters. While a run works, the panel shows each
  capability's fixed `step` label, never model text or arguments. Tests
  `test_chat_prompt_lists_what_is_open_and_replies_cite_sources`,
  `test_sources_come_from_audit_rows_with_validated_details`,
  `test_step_labels_come_from_the_fixed_table`,
  `test_every_reachable_capability_has_its_labels`.
- T9. **Never approve.** No `approval.*` capability exists: a run can read
  the update plan and recommend, and an update that needs approval waits
  on its own item for the admin ([approvals.md](approvals.md) A2). Injected
  text that claims an approval approves nothing, and a run never asks for
  one. Test `test_no_run_can_approve`.
- T10. A reply is pushed only when the admin is not watching the
  conversation (presence lease), titled `Agent: <the item's title>` with the
  body cut at 180 characters, opening the item, with a Reply button into
  the same conversation and the item's id as its tag, so it replaces the
  push it answers (one push per item); for an item whose push is urgent the
  tag is `ha-ops-agent-<item id>`, so model text never replaces a daemon
  safety push. The stored reply keeps its full text. Presence is a
  20-second lease refreshed by `POST api/presence {item, visible}`; `read`
  sets the conversation's last read turn (kv `chat.read:<thread>`);
  `unread` is any later non-admin, non-summary turn. Tests
  `test_push_replies_land_on_the_item_the_push_was_about`,
  `test_reply_is_not_pushed_while_the_admin_watches`.
- T11. **Remember.** `POST api/items/<id>/remember {turn_id, text}` (text
  at most 500 characters, 50 a day) stores a fact and, with git on,
  proposes a `facts` change that appends `- <fact> (noted <day>)` under
  `## Noted through the ops agent` in the profile's facts document;
  without a repository the fact stays in the store and the response says
  `refused`. The AI's equivalent is `facts.propose`. Tests
  `test_remember_this_becomes_a_git_proposal`.

## Push replies

- T12. The Companion app's `mobile_app_notification_action` event arrives
  on the daemon's websocket subscription (no webhook). The admin identity
  is the user of the `mobile_app` config entry whose device name slugifies
  to the configured notify service, confirmed active, not system-generated
  and a `system-admin`; otherwise every event is refused and the runtime
  problem `notify.not_admin` fails the agent closed. The identity is
  re-confirmed every 4 minutes once known (every 10 until known, every 45
  seconds while a re-check is not answered, which keeps the last confirmed
  identity); the panel accepts it only within 5 minutes of the last
  confirmation, 15 while re-checks fail ([panel.md](panel.md) N2). Tests
  `test_admin_identity_from_registration`, `test_is_active_admin`.
- T13. An action is accepted only when: the admin is known; the event's
  `context.user_id` equals the admin id (constant time); `data.action`
  matches `OPS_(REPLY|SNOOZE|DONE)_[0-9a-f]{32}`; any source-device fields
  equal the registration; and the action id is consumed single-use and
  unexpired (3 days) in one transaction. The result is a `user.message`
  (REPLY) or `user.action` event with `dedupe_key = action:<id>`;
  duplicates pending or in flight are dropped; refusals are audited at most
  30 per window. Tests `test_verified_reply_becomes_an_event`,
  `test_spoofed_or_forged_events_are_refused`, `test_replay_and_expiry`,
  `test_unknown_admin_or_store_refuses_everything`,
  `test_the_real_ios_event_shape_is_accepted`,
  `test_a_named_device_may_be_the_registration_id_or_its_slug`,
  `test_gate_audits_refusals_with_a_limit_and_queues_verified`.
- T14. SNOOZE reopens a done task one day later; DONE closes a pending one.
- T14a. **A reply lands on the item the push was about.** The REPLY's
  conversation is the thread recorded with its action id when the push was
  sent (the daemon's record, consumed single use, T13): a Needs-you push and
  an agent push about an item carry that item's conversation. Without one
  the push's tag names the item (`<item id>` or `ha-ops-agent-<item id>`). A push about nothing that still exists
  starts a question (T1a). The turn is marked
  "via notification"; the answer follows T10. Test
  `test_push_replies_land_on_the_item_the_push_was_about`.

## Reminders and follow-ups

- T15. `task.create` (`remind`): kind `reminder` or `followup`; `at` is
  `+Nm`, `+Nh`, `+Nd` or a local `YYYY-MM-DDTHH:MM`; due between 1 minute
  and 366 days (admin owner) or 14 days (agent owner); at most 50 open tasks
  for the admin and 10 for the agent; follow-up chains at most 3 deep;
  title at most 120 and note 1000 characters; `reply` only for admin
  tasks; idempotent per run. `task.cancel` cancels only the caller's own
  pending tasks, never system tasks. Tests
  `test_chat_creates_a_reminder_that_fires_and_snoozes`,
  `test_a_firing_is_delivered_once_even_after_a_crash`,
  `test_tasks_survive_a_restart`, `test_bad_reminder_requests_are_refused`,
  `test_cancel_and_system_tasks`, `test_parse_at_local_time`,
  `test_parse_at_days_are_local_calendar_days_across_dst`.
- T16. A due reminder pushes "Reminder: ..." (or "Agent-scheduled: ...")
  with Done and "Snooze 1 day" buttons, a Reply button only for admin
  tasks with `reply`, and quiet-ok only for admin reminders. A due
  follow-up runs the `followup` profile with the creator's origin (3 a day,
  retried after 6 hours, at most 3 attempts) and pushes its reply as
  "Follow-up: ...", opening the entry's own item (its words under `agent`),
  whose conversation a Reply continues; a reminder with `reply` opens the
  reminder's item and Reply continues its conversation. The agent's own tasks (owner `agent`) push only when
  created with a push class (`reason`, [capabilities.md](capabilities.md)
  K11); with `note` the reminder, or the follow-up's result, is a quiet
  History note (activity kind `note`, the words under `agent`, never a
  need). Their pushes never carry a Reply button. Tests
  `test_reminders_the_admin_set_ring_but_agent_ones_wait`,
  `test_the_agents_own_follow_up_pushes_only_with_a_reason_and_never_offers_a_reply`.
  Firing is at-least-once with a `mark_fired` guard. A
  reminder set from an item's Remind me action (maintenance.md M13a) opens
  that item.

## Investigations

- T17. `task.investigate` (admin origins only; 1 per run, 4 a day; steps
  1..3, default 2) needs a chat run in a conversation, one open
  investigation per conversation and two house-wide. It is a `followup` task with
  `payload.investigation`. Each step is an `investigate` run; one system
  card with status `investigation` is rewritten in place (state, step,
  actions `cancel`); each step's findings are an agent turn with sources;
  summaries are kept (400 characters, at most 3); the next step follows 5
  seconds later while `next` is non-null; a blocked step waits at most 3
  times within 24 hours; the final push "Investigation: <title>" goes out
  unless the admin is watching, a failure pushes "Investigation stopped";
  both open and are tagged with the conversation's item (T10). Its own item
  (kind `investigation`) follows the item it was started from.
  `POST api/items/<id>/cancel {task_id}` cancels the pending task of that
  conversation or kills the running step.
  Tests `test_an_investigation_reports_back_in_its_thread`,
  `test_an_investigation_can_be_cancelled_before_and_during_a_step`,
  `test_investigation_limits`, `test_a_blocked_step_waits_then_stops`,
  `test_cancel_while_waiting_for_the_slot_never_starts_the_step`.
- T17a. **Plan a fix** (a maintenance item's `plan_fix` in the panel) starts
  the same kind of investigation in that item's own conversation for the
  admin (owner admin, origin admin-chat, 2 steps, `payload.plan_fix`) with
  a fixed daemon brief and the item id in its payload; each step's prompt
  holds the item's `<item>` block (T6a). It has no item of its own: the
  finding itself is Running while it runs, with the phases "Read the
  evidence" and "Write the plan" and a Stop action, its plan becomes the
  finding's `agent` text, and a proposal it makes is decided on the finding
  ([maintenance.md](maintenance.md) M13b). The last step's push is "Plan
  ready: <the finding's title>" unless the finding now asks (the item
  book's push says so). The last step's findings are shown once, as the
  finding's plan: the conversation gets no agent turn for them, only its
  investigation card, which then reads "Plan ready: it is above, under
  The agent says." (earlier steps' findings stay agent turns). It obeys T17's limits (one open investigation per
  conversation, two house-wide, the open-task cap), never uses
  `task.investigate`'s quota (which the agent's chat investigations use),
  and its runs may use the 2 investigate runs kept for the admin
  ([budget.md](budget.md) B1). The same item, finding fingerprint and day
  again is a duplicate (no second run), even after the plan moved the
  item's revision; refused, the action
  answers 409 with the reason. A step a cap refuses runs nothing: the
  finding is Scheduled (never Running, no Stop) at its retry time, gist
  "Plan a fix queued: <the limit>. Retrying automatically at <time>." (one
  statement), with **Cancel the plan** (`cancel`); the step runs again by
  itself when the limit resets (at most 3 waits, at most a day ahead). The
  line is composed when shown from the stored reason code and retry time,
  never from stored words. Tests `test_plan_a_fix_runs_on_the_finding_itself`,
  `test_stop_on_a_running_plan_cancels_it`,
  `test_a_capped_plan_waits_queued_and_retries_by_itself`.
- T17b. **A plan's verdict.** The last step of Plan a fix also answers
  `verdict` (`{verdict, reason, evidence}`, or null; every other
  investigation answers null). The daemon keeps it as the finding's verdict
  ([maintenance.md](maintenance.md) M13d) for the fingerprint the item
  showed when the plan started, by `plan_fix`: a plan that concludes
  nothing is needed (`not_affected`) closes the finding (within the
  guards) and it stays closed until the fingerprint changes; a "No fix
  needed" plan never lands in Needs you. `needs_owner` needs its
  `owner_action` (maintenance.md M13d), else it is kept as `unknown`.
  Tests `test_a_not_affected_plan_closes_the_finding_and_is_remembered`,
  `test_a_no_fix_needed_plan_never_lands_in_needs_you`.
