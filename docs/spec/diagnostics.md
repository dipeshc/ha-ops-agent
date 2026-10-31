# Diagnostics export

`GET api/diagnostics` returns a JSON document for bug reports. It is built
from an allowlist, never by dumping state.

Implementation: `opsd/diagnostics.py`. Tests: `tests/test_diagnostics.py`.

- D1. `format: 1`, `generated_at`, `versions {agent, core, supervisor, os,
  python, arch}` (Core, Supervisor and OS from the latest snapshot).
- D2. `options`: every group except `profile`; secret values and
  `notify.service` reduced to `set` or `unset`; lists to `{count}`; other
  scalars as-is with strings cut at 64 characters; plus `options_valid` and
  `problems` (redacted, 200 characters each). Test
  `test_every_option_group_is_summarised_and_secrets_are_set_or_unset`.
- D3. `profile`: loaded, problems, overlay names, git on, proposal kinds,
  push target set or unset, counts only for entities, need_approval,
  watched, fragile, gates and reload domains, and which documents are set.
  No entity id, name or path from the house.
- D4. `store`: available, schema version, row counts per table, Needs-you
  count, failed runs of the last 7 days (at most 20: time, profile, status),
  audit errors of the last 7 days (at most 20, detail redacted); or
  `{available: false, error}`. Test `test_a_missing_store_is_reported_not_raised`.
- D5. `health`: last check time and result, run status, Core down since and
  state, snapshot time, source errors (at most 20), open items as totals by
  severity and kind, self-test, inbound state, AI state, the credential
  kind (`ai_credential`: `setup_token`, `api_key` or null), updates option,
  paused and effective.
- D6. Excluded by construction: entity ids, item keys, chat text, prompts,
  model output, tokens. Tests `test_secrets_and_house_names_never_leave`,
  `test_failed_runs_are_listed_without_their_output`.
- D7. The panel downloads it as `ha-ops-agent-diagnostics-<date>.json`; the
  route needs the admin like every other.
