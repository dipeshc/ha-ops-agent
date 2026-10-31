---
by: owner (spec review 0.17.0)
spec: docs/spec/ai-runner.md AP1a
---

# Accept a provider API key in `claude.token`

**What's wrong.** The option text (`ha_ops_agent/config.yaml:55`,
`translations/en.yaml`) and the README promise that `claude.token` takes
either a setup token or an API key, but the runner exports whatever it
holds as `CLAUDE_CODE_OAUTH_TOKEN` (`opsd/claude_runner.py:221-237`,
`build_env`) and nothing detects a key. An API key therefore fails every
run. `docs/spec/ai-runner.md` AP1a specifies the intended behaviour and
marks it not yet implemented.

**Evidence.** `grep -rn "ANTHROPIC_API_KEY\|sk-ant" opsd/` matches only the
redactor pattern (`opsd/redact.py`). `opsd/config.py:517-524`
(`claude_token`) returns the raw string with no classification.
`opsd/budget.py` assumes subscription usage windows for every run.

**Consequence.** Users without a subscription cannot use the AI at all
although the documentation says they can; the deterministic fallback runs
and the panel shows failed runs.

**Acceptance criteria.**
- A token starting with `sk-ant-` is exported as `ANTHROPIC_API_KEY` and
  `CLAUDE_CODE_OAUTH_TOKEN` is absent; any other token keeps today's path.
- With a key the share rule and the plan estimate are skipped (no
  `share_cap:*`, `account_busy:*` or `share_reserve:*` refusals), while the
  daily run caps and `limits.run_max_usd` still apply.
- The key never appears in logs, transcripts, the diagnostics export or
  `api/state` (`claude.token` stays `set`/`unset`); `api/state` reports the
  credential kind.
- Tests in `tests/test_claude_runner.py` (environment for each kind),
  `tests/test_usage.py` (share rule skipped with a key) and
  `tests/test_diagnostics.py` (no leak).
- `docs/spec/ai-runner.md`: drop the "planned" marker from AP1a and update
  AP1 and AP3 in the same commit; `docs/spec/budget.md` B3 notes the
  exemption.

**Exact files to change.** `opsd/config.py` (credential kind),
`opsd/claude_runner.py` (`build_env`), `opsd/runner.py` and
`opsd/budget.py` (skip the share rule for a key), `opsd/panelapi.py` and
`opsd/diagnostics.py` (credential kind), the tests and spec files above.

**Suggested approach.** Add a small `Credential` value (kind + secret) built
once in `config.py`; thread the kind through `RunConfig` so `build_env`
picks the variable, and give `reserve_run` a flag that bypasses the window
checks. Verify against the provider's documented environment variables
before relying on the prefix alone.
