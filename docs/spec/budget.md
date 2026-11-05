# The AI usage budget and the kill switch

The AI is bounded by daily run caps, per-run turn and cost caps, one
rolling run-time budget, an optional daily cost cap, a memory guard and a
kill switch. None of these touches the deterministic checks, pushes or
updates.

Implementation: `opsd/budget.py`, `opsd/usage.py`, `opsd/aicontrol.py`,
`opsd/store.py` (`reserve_run`, `finish_run`, `add_cost`, `usage`). Tests:
`tests/test_usage.py`, `tests/test_usage_estimates.py`, `tests/test_runner.py`.

## Caps

- B1. Daily run caps per profile: triage 6, chat 20, followup 3,
  investigate 6, summary 6, update_review 2, maintenance_plan 3 (the scan's
  own check; any other profile 3); refusal
  `run_cap_reached:<profile>`, whose card names the actual limit ("Today's
  limit for investigations and Plan a fix is reached (6 runs a day, the
  last 2 kept for your Plan a fix taps)"). Of the investigate cap, 2 runs
  are kept for the admin's own Plan a fix taps (`OWNER_RESERVED_RUNS`):
  other investigations stop at 4, a Plan a fix run may use all 6, so the
  agent's runs never starve what the admin asked for and the cap, the
  run-time budgets and the cost cap are unchanged. These are the only
  daily caps on AI runs: a chat message, a follow-up and a Plan a fix tap
  have no separate daily count. Tests `test_each_cap_refuses`,
  `test_chat_flood_is_capped`,
  `test_the_maintenance_check_has_its_own_visible_daily_cap`,
  `test_the_agents_investigations_never_use_the_runs_kept_for_plan_a_fix`.
- B2. Per-run turn and cost caps come from the profile table in
  [ai-runner.md](ai-runner.md); the cost cap is passed to the provider on
  every run.
- B3. **Run-time budget.** `B = plan hours x 3600 x
  usage.max_share_percent / 100` seconds of AI run time per rolling 7 days,
  and `B / 5` per rolling 5 hours (`usage.max_share_percent` default 5,
  clamped 0.5..25). It is the agent's own wall-clock run time, labelled as
  such; it is not a measured share of the subscription. Every run stops at
  80 % of each window's budget: a run is refused when less than 60 seconds
  of it is left (`share_cap:five_hour`, `share_cap:seven_day`, the window
  with less left), and otherwise the run's reserved time (its timeout) is
  trimmed to what is left. A run is also refused when the account's own
  measured utilisation in the last hour is at least 90 % of either window
  (`account_busy:<window>`). Tests `test_each_cap_refuses`,
  `test_a_reservation_is_trimmed_to_the_budget_left_and_user_runs_stop_at_half`.
  **Exemption:** with a provider API key ([ai-runner.md](ai-runner.md)
  AP1a) there are no subscription windows, so neither B3 nor B5 applies:
  no `share_cap:*`, `account_busy:*` or `share_reserve:*` refusal, and the
  run time is the profile's reserve (R3). B1, B2, B3a and the per-run cost
  cap still apply. Test
  `test_an_api_key_skips_the_share_rule_but_not_the_run_caps`.
- B3a. **Daily cost cap.** `limits.daily_max_usd` (default unset or 0: no
  cap) bounds the reported cost of the day's runs (`total_cost_usd`, kept
  as a `kv` daily counter in millionths of a USD). A run's cost cap is
  trimmed to what is left of it, and a run is refused (`cost_cap:day`) when
  less than 0.05 USD is left; it applies with any credential. Unset, the
  worst case is bounded only by B1 and B2: about 50 runs a day at their
  per-run caps (6 triage x `run_max_usd`, 20 chat x 0.5, 6
  investigations x 1.0, 6 summaries x 0.2, 3 follow-ups x 0.5, 2 update
  reviews x 0.5 and 3 maintenance plans x 0.5), 30.2 USD a day with
  the defaults (plus whatever a last turn adds past a per-run cap), and
  more when `run_max_usd` is raised. Test
  `test_the_daily_cost_cap_trims_each_run_and_refuses_when_spent`.
- B4. Plan tiers: `claude.plan` `pro` 40, `max5` 140, `max20` 240 hours
  a week (low-end estimates); any other value (the legacy `auto`) is read
  as `pro`, the default. Tests `test_share_option_scales_run_time_and_is_clamped`,
  `test_a_legacy_auto_plan_loads_as_pro`.
- B5. User-class runs (chat, investigate, summary) stop at half of each
  window's stop line (40 % of the budget; refusal `share_reserve:<window>`,
  and their reserved time is trimmed to that line), so agent-class runs
  (triage, followup, update review, maintenance plan) always keep at least
  the other half of the stop line. Tests `test_chat_leaves_half_the_share_to_monitoring`,
  `test_chat_at_its_cap_leaves_triage_half_the_stop_line_on_the_default_plan`.

