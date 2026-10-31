# Changelog

## 0.19.0

Storage consolidation and items written at the source. Nothing you see
changes beyond the points below.

- The store has ten tables instead of nineteen: one `approvals` record for
  every approval, approval request, config change and house fact (a kind
  and a payload; the binding, single use and expiry checks are unchanged),
  and the small state (daily counters, quiet-hours holds, push action ids,
  reload times, the chat's read mark) in `kv`. `/data/reloads.json` moves
  into the store; the activity log, the thread table and the unused cost
  columns go.
- **Upgrade.** The first start of 0.19.0 migrates the 0.18 store in one
  transaction: open approvals, requests, changes and facts, a running
  update night, held pushes, reminders, the chat, the usage ledger and
  every item keep working. If the migration fails, the store stays as 0.18
  left it and the agent runs degraded until it succeeds. A store older
  than 0.18 is refused: install 0.18 first and start it once. Before the
  migration the store is copied to `/data/ops.db.v18.bak` (kept 14 days);
  without the copy the migration does not start. To go back to 0.18: stop
  the add-on, put `ops.db.v18.bak` back as `ops.db`, install 0.18.x (or
  restore the add-on's backup taken before the update).
- Items are written in the same transaction as the record they show (an
  update job, an approval, a change, a reminder, the health check's open
  items, a maintenance scan) instead of being re-derived every minute.
  Pushes, Home, History, the Needs you badge and `sensor.ops_agent` behave
  as before.
- History no longer turns old records into "cancelled: no longer
  pending": a reminder stays done after 14 days, a change older than the 30
  newest keeps its state, and a house fact shows its decision ("rejected",
  "applied", "proposed as config change #n").
- The chat's activity context is the items' timelines of the last week.

The AI credential:

- Accept an Anthropic API key in `claude.token`: a credential starting with
  `sk-ant-api` is passed as `ANTHROPIC_API_KEY`, setup tokens keep
  `CLAUDE_CODE_OAUTH_TOKEN`, and only one is ever set. With a key the
  subscription share rule and `claude.plan` do not apply; the daily run
  caps and `limits.run_max_usd` (each run's cost cap, enforced by the
  provider) do.
- Panel state `ai.credential` and the diagnostics `health.ai_credential`
  report the credential kind, never the value.

Fixes and the add-on:

- An entity in `health.expected_unavailable` no longer makes its custom
  integration critical, and the first-run baseline accepts it.
- Store icon and logo.

For house overlays (`opsd.house`):

- `opsd.reload.reload_domain` takes the store instead of the data
  directory: replace `reload_domain(api, d.paths.data, domain, ...)` with
  `reload_domain(api, d.store, domain, ...)`. Without a store it refuses.
- Unchanged: `Store.bump`, `unbump`, `count`, `audit_begin`, `audit_finish`,
  `changes`, `ActivityLog.record` (`d.activity.record`) and
  `opsd.changes.build_proposal`, `_day`, `_repo`.
- Removed: `Store.activity`, `add_activity`, `link_activity` and
  `set_activity_turn` (activity entries are items: read them with
  `Store.items_all(prefix="activity:")` or `Store.recent_events`),
  `Store.items_all(derived=...)`, `ItemBook.sync` (a write updates its
  items; tests call `ItemBook.tick()` or `Daemon.items_flush()`) and
  `Hub.listeners`.

## 0.18.0

Proactive maintenance:

- Deprecations and removals that affect this install (repairs, Core log
  warnings, release notes, archived HACS repositories, Core integrations
  that cover a custom one) become items with the removal version, the
  releases left and a plan. A deprecation asks you only within two releases
  of its removal; replacement candidates and suggestions are a new Home
  section, *Suggestions*, and never push.
- A YAML migration becomes a one-tap git proposal (git on only).
- Dashboard migrations: a plan you approve, executed by the new
  approval-only `dashboard.save` capability with a backup, a read-back and
  an automatic restore. Only core display-only cards with no tap actions
  are ever edited; every other card gets a text-only plan. New profile key `dashboards.generated` for
  dashboards something else writes.
- A Core update that removes something still in use always asks.

Documentation:

- `docs/spec/`: the specification, 20 documents of numbered, testable
  requirements with pointers to the code and tests that implement them.
  The panel wire contract now lives in `docs/spec/panel.md`.
- The README describes the agent independently of its AI provider, with an
  *AI access* section and panel screenshots (sample data). Docs, comments
  and strings say "the agent" or "the AI"; Claude is named only where
  something is specific to Claude Code.
- Docs and option descriptions now say what the code does: a Claude Code
  setup token is the only sign-in, the urgent push cap is 8, only the admin
  who owns the push device uses the panel, and an approval is one panel
  step.
- The add-on description, `DOCS.md` and `NOTICE` no longer carry a
  disclaimer line.

## 0.17.0

First public release.

- Daily health check against an accepted baseline, AI triage when something
  changed, pushes only when you are needed.
- Nightly updates: soaked patches and minors with an encrypted backup, a
  health diff and rollback; majors, HAOS, firmware and fragile pieces ask.
- One admin chat in the panel and through push replies, with reminders,
  investigations and approval requests.
- Optional git proposals for automations, alerts and templates.
- The house profile (`ops/profile.yaml`) for what is specific to a home.
- A diagnostics export (System > Download diagnostics) for bug reports.
- `aarch64` and `amd64`.
