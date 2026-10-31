# Overview and principles

ha-ops-agent is an unattended maintenance add-on for Home Assistant OS and
Supervised installs. It checks the system daily against an accepted
baseline, uses an AI for the parts that need judgement, installs soaked
updates with a backup, a health diff and a rollback, and holds one admin
chat. It is built so that a wrong or manipulated AI answer cannot do much
harm.

## Components

| Component | Spec | Modules |
| --- | --- | --- |
| Daemon (`opsd`) | this document | `daemon.py`, `__main__.py`, `supervisor.py`, `api.py`, `ws.py`, `live.py`, `events.py` |
| Configuration and the house profile | [configuration.md](configuration.md), [profile.md](profile.md) | `config.py`, `profile.py`, `quiet.py`, `redact.py`, `privacy.py` |
| Health check, baseline, triage | [health.md](health.md) | `health.py`, `baseline.py`, `triage.py`, `evidence.py`, `fingerprint.py`, `queries.py`, `history.py` |
| Updates and the OS flow | [updates.md](updates.md), [os-update.md](os-update.md) | `policy.py`, `updates.py`, `updexec.py`, `updiff.py`, `updatefix.py`, `backups.py` |
| Approvals, items, pushes | [approvals.md](approvals.md), [items-and-pushes.md](items-and-pushes.md) | `approvals.py`, `items.py`, `activity.py`, `notify.py`, `channels.py`, `quiet.py` |
| Panel and API | [panel.md](panel.md) | `panel.py`, `panelapi.py`, `live.py`, `panel/` |
| Chat, inbound, tasks | [chat.md](chat.md) | `assistant.py`, `conversation.py`, `inbound.py`, `tasks.py`, `proposals.py`, `chatcards.py` |
| Proactive maintenance | [maintenance.md](maintenance.md) | `maintenance.py`, `inventory.py`, `dashboards.py` (with `evidence.py`) |
| Git proposals | [proposals.md](proposals.md) | `changes.py`, `changepaths.py`, `gitops.py`, `safegit.py`, `githistory.py`, `configcheck.py` |
| AI runner and provider | [ai-runner.md](ai-runner.md) | `runner.py`, `runctx.py`, `claude_runner.py`, `guard.py`, `prompts/` |
| Capabilities and opsctl | [capabilities.md](capabilities.md) | `capabilities.py`, `policy.py`, `rpc.py`, `opsctl.py`, `effects.py` |
| Integration reloads | [reload.md](reload.md) | `reload.py` |
| Selfguard | [selfguard.md](selfguard.md) | `selfguard.py`, `access.py` (what opens the house, shared by health, reloads and dashboard edits) |
| Privilege separation | [privsep.md](privsep.md) | `privsep.py`, `selftest.py`, `init-ops.sh` |
| Usage budget and kill switch | [budget.md](budget.md) | `budget.py`, `usage.py`, `aicontrol.py` |
| Storage | [storage.md](storage.md) | `store.py`, `jsonstore.py`, `state.py`, `lock.py`, `logs.py` |
| Diagnostics | [diagnostics.md](diagnostics.md) | `diagnostics.py` |

## Invariants

These hold at every stage; each has tests named in the owning document.

- INV1. **The daemon executes; the AI only proposes.** Every change to Home
  Assistant, the host, git or the agent's own records is made by tested
  daemon code after its own checks. The AI asks through named capabilities
  ([capabilities.md](capabilities.md)); the daemon records, checks and
  carries out, or refuses.
- INV2. **Locks, alarms and anything that opens the house are off limits.**
  No capability operates them; a reload never targets an integration that
  provides one ([reload.md](reload.md)); exposure of one to Assist is a
  critical health item ([health.md](health.md)).
- INV3. **The agent never changes itself, the Supervisor or the host**,
  except the operating-system update and reboot the admin approved, through
  a single-use flow ([selfguard.md](selfguard.md)).
- INV4. **The AI never sees secrets.** The Supervisor token, the live
  configuration, `secrets.yaml`, `.storage`, the backups and the add-on's
  data are unreachable at the operating-system level; the daemon checks
  this before every run and refuses to start the AI otherwise
  ([privsep.md](privsep.md)).
- INV5. **Untrusted text is data.** Device names, logs, release notes,
  documents and chat messages reach the AI fenced as data, are rendered by
  the panel as text, and never title a need or a push; model text appears
  in one field (`agent.text`) ([items-and-pushes.md](items-and-pushes.md)).
- INV6. **The admin approves what is risky.** Majors, OS, firmware, fragile
  pieces, held versions and config changes ask; an approval is admin-only,
  bound to the exact plan shown, used once and expires
  ([approvals.md](approvals.md)).