## Measurement

- B6. A run is charged its run time: its `duration_s` when it finished,
  its reservation while it runs. The account utilisation that B3's
  `account_busy` compares is the last reading of the provider's rate-limit
  events, in the unit of its payload (`budget.reading_unit`, judged over
  every window of it, because Claude Code is Node and writes 1.0 as `1`):
  any non-integer value makes the whole payload fractions of the window;
  otherwise any value over 1 makes it percents; a payload whose values are
  all 0 or 1 is ambiguous and is read in the unit learned from the last
  unambiguous one (kv `usage.unit`), or as percents with a warning when
  none was learned. A boolean, a string, NaN, infinity or a negative value
  is no reading; over the limit counts as a full window (100 %). The
  "claude run finished" log line carries each window's reading exactly as
  reported with its JSON type (`{"value", "type"}`) and the payload's unit
  (`fraction`, `percent` or `ambiguous`). Tests
  `test_utilisation_parsing`, `test_the_utilisation_unit_is_judged_over_the_whole_payload`,
  `test_an_ambiguous_reading_uses_the_unit_learned_last`, `test_stream_rate_limit_events`,
  `test_a_run_is_charged_its_run_time_whatever_the_utilisation_says`.
- B7. **Reservation.** One transaction inserts the `runs` row with reserved
  turns and seconds before the provider starts; `finish_run` replaces them
  with actuals; an aborted run with no tokens keeps a quarter of the
  reservation; a run the daemon lost (crash) is closed as `interrupted` at
  its full reservation. A run reserves no tokens. A
  finished run's reported cost is added to the day's cost counter (B3a).
  Tests `test_reservation_replaced_by_actuals`,
  `test_concurrent_reservations_cannot_both_take_the_last_slot`,
  `test_crashed_runs_keep_their_reservation`,
  `test_failed_run_charges_its_reservation_and_one_slot`.
- B8. Exposure: `sensor.ops_agent_ai` (state `enabled`, `ai_disabled` or
  `usage_limited`; attributes `run_time_5h_s`, `run_time_5h_cap_s`,
  `run_time_7d_s`, `run_time_7d_cap_s` (the caps are the 80 % stop lines
  of B3), `account_5h_percent`, `account_7d_percent` (null when not
  measured in the last hour), `cost_today_usd`, `daily_max_usd`, today's
  totals, per-profile counts, last run), `sensor.ops_agent_runs_today` and
  the `usage` block of `GET api/state`. With an API key the run-time caps
  are null. Tests `test_sensors_are_graphable`,
  `test_an_api_key_shows_no_window_caps`.

## What is skipped when exhausted

- B9. Any run whose reservation fails is skipped. A blocked chat turn gets a
  refused card and auto-retries ([chat.md](chat.md)). A blocked
  investigation step waits, then stops. Blocked triage still gets the
  deterministic safety push ([health.md](health.md) H17). A blocked update
  review holds the AUTO candidates when `updates.hold_if_unreviewed` is on.
- B10. After `usage_limited` the daemon backs off 1 hour, doubling to 24, or
  until the provider's reset time if within 24 hours; a successful run
  resets the level. No push. Tests `test_kill_switch_and_backoff`,
  `test_usage_limit_backs_off_without_a_push`.

## The kill switch

- B11. The helper `input_boolean.ops_agent_ai_enabled` is created over the
  websocket if missing (on at first creation; a recreated one starts off
  with a notice) and watched through the daemon's one event stream. Tests
  `test_kill_switch_events_and_sync`,
  `test_a_deleted_helper_is_off_at_once_and_recreated`,
  `test_one_websocket_carries_taps_and_the_switch`.
- B12. Off, from anyone, or the helper unknown, unavailable or missing once
  it exists, kills the running AI (the whole process group; status
  `killed`, charged the floor) and refuses new runs (`ai_disabled`). On by
  anyone but the verified admin or the daemon is reverted: killed, turned
  off again, audited, one notice a day. `POST api/ai {on: false}` turns it
  off at once; `{on: true}` is an admin-only panel action. Tests
  `test_kill_switch_and_backoff`,
  `test_kill_switch_stops_a_running_triage_but_not_the_safety_push`,
  `test_panel_resume_is_the_admins_act`,
  `test_no_admin_known_means_no_one_can_enable`.
- B13. Switch state, creation, backoff level and deadline persist in the
  store's `kv`; every change is audited on channel `ai-control`.
  Deterministic health checks, liveness and safety pushes never consult the
  switch. Test
  `test_kill_switch_stops_a_running_triage_but_not_the_safety_push`.
