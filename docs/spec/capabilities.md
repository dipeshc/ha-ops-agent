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
- K2. Verbs on the socket (0.21.0): `status`, `baseline`, `health
  [--diff]`, `repairs`, `entities`, `integrations`, `entries`, `updates`,
  `backups`, `logs <core|supervisor|host|addon>`, `history <entity>`,
  `system`, `core-compare <domain>` (3/12, refund on refusal), `update-plan`, `git-status`, `git-log`, `git-diff`, `git-show`
  (git), `vacuum-rooms` (overlay), `remind` (`task.create`, 5/30), `tasks`,
  `cancel` (10/30), `ack` (`baseline.ack`, agent-self only, 100/400),
  `notify` (`notify.admin`, agent-self only, 1/9), `investigate`
  (`task.investigate`, admin origins, 1/4), `request-approval <update.x|backup>`
  (`approval.request`, admin origins, 2/6), `propose-change` (3/10, git),
  `propose-fact` (2/5, git), `propose-suppression` (1/3, git and overlay),
  `propose-rebase <domain>` (`fork.propose`, 1/3, refund on refusal),
  `integration-reload <domain>` (2/6, refund on refusal).
- K3. Internal capabilities never reachable from the socket, the CLI, the
  guard or the prompt: `change.apply`, `change.revert` (tier APPROVE),
  `dashboard.save` (tier APPROVE), `fork.apply` (tier APPROVE, effect
  `{MUTATE_HA}`: writes an approved fork rebase and restarts Core, only as
  an update job, [updates.md](updates.md) U26–U31), `backup.create`,
  `update.install` (tier from the update rule), `update.rollback`
  (AUTO_NOTIFY; also restores a fork rebase's snapshot). Test
  `test_only_the_export_and_the_drafts_are_allowed` and the registry tests.

- K4. "Read" capabilities (effects exactly `{READ}`): `health.snapshot`,
  `ha.entities`, `ha.entries`, `ha.logs`, `ha.history`, `ha.core_compare`,
  `update.plan`, `vacuum.rooms`, `git.status/log/diff/show`.
  `ha.core_compare` is the one read that makes the daemon reach the
  internet (`NETWORK_READS` in `opsd/runner.py`): it is not among the
  "reads" a run profile gets as a group, and only `chat`, `investigate`
  and `maintenance_plan` name it ([ai-runner.md](ai-runner.md)).

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
  budget class. It needs a `reason`: a class from `PUSH_REASONS`
  (`needs_action`, `physical_task`, `security`: the admin must act or
  decide) pushes; `note` writes a quiet History note instead (activity kind
  `note`, the text under `agent`; no push, no chat message, no quota unit,
  at most 3 a run); any other or no reason is refused before any quota is
  taken, with the valid classes in the error. The agent's own `task.create`
  needs the same `reason` (a push class or `note`), which decides whether
  the task's reminder or follow-up result is pushed when due
  ([chat.md](chat.md) T16). The deterministic critical push
  ([health.md](health.md) H17) is unaffected. Tests
  `test_rpc_effects.py:177-242`, `test_a_push_needs_a_reason_class_and_a_note_takes_no_push_quota`,
  `test_an_agent_push_for_information_goes_to_history_and_the_critical_push_still_goes`,
  `test_daemon.py:498-538`.
- K12. **`dashboard.save`** (verb `dashboard-save`, executor
  `opsd.dashboards:exec_internal`, which always refuses) is daemon-internal:
  tier APPROVE, effect `{MUTATE_HA}`, origin agent-self only, in no run
  profile's grant. The daemon saves one storage dashboard only through
  `dashboards.DashboardWriter.execute`, applying only the narrow edit of
  [maintenance.md](maintenance.md) M18, and only to core display-only
  cards with no tap actions (any other card gets a text-only plan; a
  display card that names, matches or could select access hardware is
  refused too), on a dashboard Home Assistant lists in `storage` mode at
  the request and at execution, after consuming the admin's
  dashboard approval, in act mode, and refuses when `capabilities.never`
  names `dashboard.save`; `capabilities.require_approval` cannot loosen
  it. The save itself passes the self-guard only inside its approved scope
  ([selfguard.md](selfguard.md) G8). See [maintenance.md](maintenance.md)
  M20–M23. Test `test_dashboard_save_is_an_approval_only_daemon_capability`.
- K13. **`logs host --identifier`** names one journal identifier,
  `[A-Za-z0-9_.@-]+`, never containing `..` and never only dots; it is
  percent-encoded as one segment of `/host/logs/identifiers/<id>`.
  Test `test_logs_literal_grep_and_redaction`.
- K14. **`ha.core_compare`** (verb `core-compare <domain>`, executor
  `opsd.coresource:cmd_core_compare`, validator
  `opsd.coresource:validate_compare`) compares a custom integration with
  Home Assistant Core's integration of the same domain, read-only, with no
  approval. Effects `{READ}`, quota 3 per run and 12 per day, a validator
  refusal gives the daily unit back.
  - **Input.** Only the domain, `[a-z0-9_]{1,64}`, naming a directory
    `custom_components/<domain>` that is a real directory (not a symbolic
    link) with a regular `manifest.json`; refused before any quota or
    network use otherwise, and outside a run. The Core version is the live
    installed one (Supervisor `/core/info`), never model input, and must
    be a release tag (`YYYY.M.P`, or a beta `YYYY.M.PbN`); a dev build is
    refused.
  - **Fetch** (the daemon only; the AI's sandbox has no network): the file
    list from `https://api.github.com/repos/home-assistant/core/contents/homeassistant/components/<domain>?ref=<version>`,
    unauthenticated, and each file from
    `https://raw.githubusercontent.com/home-assistant/core/<version>/homeassistant/components/<domain>/<file>`,
    through `evidence.fetch_text` narrowed to that one host (HTTPS, TLS
    verified, a redirect to any other host refused). These two hosts are
    `evidence.CORE_SOURCE_HOSTS`; `raw.githubusercontent.com` is not in
    the general `ALLOWED_HOSTS`. A file name matches `[A-Za-z0-9_.-]{1,64}`
    and never contains `..`; a listing keeps at most 40 files, a file is
    at most 256 KiB (larger ones are listed as left out), and each fetched
    file must match the git blob SHA the listing gives for it or it is not
    compared. Only files the custom integration also has are fetched.
    GitHub's 403/429 stops every GitHub call for 30 minutes (the backoff
    `opsd.evidence` shares). One call stays within 90 seconds, 10 per
    request.
  - **Cache** under `/data/core-source/` (directories 0700, files 0600,
    root-only): listings by tag and domain, files by blob SHA (checked
    again when read). Listings of tags outside the installed tag and the
    18 monthly releases before it, and files unused for 30 days or beyond
    800, are pruned.
  - **Local read.** Top-level regular files of
    `custom_components/<domain>/`, read as root through one descriptor of
    the directory reached with `O_DIRECTORY | O_NOFOLLOW` at each level
    from the config directory (a link for `custom_components` or the
    domain is refused), never through a symbolic link, at most 40 and 256 KiB each, UTF-8 only; subdirectories,
    `.pyc`, hidden and backup files are listed as not compared.
  - **`--fork`.** Also the fork's origin (the newest Core tag among the
    installed one and the `.0` releases of the last 18 months whose files
    differ least from the fork's) and the fork's own changes against it as
    numbered hunks (`<file>#<n>`, with their added and removed lines),
    fenced in `<fork-hunks>`: the ids `propose-rebase --keep` takes
    ([proposals.md](proposals.md) C11–C12).
  - **Answer.** A header with the Core version and the GitHub tree URL; a
    summary (files compared, changed and identical; files only in the
    custom integration; files only in Core; changed lines per file as
    `+added -removed`; subdirectories; what was left out and why); then a
    unified diff per changed file (`core/<version>/<file>` against
    `custom/<file>`), fenced in `<core-compare-diff>` as untrusted data
    (angle brackets replaced) and cut at 32 KiB with a marker saying how
    much was not shown (so the whole answer stays inside the socket's
    40 000-character cap). When Core has no such integration the answer
    says it is a pure custom integration. The answer is redacted.
  Tests `tests/test_coresource.py`.
- K15. **`fork.propose`** (verb `propose-rebase <domain> --keep <ids|none>
  --reason <text>`, executor `opsd.forkrebase:exec_propose`, validator
  `opsd.forkrebase:validate_propose`): effects `{PROPOSE}`, origins admin,
  admin-chat and agent-self, quota 1 per run and 3 per day, a validator or
  build refusal gives the daily unit back. Granted to `chat`,
  `investigate` and `maintenance_plan`, never `triage` or `followup`. It
  records a proposal and writes nothing else; see
  [proposals.md](proposals.md) C11–C14. Tests `tests/test_forkrebase.py`.