- INV7. **Updates are reversible or they ask.** Every automatic update has a
  backup, a health comparison and a rollback path; one without a precise
  rollback asks first; a rolled-back version is held ([updates.md](updates.md)).
- INV8. **Fail closed.** Invalid options, a refused profile, a non-admin
  push device, a failed self-test or an unreadable health all stop acting,
  installing and running the AI; the deterministic checks and safety pushes
  continue ([configuration.md](configuration.md)).
- INV9. **Bounded.** Daily run caps, turn, cost and time limits, a usage
  share, a memory guard and a kill switch for the AI; daily budgets for
  pushes; quotas and rate limits for every capability ([budget.md](budget.md),
  [items-and-pushes.md](items-and-pushes.md)).
- INV10. **Local first.** The house keeps working when the AI provider or
  the internet is down: the health check, the update rule, pushes and the
  panel are deterministic code that never waits on the AI.

## Trust model

- The admin is the one active Home Assistant administrator who owns the
  configured push device. The panel admits only that user id; push replies
  are accepted only from that user and device. Code running in the Home
  Assistant frontend with the admin's session (another add-on's panel, a
  custom card) could act as the admin in the panel; the CSRF token and
  same-origin checks stop cross-site requests but not same-session code.
- The Supervisor is trusted. The AI provider is not: its output is
  schema-checked, redacted, and acted on only through the capability
  pipeline. Home Assistant data (entity names, logs, repairs, release
  notes) and anything the admin pastes into chat are untrusted text.
- The Supervisor token lives only in the daemon's environment. The AI's
  credential is passed only to the AI process and never logged
  (every log line and transcript passes the redactor).

## Daemon life cycle

- D1. Start: clear stale Core-down state, open the opsctl socket, resolve
  the time zone (Supervisor info, else `TZ`, else UTC), run the self-test,
  open the panel on port 8099, wait the startup delay (default 600 s). With
  a store: recover running tasks, chat and changes; schedule `health.check`
  daily at `health.check_time` (with a catch-up when the last check is over
  24 hours old), `update.window` daily at `updates.window` (no catch-up)
  and `status.refresh` every 10 minutes; resume an interrupted update run;
  close stale AI runs; ensure the AI switch helper; then run the scheduler,
  the event dispatcher, the liveness loop, the admin-identity loop, the
  websocket event stream and the assistant worker. Without a store: a
  degraded push and a reduced schedule ([storage.md](storage.md) S5).
- D2. The liveness loop runs every 60 seconds: flush held pushes, check
  Core (`GET /core/api/`), raise the Core-down alert after 5 minutes (20
  while Core is starting or inside the update window, 45 while Supervisor
  jobs are busy) and the "Core is back" push plus a health cycle on
  recovery, request an overdue check after 26 hours, and tick the items
  (the rechecks and the pushes time made due,
  [items-and-pushes.md](items-and-pushes.md) I5b, N1).
- D3. The watchdog target is `/healthz` on the panel port. SIGTERM cancels
  the main task and drains in-flight pushes for at most 2.5 seconds; s6
  restarts the daemon on a non-zero exit.
- D4. The Supervisor client uses `http://supervisor` with the
  `SUPERVISOR_TOKEN` environment variable only, 30-second timeouts (60 for
  logs), and passes every request through selfguard. The websocket client
  speaks RFC 6455 to `ws://supervisor/core/websocket`, authenticates with
  the same token, limits messages to 64 MiB, and the event stream pings
  every 50 seconds, replaces a connection silent for 130 seconds, drops
  events beyond 30 per minute and reconnects with a backoff from 1 to 60
  seconds. Tests `test_ws_accept_key_and_frames`, `test_inbound.py:300-363`.
- D5. The add-on image is built on the user's machine from a per-architecture
  pinned base image (`aarch64`, `amd64`), pinned Alpine packages and the
  pinned AI provider package; a rotated upstream package fails the build
  rather than changing silently. The add-on runs with `hassio_role:
  manager`, `init: false` (s6 as PID 1), ingress on 8099, an admin-only
  panel, and a hot backup with a pre-backup checkpoint. Tests
  `test_every_apk_package_is_pinned`,
  `test_the_base_image_is_pinned_per_architecture_with_no_default`.

## Wording

The spec says "the agent" for the add-on as a whole, "the daemon" for the
code that acts, and "the AI" or "the model" for the language model. A
provider or product is named only in [ai-runner.md](ai-runner.md), the
`llm.provider` option values, and the `claude.*` option group, which are
provider-specific by nature.
