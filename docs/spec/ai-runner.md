# The AI runner and the AI provider

The AI is a bounded advisor: it reads a secret-free copy of the
configuration, asks the daemon for named actions through `opsctl`, and
returns a schema-checked result. The daemon decides whether to act on any
of it. This document has two parts: the provider-independent run model, and
the one provider the 0.19.0 code implements.

Implementation: `opsd/runner.py` (profiles, preflight, run life cycle),
`opsd/runctx.py`, `opsd/guard.py` and `bin/guard-hook` (the tool guard),
`opsd/claude_runner.py` (the provider), `prompts/`. Tests:
`tests/test_runner.py`, `tests/test_guard.py`, `tests/test_claude_runner.py`,
`tests/test_hardening.py`, `tests/test_privsep.py`.

## Run profiles

| Profile | Prompt | Schema | Capabilities | Class | Turns | Cost cap (USD) | Drafts |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `triage` | `profiles/triage.md` | `triage-result` | reads + `task.create`, `task.list`, `baseline.ack`, `notify.admin`, `suppress.propose`, `integration.reload` | AGENT | `limits.run_max_turns` (30) | `limits.run_max_usd` (1.5) | no |
| `chat` | `profiles/chat.md` | `reply-result` | reads + `task.create/list/cancel`, `task.investigate`, `approval.request`, `change.propose`, `facts.propose`, `suppress.propose`, `integration.reload` | USER | 15 | min(0.5, run_max_usd) | yes |
| `followup` | `profiles/followup.md` | `reply-result` | reads + `task.create/list/cancel`, `approval.request` | AGENT | 15 | min(0.5, run_max_usd) | no |
| `investigate` | `profiles/investigate.md` | `investigate-result` | reads + `task.list`, `approval.request`, `change.propose`, `facts.propose`, `suppress.propose`, `integration.reload` | USER | 25 | min(1.0, run_max_usd) | yes |
| `summary` | `profiles/summary.md` | `reply-result` | none | USER | 5 | min(0.2, run_max_usd) | no |
| `update_review` | `profiles/update_review.md` | `update-review` | none | AGENT | 6 | min(0.5, run_max_usd) | no |
| `maintenance_plan` | `profiles/maintenance_plan.md` | `maintenance-plan` | none | AGENT | 8 | min(0.5, run_max_usd) | no |

- R1. "Reads" are the capabilities whose only effect is READ
  ([capabilities.md](capabilities.md)). A profile's capability list is the
  run's grant; the daemon refuses any other verb. `summary` uses
  `claude.model_fast` when set, every other profile `claude.model_standard`.
  Test `test_profiles_reference_enabled_capabilities`.
- R2. The system prompt is `prompts/base.md` plus the profile file, with the
  house documents list, the proposals section (`proposals.md` or
  `proposals-off.md`), the fragile list, overlay sections, the fenced house
  notes (4000 characters) and a capability table limited to what the
  install has. Result schemas are passed verbatim to the provider and
  validated again by the daemon (`triage` `{result, summary<=600}`; reply
  `{reply: null|<=1500, summary<=300}`; investigate adds `next: null|<=600`;
  update review as in [updates.md](updates.md) U8; maintenance plan as in
  [maintenance.md](maintenance.md) M16–M18).
- R3. Run time is not per profile: `min(20 minutes, reservation)` where the
  reservation is the smaller of the profile's reserve (triage 600 s, chat
  180, followup 180, investigate 420, summary 90, others 180) and what the
  usage windows still allow; under 60 seconds the run is refused
  ([budget.md](budget.md)).

## Preflight and life cycle

- R4. Preflight order, each a refusal status: options valid
  (`options_invalid`), provider not `none` (`no_provider`), a token
  (`no_token`), a store (`store_unavailable`), the kill switch and backoff
  (`ai_disabled`, `usage_limited`), at least 1200 MB of available memory
  (`skipped_low_memory`), the user-class reserve, isolation
  (`isolation_failed`: the unprivileged account exists, no sensitive path is
  reachable by it, the last self-test passed), the daily run cap and share
  reservation, the export (`export_failed`), the prompt files
  (`setup_failed`), the kill switch again, the drafts directory. Tests
  `test_preflight_refusals_never_start_claude`,
  `test_isolation_failure_blocks_claude`.
- R5. One AI process at a time (a slot lock; `skipped_busy` when the wait
  expires; `cancelled` when the kill switch fired while waiting). The run is
  bound to the opsctl socket (`begin_run`, `attach_process`, `end_run`) so
  only its own process tree can call verbs ([privsep.md](privsep.md)).
- R6. Before every run the run copy is rebuilt and the provider's state
  directory is wiped and recreated (0700, owned by the run user); after the
  run both the state and the drafts directory are wiped again. Tests
  `test_claude_state_is_fresh_for_every_run`,
  `test_a_run_leaves_no_state_and_still_gets_the_token`.
