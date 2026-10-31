# Specification

This directory is the specification of ha-ops-agent: what the add-on does,
stated as testable requirements, with pointers to the module that
implements each one and the tests that enforce it.

## How to use this spec

- **The docs are the spec.** Where a document here and the code disagree,
  the code is wrong. File the mismatch as a backlog item
  (`docs/backlog/`) and fix the code, unless the owner decides to change the
  spec, in which case the spec edit and the code change land together.
- **Behaviour changes edit the spec in the same commit.** A change that
  alters externally observable behaviour (an option, a route, a threshold,
  a rule, a push, a refusal) is incomplete without its spec edit. A
  reviewer treats a missing spec edit like a missing test.
- **Sharpen, never re-decide.** A change may make an already-decided
  statement precise. Re-deciding one is the owner's call, raised as a
  `blocked/` item.
- **Requirements are numbered** (`P1`, `U12`, ...) per document so items,
  commits and reviews can cite them. Renumbering is a spec change; prefer
  appending.
- **Synthetic examples only.** Nothing here comes from a real install.
- **Provider-agnostic wording.** Say "the AI" or "the model"; name a
  provider only in [ai-runner.md](ai-runner.md) and the options that are
  provider-specific by nature.

## Documents

| Document | Covers |
| --- | --- |
| [overview.md](overview.md) | Components, invariants, trust model, daemon life cycle, build. |
| [configuration.md](configuration.md) | Add-on options, validation, fail-closed, quiet hours. |
| [profile.md](profile.md) | The house profile: schema, validation, overlays, merging with options. |
| [health.md](health.md) | The daily check, items and severities, the baseline, acks, triage, evidence. |
| [updates.md](updates.md) | The update rule, soak, gates, the AI review, execution, verify, rollback, holds, the one-tap fix, backups. |
| [os-update.md](os-update.md) | The operating-system update and reboot flow. |
| [approvals.md](approvals.md) | Approval records, binding, single use, expiry. |
| [items-and-pushes.md](items-and-pushes.md) | Items, the state vocabulary, derivation, revisions, and the push policy. |
| [panel.md](panel.md) | The panel transport, security, the item record and every route. |
| [chat.md](chat.md) | The one chat, push replies, reminders, follow-ups, investigations. |
| [proposals.md](proposals.md) | Config changes as git proposals. |
| [maintenance.md](maintenance.md) | Proactive maintenance: deprecation signals, the inventory, findings, plans, dashboard migrations and the `dashboard.save` capability. |
| [ai-runner.md](ai-runner.md) | Run profiles, preflight, the run copy, the tool guard, and the AI provider. |
| [capabilities.md](capabilities.md) | The capability registry, policy tiers, the opsctl socket and pipeline. |
| [reload.md](reload.md) | Integration reloads: allowlist, never-list, live checks, rate. |
| [selfguard.md](selfguard.md) | The allowlist of Supervisor, Core and websocket calls; self identity; the OS flow. |
| [privsep.md](privsep.md) | Accounts, file modes, the pre-run check, the socket boundary, the self-test. |
| [budget.md](budget.md) | The AI usage budget, measurement, and the kill switch. |
| [storage.md](storage.md) | The store, migrations, recovery, retention, JSON files, logs. |
| [diagnostics.md](diagnostics.md) | The diagnostics export. |

User-facing documentation (`README.md`, `ha_ops_agent/DOCS.md`,
`examples/profile.yaml`) follows this spec and must not contradict it.
