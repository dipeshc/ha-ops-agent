# Changelog

## 0.21.0

The agent can now answer "is this fork still needed?" itself, and bring a
fork up to date with one approval. No store change (the store stays at
schema 19); upgrading from 0.20.0 needs nothing, and existing options stay
valid.

Custom integrations that fork a Core one:

- New `opsctl core-compare <domain>`: the add-on fetches Home Assistant
  Core's own files for the exact Core version you run (from GitHub's
  contents API and raw file host only, each file checked against its git
  blob SHA, at most 40 files of 256 KiB, cached under `/data`) and
  compares them with `custom_components/<domain>/`: a summary (files only
  in the custom integration, only in Core, changed lines per file) and a
  unified diff per file, redacted, fenced as data, with one 32 KiB budget
  for file contents and a clear marker where anything is cut.
  It is read-only and takes no approval. Chat, investigations ("Plan a
  fix") and the maintenance plan can use it; the nightly triage cannot.
  The agent reports what it shows as facts and cites the diff. The AI's
  own sandbox still has no network: the add-on fetches.
- `core-compare <domain> --fork` also finds the Core release the fork was
  copied from (the closest of the last six monthly releases, at most six
  more GitHub API calls) and numbers the fork's own changes against it.
  GitHub asking it to slow down never holds up the release-note checks.
- New `opsctl propose-rebase <domain> --keep <ids> --reason …`: the agent
  proposes rebasing the fork onto your Core version, keeping only the
  changes it names. It never writes file content: the add-on builds the
  files from Core's source and your fork, refuses anything that does not
  apply cleanly, does not parse, imports something new or adds a file,
  and sets the manifest's `version`. The proposal is an item on Home
  ("Rebase onto Core") with the full diff, the changes kept and the Core
  version, and a card in the chat when "Plan a fix" made it.
- A fork of anything that opens the house (locks, the alarm, garage doors,
  gates), or of a domain on the never-reload list or your profile's
  `reload.deny`, is never rebased.
- Approving it ("Rebase tonight" or "Rebase now") runs it as an update job:
  an encrypted backup, the old files kept, the new files written in place
  (mode and owner kept; the integration's directory is opened level by
  level without following links, so a link swapped in for it or for
  `custom_components` is never followed, and each file goes through a
  fresh randomly named temporary file), Core's config check, one
  Core restart, the health check (locks and the alarm included) and, if
  anything is worse or the restart fails, the old files back with a second
  restart and a push. A good rebase is committed if git tracks the files.
  The approval is bound to the fork's files, the Core version and the
  exact result, so any change in between voids it.

Safety:

- A Core restart now passes the self-guard only inside one update job's
  own scope (a HACS integration the agent updated, or an approved fork
  rebase), never otherwise.

Maintenance:

- A Core log deprecation is tied to the integration that logged it (or the
  one Core's own report names), never to words like "Core" or "helpers",
  and is worded as what it is, still as the log's claim: "The Core log
  says acme_fan uses `old_api`, which Home Assistant removes in 2027.8". A
  line with no reliable integration is dropped, and so is a log finding an
  earlier version titled "Core".
- The nightly maintenance plan proposes a fork rebase only for a finding
  from a repair or the release notes, never from a log line; you can
  still ask for one with Plan a fix or in the chat.

Panel:

- "Ask the agent" fills in "Its title: \"…\"" (quoted) instead of calling
  every title "the device's own text".

## 0.20.0

The agent handles day-to-day items itself and pulls you in only when it
needs you, and every item on Home has something you can do with it. No
store change (the store stays at schema 19); upgrading from 0.19.1 needs
nothing, and existing options stay valid.

AI budget:

- One honestly labelled run-time budget replaces the estimated share of
  your subscription: the agent's runs may take `usage.max_share_percent`
  (default 5) of your plan's weekly hours (`claude.plan`) per rolling 7
  days, and a fifth of that per 5 hours. Runs stop at 80 % of it, chat and
  investigations at half of that (so health checks and update reviews
  always keep the other half), and a run's time limit is trimmed to what
  is left. It still yields when your account is above 90 % of a window, and
  the daily run caps, turn and cost caps per run, the kill switch and the
  usage-limit backoff are unchanged. Refusals keep their codes
  (`share_cap:<window>` now means the run-time cap).
- `claude.plan: auto` means pro: Claude Code's output names no plan tier,
  so the detection is removed. Set `claude.plan` if you have a Max plan.
- New optional `limits.daily_max_usd`: the most a day's runs may cost, as
  the provider reports it (meant for API keys; empty or 0 is no cap). Each
  run's cost cap is trimmed to what is left, and runs stop below 0.05 USD.
  Without it a day is bounded only by the run caps, about 30 USD at the
  defaults.
- `sensor.ops_agent_ai` shows `run_time_5h_s`, `run_time_5h_cap_s`,
  `run_time_7d_s`, `run_time_7d_cap_s`, `account_5h_percent`,
  `account_7d_percent` (measured, or null), `cost_today_usd` and
  `daily_max_usd`. Removed: `plan`, `max_share_percent`,
  `share_stop_percent`, `agent_share_5h_percent`, `agent_share_7d_percent`
  and the two `*_estimated` flags; update any card or automation that read
  them.
- The panel's Budget rows read "Run time, 7 days: N of M min", "Run time,
  5 hours: …", "Account, 7 days: X %" when measured, and "Cost today" when
  a daily cap is set.
- `GET api/usage` is removed (nothing used it).
- Account utilisation is read in the unit of each reading: any decimal
  value in it means fractions of the window (0.09 is 9 %), otherwise any
  value over 1 means percents; a reading of only 0s and 1s uses the unit
  learned from the last clear one (percent, with a warning, until one is
  seen). Over the limit counts as 100 %. 0.19.1 read every reading as a
  percent, so a fractional reading such as 0.95 (95 %) was taken for
  0.95 % and the agent could miss that your account was near its limit and
  fail to yield. This fixes it.
- The "claude run finished" log line carries each window's utilisation
  exactly as reported, with its JSON type, and the unit it was read in.

Needs you only when needed:

- Deprecations ask 30 days before their deadline (the month the removal
  release ships), not two releases ahead. Until then the finding and its
  one-tap fix (a config change or a dashboard plan) wait in Coming up,
  ready to apply: a fix stays open until a week after its finding starts
  asking (90 days for a finding without a deadline), and maintenance fixes
  have their own limit of 5 open, so they never block other proposals.
  Findings without a deadline never ask. Hiding a finding only quiets it:
  a hidden removal still holds back a Core update that would break it, and
  the finding comes back 30 days before its deadline (Hide is not offered
  while a finding asks).
- An update whose release age is unknown waits (checked again every night)
  instead of asking; it asks after 30 days, or at once when a repair or a
  breaking change touches it.
- The agent's triage run pushes only when you must act or decide: its push
  needs a reason (`needs_action`, `physical_task` or `security`); for
  information it writes a note in History (`--reason note`), and a push
  without a valid reason is refused. Its own reminders and follow-ups
  follow the same rule (pushed only with a push class, never with a Reply
  button; otherwise a note in History). The daemon's own
  critical pushes (locks, the alarm, access hardware, Core down, backup
  failures and the rest) are unchanged.
- No longer in Needs you: an update install that failed before or during
  the install call and that the next night retries by itself (an
  automatic update still offered, not approved by you, with automatic
  updates running, and the entity's first failure in 14 days; anything
  else asks at once), an approved update Home Assistant no longer offers,
  a config change that failed and was cleanly undone (one that may be
  half applied asks you at once, urgently), a change that cannot be shown
  in full (it expires by itself), a fact noted without a proposal, info health items,
  and a dashboard change that was refused or restored. Items an earlier
  version put in Needs you are recomposed at start.

Every item is actionable:

- Every item has "Ask the agent": the chat opens with the item attached
  and a question naming the item by its id, with its title quoted as the
  device's own text, and the agent reads the item's
  record (fenced as data) with your message.
- Maintenance findings and suggestions have "Plan a fix" (an
  investigation in the chat that writes a plan and proposes the fix where
  it may, or says plainly that you or an engineering session must do it),
  "Remind me" in 30 or 90 days or near the deadline, and "Hide until it
  changes" (the former "Not relevant here").

For overlays and integrators: the overlay API is unchanged. Internal
changes that code outside this repository could notice:
`opsctl notify` needs `--reason` (a push class or `note`), and so does
`opsctl remind` for the agent's own tasks; `Capability` gains `quota_free`;
`budget.utilisation` takes the payload's unit (`budget.reading_unit`);
`build_proposal` takes `expires_at`; a failed update job records
`target.failed_from`; `RunResult.plan_hint`,
`claude_runner.plan_hint`, `budget.run_share`, `budget.share_inconclusive`,
`Caps.estimated_share`, `Caps.share_stop` and `Store.run_shares` are
removed; `Store.finish_run` no longer takes `share_5h`, `share_7d` or
`share_estimated`; `Caps.from_options` no longer takes `detected_plan`;
`budget.resolve_plan` takes the option only; `policy.Decision` gains
`age_unknown`; the effect sink gains `note_message`; activity entries gain
the quiet kind `note`.

## 0.19.1

Hardening and fixes. No store change; upgrading from 0.19.0 needs nothing.

Health check and pushes:

- Each piece of access hardware that goes down (a lock, an alarm panel, a
  cover that is not positively a blind or the like, a switch, button,
  script, scene, input boolean or valve named like a door, gate or lock; never a
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
  command, another work tree or a remote other than `origin`. The agent no
  longer runs any git command that converts work-tree files or edits the
  index entry by entry (`add`, `commit`, `status`, `update-index`, a
  work-tree diff): an applied change is committed from the exact approved
  contents (`hash-object --stdin --no-filters`, `mktree`, `commit-tree`,
  `update-ref`, then `read-tree`), never read back from the work tree, and
  changed files (content, or the executable bit unless `core.fileMode` is
  false) are found by hashing them
  without filters. `opsctl git-diff REV` now compares REV with the index.
  Fetch and push use `remote.origin.url`, which must be a GitHub SSH URL
  (`git@github.com:<owner>/<repo>`).

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
- `Repo.commit_paths(paths, message)` is replaced by
  `Repo.commit_files({path: bytes or None}, message)`, which commits the
  given contents (None deletes) instead of reading the work tree.

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
