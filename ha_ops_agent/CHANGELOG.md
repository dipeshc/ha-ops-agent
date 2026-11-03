# Changelog

## 0.19.1

Hardening and fixes. No store change; upgrading from 0.19.0 needs nothing.

Health check and pushes:

- Each piece of access hardware that goes down (a lock, an alarm panel, a
  cover that is not positively a blind or the like, a switch, button,
  script, input boolean or valve named like a door, gate or lock; never a
  sensor, binary sensor, camera or light) is its own critical item, `access:<entity_id>`,
  instead of part of its integration's `entities:<platform>` item. Only
  your accept in the panel silences it, until it recovers; the agent can
  snooze it for 3 days at most and never accept it, and the first run never
  accepts one that is down. After the upgrade, access hardware that is down
  now is reported once even if an earlier accept covered its integration.
- The Assist exposure check uses the same rule: door-class and
  unclassified covers and access-named switches and buttons exposed to
  Assist are reported too.
- An item the agent snoozed or accepted in its triage run, or that you
  acked in the panel, no longer stays in Needs you: it shows as resolved
  with who acked it, until when and why, and you can still accept or snooze
  it there yourself. Its critical push still goes out.
- `sensor.ops_agent` carries daemon-written attributes only; the triage
  and update-night summaries stay in the panel.
- A push from the agent's own run links to the chat but has no Reply
  button.
- The push relay caches only the verified admin's registration, never
  another device with the same name.
- Account utilisation is read as a percent, as the provider reports it:
  1 % is no longer taken for the whole window, so the agent no longer
  yields or stops runs for a week after a small reading. A reading over
  100 counts as a full window.

The panel:

- The admin is re-confirmed every 4 minutes and the panel accepts the
  admin only within 5 minutes of the last confirmation (it was daily).
  While Home Assistant does not answer the re-check, the last confirmed
  admin is kept for up to 15 minutes and the check is retried every 45
  seconds; only an answer that the user is no longer an admin clears it.

Config proposals:

- The current file is checked as well as the new one: a file that already
  calls an action outside the allowlist (a lock, the alarm, a cover, a
  switch), uses devices or areas, or carries YAML tags is never changed by
  a proposal, and an existing alert is never removed and keeps what it
  watches, its repeat, skip_first and acknowledgement settings and its
  notifiers.
- An automation's id and alias may not repeat another automation's;
  `repeat` runs at most 10 times (a literal count or list, no
  `while`/`until`); webhook triggers and sub-minute time patterns are
  refused; helper targets are checked against every tracked YAML file.
- You review every file's whole diff: a proposal too long to show in full
  is refused instead of cut, and the item lists the actions and targets
  each file adds and removes. A stored proposal whose diff is not complete
  can only be rejected.
- The house facts document changes only through propose-fact (one fact
  appended), is capped at 16 KiB, and no proposal applies in observe mode,
  documents included.
- Git runs with signing, credential helpers, askpass and every repository
  filter, diff and merge driver neutralised, over SSH only, and refuses to
  run when the repository config sets includes, URL rewrites, a proxy
  command or another work tree. Fetch and push use `remote.origin.url`,
  which must be a GitHub SSH URL (`git@github.com:<owner>/<repo>`).

Updates and approvals:

- An add-on installs only while the Supervisor still offers the planned
  version, checked at the pre-check and again right before the call;
  otherwise the job is skipped and nothing is called.
- An update approval is used only while unexpired and bound to its plan,
  for the same entity, versions, kind, title, slug and HACS repository.
- A Supervisor-managed update entity is never installed through
  `update.install`.
- An update's config fix passes the proposal call allowlist (no templated
  action, service, entity id or target), a template macro changes only
  when no YAML picks an action or target with a template or does more
  than a proposal may while using any template, and a Core
  update that may need a config change is reviewed in a run of its own
  over Home Assistant's notes only.
- With `updates.review: false`, `updates.hold_if_unreviewed` (default on)
  now holds AUTO candidates that have no earlier assessment, as the option
  describes. Set it off to install them unreviewed.

The AI run and the add-on:

- The AI's copy of the configuration is redacted with git on as well.
  Tracked files that are not UTF-8 or over 256 KiB are left out of it.
- `opsctl logs host --identifier` refuses dot segments and encodes the
  identifier.
- The guard hook gets 60 seconds instead of 10, and the AI account's
  directory is recreated at every start.
- A weekly CI job fails when a package from the base image is out of date.

For house overlays (`opsd.house`):

- Unchanged: `opsd.reload.reload_domain`, `Store.bump`, `unbump`, `count`,
  `audit_begin`, `audit_finish`, `changes`, `ActivityLog.record`,
  `opsd.changes.build_proposal`, `_day`, `_repo` (signatures).
  `build_proposal` refuses more content as above, and refuses the house
  facts document unless `kind="facts"`.
- `opsd.notify.extract_registration(entries, service, user_id,
  device_id=None)` and `Notifier.cache_registration(path, user_id,
  device_id=None)` take the admin's ids.
- `EffectSink.send_message(message, *, reply=True)`.
- `opsd.health.derive_assist_exposure(exposure, states, registry=())`.
- `opsd.changepaths.allowed_path` no longer accepts the house facts path.
- `opsd.configcheck.check_configuration_edit` takes optional `entities`
  and `admin_notify`.
- Git calls can raise `opsd.safegit.UnsafeRepoConfigError` (a
  `subprocess.SubprocessError`; `Repo` turns it into `GitError`).

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
  without the copy the migration does not start. To go back to 0.18, in
  this order: stop the add-on; delete `/data/ops.db-wal` and
  `/data/ops.db-shm` (a leftover WAL from 0.19.0 would corrupt the restored
  store); copy, do not move, `ops.db.v18.bak` to `ops.db`; install 0.18.x
  and start it. Or restore the add-on's backup taken before the update.
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
  integration critical, and the first-run baseline accepts it. It leaves
  the integration's entity total too, so the integration is still critical
  when every other entity is down. Access hardware is never silenced:
  locks, alarm panels, any cover that is not positively a blind, shade,
  curtain, awning or shutter, and anything whose id or name says door,
  gate or lock are still reported when listed, in the daily check,
  `opsctl health --diff` and the health compares after an update (where a
  listed lock going down rolls the update back), with one warning in the
  log.
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
