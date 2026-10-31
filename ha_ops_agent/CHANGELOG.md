# Changelog

## 0.18.0

- Proactive maintenance: deprecations and removals that affect this install
  (repairs, Core log warnings, release notes, archived HACS repositories,
  Core integrations that cover a custom one) become items with the removal
  version, the releases left and a plan. Pushed only within two releases of
  removal or when a proposal or plan waits for you; replacement candidates
  and suggestions are a new Home section.
- A YAML migration becomes a one-tap git proposal (git on only).
- Dashboard migrations: a plan you approve, executed by the new
  approval-only `dashboard.save` capability with a backup, a read-back and
  an automatic restore. New profile key `dashboards.generated`.
- A Core update that removes something still in use always asks.

## 0.17.0

First public release.

- Daily health check against an accepted baseline, Claude Code triage when
  something changed, pushes only when you are needed.
- Nightly updates: soaked patches and minors with an encrypted backup, a
  health diff and rollback; majors, HAOS, firmware and fragile pieces ask.
- One admin chat in the panel and through push replies, with reminders,
  investigations and approval requests.
- Optional git proposals for automations, alerts and templates.
- The house profile (`ops/profile.yaml`) for what is specific to a home.
- A diagnostics export (System > Download diagnostics) for bug reports.
- `aarch64` and `amd64`.
