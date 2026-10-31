# Capabilities, policy and the opsctl protocol

A capability is a named action the AI may request. The registry is the
single source of the `opsctl` verbs, the guard's verb list, the RPC schema
and the prompt's capability table. The daemon executes; the AI only asks.

Implementation: `opsd/capabilities.py` (registry), `opsd/policy.py`
(tiers and `decide`), `opsd/rpc.py` (socket server and pipeline),
`opsd/opsctl.py` and `bin/opsctl` (client), `opsd/effects.py`,
`opsd/runctx.py`. Tests: `tests/test_capabilities.py`,
`tests/test_rpc_effects.py`, `tests/test_opsctl.py`, `tests/test_policy.py`.

## The registry

- K1. Every row has a unique name and verb (never `help`), references
  resolving to `opsd...:function`, typed arguments named `[a-z]{1,20}`
  (booleans never positional), `requires` within `git`, `vacuums`,
  `localpatches`, `suppress`, allowed origins, effects, a tier, a quota
  `(per_run, per_day)` and `enabled`. The argparse tree, the guard's verb
  set and the RPC schema are generated from it. Tests
  `test_every_reference_resolves`, `test_guard_parser_and_schema_are_generated`,
  `test_registry_rejects_bad_rows`, `test_disabled_capability_is_not_offered_anywhere`.
- K2. Verbs on the socket (0.17.0): `status`, `baseline`, `health
  [--diff]`, `repairs`, `entities`, `integrations`, `entries`, `updates`,
  `backups`, `logs <core|supervisor|host|addon>`, `history <entity>`,
  `system`, `update-plan`, `git-status`, `git-log`, `git-diff`, `git-show`
  (git), `vacuum-rooms` (overlay), `remind` (`task.create`, 5/30), `tasks`,
  `cancel` (10/30), `ack` (`baseline.ack`, agent-self only, 100/400),
  `notify` (`notify.admin`, agent-self only, 1/9), `investigate`
  (`task.investigate`, admin origins, 1/4), `request-approval <update.x|backup>`
  (`approval.request`, admin origins, 2/6), `propose-change` (3/10, git),
  `propose-fact` (2/5, git), `propose-suppression` (1/3, git and overlay),
  `integration-reload <domain>` (2/6, refund on refusal).
- K3. Internal capabilities never reachable from the socket, the CLI, the
  guard or the prompt: `change.apply`, `change.revert` (tier APPROVE),
  `backup.create`, `update.install` (tier from the update rule),
  `update.rollback` (AUTO_NOTIFY). Test
  `test_only_the_export_and_the_drafts_are_allowed` and the registry tests.
- K4. "Read" capabilities (effects exactly `{READ}`): `health.snapshot`,
  `ha.entities`, `ha.entries`, `ha.logs`, `ha.history`, `update.plan`,
  `vacuum.rooms`, `git.status/log/diff/show`.

## Policy

- K5. Tiers in order: AUTO < AUTO_NOTIFY < APPROVE < NEVER. `decide` takes
  the strictest of: the row's default; NEVER when the origin is not allowed;
  NEVER when the origin is `admin-chat` and the effects are not within
  `{READ, SCHEDULE, NOTIFY, PROPOSE, RELOAD}`; the row's resolver (the update
  rule for installs); `capabilities.require_approval` (APPROVE) and
  `capabilities.never` (NEVER). Only AUTO and AUTO_NOTIFY execute. In
  `observe` mode (or with invalid options) an executable request with a
  RELOAD, MUTATE_HA or MUTATE_GIT effect becomes report-only. Options never
  loosen. Tests `test_policy.py:178-236`, `test_act_mode_executes_observe_mode_only_reports`,
  `test_origins_outside_the_row_and_the_admin_switch_are_refused`.
- K6. The run's grant is its profile's capability list
  ([ai-runner.md](ai-runner.md)); a capability whose `requires` group the
  install lacks is refused and not offered.

## The socket and the pipeline

- K7. The socket is `/run/ops-sock/opsd.sock`, directory root:ops 0750,
  socket root:ops 0660. The client (`bin/opsctl`) holds no token, sends one
  request and prints the output (exit 0) or `error: <msg>` (exit 1);
  without a daemon it prints `cannot reach the ops daemon`. Test
  `test_socket_roundtrip_and_permissions`.
- K8. Wire: one newline-terminated JSON object `{"verb", "args"}` of at
  most 8192 bytes, read with a 10-second timeout; the reply is `{"ok":
  true, "output"}` or `{"ok": false, "error"}`. Parsing accepts only `verb`
  and `args`, an enabled non-internal verb, known arguments, strict
  booleans, integers clamped to their range, strings checked against
  choices or pattern. Tests `test_rpc_schema_rejects`,
  `test_rpc_parse_clamps_and_defaults`, `test_rpc_limits`.
- K9. **Peer.** The daemon reads `SO_PEERCRED`; the uid MUST be non-root and
  in the allowed set (the `ops` uid in production), and the pid MUST descend
  from the bound run's root process; outside a run, mutating verbs are
  refused. At most 8 connections. Tests `test_rpc_rejects_unexpected_peer`,
  `test_socket_denies_unknown_peer_and_overload`, `test_rpc_effects.py:110-142`.
- K10. **Pipeline per request:** bind the peer; the run's request budget of
  200; parse; the profile grant; `requires`; mutating verbs need a run; the
  validator; `policy.decide`; a non-executable verdict is refused with
  "needs the admin's approval", "observe mode: report it instead" or "not
  allowed by policy"; a store is required for non-read verbs; the per-run
  and per-day quota is reserved before executing (the per-run unit refunded
  on failure, the daily unit only for rows marked refund-on-refusal); an
  audit row is begun before and finished after execution, and an audit
  failure refuses the request. Executors run with a 120-second timeout,
  sync ones in a thread under a 2-slot semaphore. Output is redacted and
  cut at 40 000 characters. Rejections are audited at most 60 per minute.
  Tests `test_rpc_effects.py`, `test_output_secrets_and_cap`.
- K11. **Effects.** `baseline.ack` only records an ack (key must be in the
  run's hand-off, days 1..30, a critical item can be snoozed at most 3 days
  and never accepted, a warning at most 7); the daemon applies it after the
  run. `notify.admin` needs a message of at most 900 characters, `urgent`
  only when the hand-off has a critical item, and is sent with the run's
  budget class. Tests `test_rpc_effects.py:177-242`, `test_daemon.py:498-538`.
