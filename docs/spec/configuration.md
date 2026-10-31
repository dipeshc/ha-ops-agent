# Configuration options

The add-on options hold behaviour; the [house profile](profile.md) holds
what is specific to a home.

Implementation: `opsd/config.py` (`Options`, `Paths`, `RuntimeProblems`),
`ha_ops_agent/config.yaml` (schema and defaults), `translations/en.yaml`
(labels), `opsd/quiet.py`. Tests: `tests/test_options.py`,
`tests/test_quiet.py`, `tests/test_infra.py` `test_options_parsing_and_token`.

## Loading and validation

- C1. Options are read from `/data/options.json` once at daemon start. A
  missing file means defaults; an unreadable or non-object file records a
  problem.
- C2. The `config.yaml` schema and the daemon's own checks MUST agree: the
  type and range of every key is checked again in code, and keys marked
  optional (`?`) are absent from `options:` except `claude.token`. Test
  `test_schema_matches_the_dataclasses`. Every group and field has a label in
  `translations/en.yaml` (`test_translations_name_every_option`).
- C3. A bad value keeps its default and records one problem that never echoes
  the value. A list keeps its valid entries and drops the invalid ones, with
  one problem. Tests `test_problems_never_echo_values`,
  `test_tighten_only_lists_keep_their_valid_entries`.
- C4. **Fail closed.** With any problem (options, profile, or a runtime
  problem), `Options.valid` is false and: `acting` is false whatever
  `agent.mode` says, `auto_updates_on` is false whatever
  `updates.auto_install` says, the AI preflight refuses every run
  (`options_invalid`), and the daily check raises a warning item
  `options:invalid`. `agent.mode` and `llm.provider` themselves are not
  rewritten. Tests `test_bad_values_fail_closed`,
  `test_invalid_options_raise_a_health_item`,
  `test_a_health_check_with_invalid_options_reports_them_without_ai`,
  `test_preflight_refusals_never_start_claude`.
- C5. Runtime problem: when the push device belongs to a user who is not an
  active Home Assistant administrator, the agent fails closed as in C4 and
  sends no push at all, safety pushes included. Test
  `test_a_notify_device_of_a_non_admin_fails_closed`.

## Options

| Key | Default | Check | Effect |
| --- | --- | --- | --- |
| `agent.mode` | `observe` | `observe` / `act` | `observe` reports only; `act` lets the daemon install updates (with `updates.auto_install`), make backups and reload allowed integrations. |
| `llm.provider` | `claude` | `claude` / `none` | `none`: no AI runs; deterministic checks, pushes and the update rule continue, but with `updates.hold_if_unreviewed` on (the default) every AUTO candidate is held for approval because nothing reviewed it. |
| `claude.token` | empty | secret, not pattern-checked | Provider credential: a setup token or an API key; see [ai-runner.md](ai-runner.md) AP1. Falls back to `/data/secrets/claude_oauth_token`. No token: AI runs are refused (`no_token`) and the deterministic fallback is used. |
| `claude.plan` | `auto` | `auto` / `pro` / `max5` / `max20` | Subscription tier for the usage-share estimate. Ignored with an API key. |
| `claude.model_standard` | `claude-sonnet-5-5` | `[a-z0-9][a-z0-9.-]{1,63}` | Model for every run profile except summaries. |
| `claude.model_fast` | empty | same pattern or empty | Model for the summary profile; empty means `model_standard`. |
| `chat.activity_days` | 7 | 1..30 | Chat activity retention. |
| `notify.service` | empty | `(mobile_app_[a-z0-9_]+)?` | Push target; empty means the profile's `notify_service`. |
| `notify.max_pushes_per_day` | 3 | 0..20 | Daily cap for the normal push bucket only (see [items-and-pushes.md](items-and-pushes.md)). |
| `notify.quiet_start`, `notify.quiet_end` | `22:30`, `07:00` | `HH:MM` or empty | Quiet hours; see C6. |
| `health.check_time` | `05:30` | `HH:MM` | Daily health check time (house time zone). |
| `health.expected_unavailable` | `[]` | entity ids | Entities that are normally unavailable and are not news, in the daily check, `opsctl health --diff` and the update health compares. Locks, alarm panels, covers that are not positively a blind, shade, curtain, awning or shutter, and anything named like access hardware are never silenced ([health.md](health.md) H11a). |
| `updates.window` | `04:00` | `HH:MM` | Nightly update window. |
| `updates.auto_install` | `off` | `off` / `on` | Install AUTO-tier updates in the window; needs `agent.mode: act`. |
| `updates.min_age_days` | 7 | 7..90 | Patch soak; minors soak twice as long. Code floors it at 7 again. |
| `updates.review` | `true` | bool | The AI reads AUTO candidates' release notes and may hold one back, never promote. |
| `updates.hold_if_unreviewed` | `true` | bool | An AUTO candidate the AI could not review is held. |
| `updates.need_approval`, `updates.never` | `[]` | `update.[a-z0-9_]{1,128}` | Always ask / never install (still reported). |
| `capabilities.require_approval`, `capabilities.never` | `[]` | `[a-z][a-z0-9_]{0,40}.[a-z0-9_.]{1,60}` | Tighten a capability's tier by name; never loosens. `never: [dashboard.save]` refuses even an approved dashboard plan. |
| `capabilities.reloadable_domains` | empty | up to 40 comma-separated `[a-z0-9_]{1,64}` | Reload allowlist; empty means the profile's `reload.domains`. |
| `usage.max_share_percent` | 5.0 | 0.5..25 | Share of a subscription usage window the agent may use (clamped again in the budget). |
| `limits.run_max_turns` | 30 | 5..100 | Turn cap of the triage profile. |
| `limits.run_max_usd` | 1.5 | 0.1..20 | Cost cap of the triage profile; other profiles use the smaller of their fixed cap and this. |
| `limits.log_level` | `info` | `debug` / `info` / `warning` / `error` | Daemon log level. |
| `limits.git_push` | `true` | bool | `false`: applied proposals are committed but not pushed. |

Which storage dashboards the agent must never edit is the house profile's
`dashboards.generated` ([profile.md](profile.md)); there is no option for
it. Dashboard changes need `agent.mode: act` and an approval
([maintenance.md](maintenance.md) M22).

## Quiet hours

- C6. Quiet hours are on when both `quiet_start` and `quiet_end` are set and
  differ; the window may wrap midnight. A window longer than 12 hours turns
  quiet hours off with one warning at start; it is not an options problem and
  does not fail closed. Urgent pushes, pushes marked quiet-ok and safety-class
  pushes bypass the window; everything else waits until it ends. Tests
  `test_window_parsing_and_off`, `test_windows_longer_than_12_hours_are_refused`,
  `test_a_too_long_window_disables_quiet_hours_with_a_warning`.

## Paths

`Paths` fixes the add-on's file layout: the config mount `/mnt/ha/config`,
the backup mount `/mnt/ha/backup`, `/data` (options, the store, secrets, runs,
snapshots and the dashboard backups under `maintenance/dashboards`), the run copy and drafts under `/run`, and the opsctl socket.
See [privsep.md](privsep.md) and [storage.md](storage.md).
