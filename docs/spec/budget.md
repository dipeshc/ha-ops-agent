# The AI usage budget and the kill switch

The AI is bounded by daily run caps, per-run turn and cost caps, a share of
the account's usage windows, a memory guard and a kill switch. None of
these touches the deterministic checks, pushes or updates.

Implementation: `opsd/budget.py`, `opsd/usage.py`, `opsd/aicontrol.py`,
`opsd/store.py` (`reserve_run`, `finish_run`, `usage`). Tests:
`tests/test_usage.py`, `tests/test_usage_estimates.py`, `tests/test_runner.py`.

## Caps

- B1. Daily run caps per profile: triage 6, chat 20, followup 3,
  investigate 6, summary 6, update_review 2 (others 3); refusal
  `run_cap_reached:<profile>`. Chat messages are separately capped at 30 a
  day. Tests `test_usage.py:99-143`, `test_chat_flood_is_capped`.
- B2. Per-run turn and cost caps come from the profile table in
  [ai-runner.md](ai-runner.md); the cost cap is passed to the provider on
  every run.
- B3. **Share rule.** `usage.max_share_percent` (default 5, clamped 0.5..25)
  is the agent's share of each account usage window; `share_stop` is 80 %
  of it. A run is refused when the agent's 5-hour share plus the profile's
  typical share (mean of its last 5 measured runs, default 0.5 %) exceeds
  `share_stop` (`share_cap:five_hour`), likewise for the 7-day window
  (default typical 0.1 %, `share_cap:seven_day`), when the account's own
  utilisation in the last hour is at least 90 % (`account_busy:<window>`),
  or when the time reservation falls under 60 seconds
  (`share_cap:run_time`). Tests `test_usage.py:46-53,121-143`.
  **Exemption:** with a provider API key ([ai-runner.md](ai-runner.md)
  AP1a) there are no subscription windows, so none of B3, B4 or B5
  applies: no `share_cap:*`, `account_busy:*` or `share_reserve:*`
  refusal, and the run time is the profile's reserve (R3). B1, B2 and the
  per-run cost cap still apply. Test
  `test_an_api_key_skips_the_share_rule_but_not_the_run_caps`.
- B4. Plan tiers for the time estimate: `claude.plan` `pro` 40, `max5` 140,
  `max20` 240 hours a week (low-end estimates); the 5-hour window is 20 %
  of the week; `auto` uses the provider's plan hint, else `pro`. Week
  seconds are `hours x 3600 x share`. Tests `test_usage.py:56-61`,
  `test_usage_estimates.py:32-40`.
- B5. User-class runs (chat, investigate, summary) are refused with
  `share_reserve:<window>` once either window's share reaches half of
  `share_stop`, so agent-class runs (triage, followup, update review) keep
  a reserve. Test `test_full_chat.py:489-515`.

## Measurement

- B6. A run's share is the difference between the last and first
  utilisation readings of the provider's rate-limit events (values over 1
  read as percent, otherwise as a fraction), one unit per run. When both
  readings exist and did not move, the share is estimated as the run's
  duration over the window and flagged `share_estimated`. Tests
  `test_usage.py:64-78,454-463`, `test_usage_estimates.py:32-65`.
- B7. **Reservation.** One transaction inserts the `runs` row with reserved
  turns, cost, seconds and share before the provider starts; `finish_run`
  replaces them with actuals; an aborted run with no tokens keeps a quarter
  of the reservation; a run that is not ok is charged at least 0.25 USD; a
  run the daemon lost (crash) is closed as `interrupted` at its full
  reservation. Tests `test_usage.py:84-96,146-151`, `test_runner.py:131-137`.
- B8. Exposure: `sensor.ops_agent_ai` (state `enabled`, `ai_disabled` or
  `usage_limited`; attributes plan, caps, shares, run time, today's totals,
  per-profile counts, last run), `sensor.ops_agent_runs_today`, `GET
  api/usage` and the `usage` block of `GET api/state`. With an API key the
  plan, `share_stop_percent` and the run-time caps are null. Test
  `test_usage.py:324-334`.

## What is skipped when exhausted

- B9. Any run whose reservation fails is skipped. A blocked chat turn gets a
  refused card and auto-retries ([chat.md](chat.md)). A blocked
  investigation step waits, then stops. Blocked triage still gets the
  deterministic safety push ([health.md](health.md) H17). A blocked update
  review holds the AUTO candidates when `updates.hold_if_unreviewed` is on.
- B10. After `usage_limited` the daemon backs off 1 hour, doubling to 24, or
  until the provider's reset time if within 24 hours; a successful run
  resets the level. No push. Tests `test_usage.py:240-252,317-321`.

## The kill switch

- B11. The helper `input_boolean.ops_agent_ai_enabled` is created over the
  websocket if missing (on at first creation; a recreated one starts off
  with a notice) and watched through the daemon's one event stream. Tests
  `test_usage.py:366-392,484-527`.
- B12. Off, from anyone, or the helper unknown, unavailable or missing once
  it exists, kills the running AI (the whole process group; status
  `killed`, charged the floor) and refuses new runs (`ai_disabled`). On by
  anyone but the verified admin or the daemon is reverted: killed, turned
  off again, audited, one notice a day. `POST api/ai {on: false}` turns it
  off at once; `{on: true}` is an admin-only panel action. Tests
  `test_usage.py:224-238,375-411,417-451`, `test_panel_resume_is_the_admins_act`,
  `test_no_admin_known_means_no_one_can_enable`.
- B13. Switch state, creation, backoff level and deadline persist in the
  store's `kv`; every change is audited on channel `ai-control`.
  Deterministic health checks, liveness and safety pushes never consult the
  switch. Test `test_usage.py:441`.
