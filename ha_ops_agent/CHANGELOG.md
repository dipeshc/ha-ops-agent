# Changelog

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
