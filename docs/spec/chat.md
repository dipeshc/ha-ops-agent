# Chat, push replies, reminders and investigations

There is one chat, for the admin. It answers questions, sets reminders,
starts investigations and can ask the admin to approve something. It never
approves anything.

Implementation: `opsd/assistant.py`, `opsd/conversation.py`,
`opsd/inbound.py`, `opsd/tasks.py`, `opsd/proposals.py` (approval
requests), `opsd/chatcards.py`, `opsd/events.py`. Tests:
`tests/test_assistant.py`, `tests/test_full_chat.py`, `tests/test_one_chat.py`,
`tests/test_chat_ux.py`, `tests/test_chat_retention.py`, `tests/test_inbound.py`.

## One chat

- T1. One thread, stored under the kv key `chat.main_thread`, created on
  demand; every push reply continues it; the panel answers 404 for any
  other thread id. Tests `test_only_the_one_chat_is_reachable_in_the_panel`,
  `test_push_replies_with_a_reminder_tag_continue_the_one_chat`.
- T2. Senders: the admin in the panel (admin header and CSRF), or a push
  reply from the admin's device (below). A chat run always carries the
  origin `admin-chat`, which policy limits to the effects READ, SCHEDULE,
  NOTIFY, PROPOSE and RELOAD ([capabilities.md](capabilities.md) K5).
- T3. Limits: a message is at most 2000 characters (413 above; empty is
  400 and audited); 30 turns a day (the message is stored, a `chat_cap`
  card is written and one "Chat limit" push goes out per day); 20 chat
  runs a day; one message in flight per thread from the panel (409
  `in_flight`); a job queue of 10 with a `busy` card on overflow. The run
  waits up to 25 minutes for the AI slot. Tests `test_chat_flood_is_capped`,
  `test_chat_budget_and_failure_reply`, `test_empty_and_long_messages`,
  `test_client_id_makes_sending_idempotent_and_one_message_in_flight`.
- T4. `client_id` (`[A-Za-z0-9_-]{8,64}`) makes a send idempotent per
  thread; a push reply derives it from the action id.
- T5. A blocked or failed run never goes silent: a `refused` or `failed`
  system card with daemon-written text. Refusals for usage limits, share
  caps, run caps, a busy account, a busy slot or the reserve auto-retry
  with a 15-minute backoff doubling up to 6 times, giving up when relief is
  more than 24 hours away; the admin may switch auto-retry off and on per
  card (re-enabling never extends the horizon). Manual retries are capped
  at 30 a day. A daemon restart marks queued and running turns
  `interrupted`. Tests `test_chat_ux.py:93-311`.
- T6. Context for the AI: the last 8 turns (fenced, 1000 characters each),
  up to 20 open tasks, the daemon's context block (pending and approved
  approvals, open approval requests, holds, open health items), 7 days of
  activity (`chat.activity_days`, at most 40 entries) and the latest
  summary; angle brackets in untrusted text are replaced. Every 30 turns a
  capability-free `summary` run condenses all but the last 8 into a system
  turn; old turns are kept and threads are never pruned. Tests
  `test_full_chat.py:376-411`, `test_chat_ux.py:341-381`,
  `test_the_main_chat_is_never_pruned`.
- T7. The reply schema is `{reply: null|<=1500, summary<=300}`; control
  characters are stripped and the strings `null`/`none` count as no reply.
  A null reply in chat is a `no_reply` failed card; in a follow-up it means
  no push. Actions are not in the JSON: they are `opsctl` calls during the
  run (`remind`, `tasks`, `cancel`, `investigate`, `request-approval`,
  `propose-change`, `propose-fact`). After the run the daemon attaches
  cards for investigations and approval requests the run created.
- T8. **Sources** are built from the run's audit rows, never from model
  text: at most 6 named sources with one validated detail each, plus the
  refused verbs; a footer of at most 240 characters. Tests
  `test_full_chat.py:376-439`.
- T9. **Ask, never approve.** `approval.request` (2 per run, 6 a day)
  creates a row in `approval_proposals` with the versions and title from
  the stored plan, never from the model; it refuses the agent and the
  Supervisor, non-approvable tiers, entities not in the plan, a live
  approval, a version rejected within 7 days and more than 5 open; it
  expires after 72 hours; `backup` asks for a backup. It settles when the
  admin acts in the panel and is voided when the plan stops offering that
  version. No other `approval.*` capability exists. Tests
  `test_full_chat.py:101-235`.
- T10. A reply is pushed only when the admin is not watching the thread
  (presence lease), as `Agent: Reply` with the body cut at 180 characters
  and a Reply button; the stored reply keeps its full text. Presence is a
  20-second lease refreshed by `POST api/presence`; `read` sets the last
  read turn; `unread` is any later non-admin, non-summary turn.
- T11. **Remember.** `POST api/threads/<t>/remember {turn_id, text}` (text
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
  problem `notify.not_admin` fails the agent closed. Tests
  `test_inbound.py:121-148`.
- T13. An action is accepted only when: the admin is known; the event's
  `context.user_id` equals the admin id (constant time); `data.action`
  matches `OPS_(REPLY|SNOOZE|DONE)_[0-9a-f]{32}`; any source-device fields
  equal the registration; and the action id is consumed single-use and
  unexpired (3 days) in one transaction. The result is a `user.message`
  (REPLY) or `user.action` event with `dedupe_key = action:<id>`;
  duplicates pending or in flight are dropped; refusals are audited at most
  30 per window. Tests `test_inbound.py:52-120,150-187`.
- T14. SNOOZE reopens a done task one day later; DONE closes a pending one.

## Reminders and follow-ups

- T15. `task.create` (`remind`): kind `reminder` or `followup`; `at` is
  `+Nm`, `+Nh`, `+Nd` or a local `YYYY-MM-DDTHH:MM`; due between 1 minute
  and 366 days (admin owner) or 14 days (agent owner); at most 50 open tasks
  for the admin and 10 for the agent; follow-up chains at most 3 deep;
  title at most 120 and note 1000 characters; `reply` only for admin
  tasks; idempotent per run. `task.cancel` cancels only the caller's own
  pending tasks, never system tasks. Tests `test_assistant.py:63-208`.
- T16. A due reminder pushes "Reminder: ..." (or "Agent-scheduled: ...")
  with Done and "Snooze 1 day" buttons, a Reply button only for admin
  tasks with `reply`, and quiet-ok only for admin reminders. A due
  follow-up runs the `followup` profile with the creator's origin (3 a day,
  retried after 6 hours, at most 3 attempts) and pushes its reply as
  "Follow-up: ...". Firing is at-least-once with a `mark_fired` guard.

## Investigations

- T17. `task.investigate` (admin origins only; 1 per run, 4 a day; steps
  1..3, default 2) needs a chat run with a thread, one open investigation
  per chat and two house-wide. It is a `followup` task with
  `payload.investigation`. Each step is an `investigate` run; one system
  card with status `investigation` is rewritten in place (state, step,
  actions `cancel`); each step's findings are an agent turn with sources;
  summaries are kept (400 characters, at most 3); the next step follows 5
  seconds later while `next` is non-null; a blocked step waits at most 3
  times within 24 hours; the final push "Investigation: <title>" goes out
  unless the admin is watching, a failure pushes "Investigation stopped".
  `POST api/threads/<t>/cancel {task_id}` cancels the pending task or kills
  the running step and voids its approval requests. Tests
  `test_full_chat.py:271-375`.