- R7. During the run, every 2 seconds: a process-tree RSS over 900 MB or
  available memory under 400 MB kills the process group
  (`aborted_memory_guard`). A timeout sends SIGTERM, waits 15 seconds, then
  SIGKILL; pipes are drained for at most 30 seconds. Tests
  `test_claude_runner.py:74-171`.
- R8. Every output line is redacted and gzip-written to
  `/data/runs/<stamp>-<profile>/transcript.jsonl.gz`, beside `prompt.txt`,
  `system-prompt.md` and, for triage, `result.json`. Run directories are
  deleted after 60 days. Test `test_run_claude_success_and_redacted_transcript`.
- R9. The guard decision log is read after the run (no symlinks, regular
  file, at most 1 MB); a tool call never decided, or allowed despite a deny,
  is a guard anomaly: at most 10 are stored and the health check reports
  `guard:anomaly` for 7 days. Tests `test_guard_anomalies_flag_undecided_calls_and_overruled_allows`,
  `test_a_recent_guard_anomaly_is_a_health_item`.

## The run copy, the drafts and the tool guard

- R10. **Run copy** `/run/ops-claude/config`, root-owned 0755, files 0644,
  rebuilt before every run and never containing symlinks. With git on: the
  tracked files (`git ls-files --cached`) that pass the readable rules,
  copied from the working tree. With git off: a strict allowlist (top-level
  `*.yaml`/`*.yml` and `README.md`; `automations`, `scripts`, `packages`,
  `alerts`, `templates`, `blueprints` `**.yaml`; `custom_templates/*.jinja`;
  the profile's `.md` documents), each file redacted with the `secrets.yaml`
  values and the provider token, at most 256 KiB per file, 16 MiB and 5000
  files in total. Readable never includes hidden parts, `secrets.yaml`,
  `secrets.yml`, `ip_bans.yaml`, `known_devices.yaml` or a `secrets`
  directory, in any letter case; readable directories are `automations`,
  `alerts`, `templates`, `custom_templates`, `custom_components`,
  `blueprints`, `scripts`, `packages` plus the profile's entries. Tests
  `test_build_export_copies_only_readable_tracked_files`,
  `test_export_skips_files_behind_symlinked_directories`,
  `test_the_export_without_git_is_a_strict_allowlist`,
  `test_a_planted_secret_or_token_is_never_exported`,
  `test_deny_files_in_any_letter_case`.
- R11. The system prompt file lives at `/run/ops-claude/system-prompt.md`,
  outside the export. The drafts directory `/run/ops-drafts` exists only for
  drafting profiles (0700, owned by the run user) with the subdirectories
  `alerts`, `automations`, `docs`, `templates` and existing `automations/**`
  subdirectories up to 4 deep.
- R12. **Guard hook** (`PreToolUse`, every tool). Allowed tools: Read, Grep,
  Glob, Bash, TodoWrite, StructuredOutput, Write; anything else is denied.
  Bash: no NUL, at most 2000 characters, no shell metacharacter outside
  quotes (`; & | \` $ < > \ ( ) { } * ? [ ] ! #`, newline), no `$`, backtick,
  backslash or `!` inside double quotes, no unterminated quote, no argument
  starting `~`; `argv[0]` MUST be `opsctl` (or `/opt/ops/bin/opsctl`) and
  `argv[1]` a registered verb or help. Read: the real path MUST stay under
  the export root. Grep: a `path` under the export is required. Glob: the
  pattern has no `..` and does not start with `/` or `~`. Write: the path
  MUST be `/run/ops-drafts/<allowed proposal path>` and the content at most
  64 KB. Tests `test_allowed_commands`, `test_denied_commands`,
  `test_read_paths`, `test_symlink_out_of_allowed_tree_is_refused`,
  `test_grep_needs_safe_path`, `test_glob_and_other_tools`,
  `test_effect_commands_cannot_smuggle_shell`, `test_hook_verbs_match_opsctl`.
- R13. The hook MUST fail closed: a deny is a JSON `permissionDecision:
  deny` with a reason; malformed input or an internal error is a deny with
  exit status 2. Every decision is appended as `tool_use_id, tool, verdict`
  to `guard-decisions.log` in the provider's state directory (append-only,
  no symlinks, 0600). Tests `test_malformed_input_fails_closed`,
  `test_hook_fails_closed`, `test_hook_fails_closed_on_internal_error`,
  `test_the_guard_hook_records_each_decision`.

## The AI provider

The 0.19.0 code has one provider, selected by `llm.provider: claude`;
`none` turns the AI off (deterministic checks, pushes and updates continue;
AUTO updates are then held unless `updates.hold_if_unreviewed` is off). The
runner calls `claude_runner.run_claude` directly; adding a provider means
adding a module with the same `RunConfig -> RunResult` contract and a
dispatch on `llm.provider`.

- AP1. **Credential.** `claude.token`, else the file
  `/data/secrets/claude_oauth_token`, holds either a Claude Code setup
  token (`claude setup-token`, a subscription) or an Anthropic API key. It
  is exported to the provider process in exactly one variable (AP1a) and
  nowhere else. Tests `test_options_parsing_and_token`,
  `test_the_credential_kind_is_read_from_its_documented_prefix`.
- AP1a. **API key.** The daemon classifies the credential by the
  provider's documented API key prefix `sk-ant-api`: a key is exported as
  `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` is absent; anything
  else is a setup token, exported as `CLAUDE_CODE_OAUTH_TOKEN` with
  `ANTHROPIC_API_KEY` absent. The `sk-ant-` stem alone MUST NOT decide,
  because setup tokens share it (`sk-ant-oat...`). Exactly one is ever set:
  Claude Code ranks `ANTHROPIC_API_KEY` above `CLAUDE_CODE_OAUTH_TOKEN`
  and, in `-p` mode, uses the key whenever it is present. With a key there
  are no subscription usage windows: the share rule, the account-busy
  yield and the user-class reserve ([budget.md](budget.md) B3, B5) do not
  apply, the daily run caps, turn caps, run time and `limits.run_max_usd`
  do, and `claude.plan` is ignored. The key is never logged, written to a
  transcript, the diagnostics export or `api/state` (the redactor masks
  `sk-ant-` values and every configured token); `api/state` `ai.credential`
  and the diagnostics `health.ai_credential` report the kind
  (`setup_token`, `api_key`, or null with none). Tests
  `test_the_environment_carries_exactly_one_credential`,
  `test_an_api_key_run_exports_only_the_key`,
  `test_an_api_key_skips_the_share_rule_but_not_the_run_caps`,
  `test_an_api_key_never_leaves_in_the_export_or_the_state`.
- AP2. **Process.** `nice -n 10 claude -p <prompt> --output-format
  stream-json --verbose --model <m> --max-turns <n> --max-budget-usd <x>
  --permission-mode dontAsk --tools Read,Grep,Glob,Bash[,Write]
  --append-system-prompt-file /run/ops-claude/system-prompt.md
  --json-schema <schema> --no-session-persistence --strict-mcp-config
  --mcp-config '{"mcpServers": {}}'`, started as the unprivileged user with
  no supplementary groups, a new session, `setpriv --no-new-privs` (or the
  `prctl` fallback), stdin closed. Test `test_run_claude_success_and_redacted_transcript`.
- AP3. **Environment.** Only `PATH` (prefixed with `/opt/ops/bin`), `LANG`,
  `LC_ALL`, `TZ`, `HOME=/data/claude-ops/home`,
  `CLAUDE_CONFIG_DIR=/data/claude-ops`, the credential in its one variable
  (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, AP1a), `USE_BUILTIN_RIPGREP=0`,
  `DISABLE_UPDATES=1`, `DISABLE_AUTOUPDATER=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, and the guard's
  `OPS_READ_EXTRA`, `OPS_PROPOSAL_KINDS`, `OPS_FACTS_PATH`. The Supervisor
  token and the other provider credential variable (`ANTHROPIC_AUTH_TOKEN`)
  are never passed. Tests
  `test_runner_passes_user_and_group`,
  `test_the_environment_carries_exactly_one_credential`.
- AP4. **Managed settings** at `/etc/claude-code/managed-settings.json`
  (0644): managed permission rules and hooks only, default mode `dontAsk`,
  bypass disabled, allow `Read(//run/ops-claude/config/**)`,
  `Bash(opsctl *)`, `Edit(//run/ops-drafts/**)`, the same environment, and
  the `PreToolUse` guard hook with a 10-second timeout on every tool.
- AP5. **Package.** The image installs a pinned `claude-code` package from
  the provider's signed apk repository, whose key is verified against a
  pinned SHA-256 before install; the build runs `claude --version`.
- AP6. **Output.** The `result` event decides the status (`ok`, `max_turns`,
  `budget`, `error`); the structured output, else the last top-level JSON
  object in the result text, is the result; tokens, cache reads, duration,
  cost, turns and permission denials are read from it (the usage ledger
  keeps no cost, [budget.md](budget.md) B7; the health check's history line
  keeps its triage run's cost). Rate-limit events give
  the usage-window readings ([budget.md](budget.md)); a rejected last
  reading or a usage-limit message makes the status `usage_limited` with
  the reset time; system events may carry a plan hint. Without a result
  event the last 20 stderr lines decide between error and usage limit.
  Tests `test_claude_runner.py:58-72`, `test_usage.py:165-218`.
