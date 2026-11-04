# Config changes as git proposals

When the configuration directory is a git repository and the profile turns
git on, the AI may propose changes to automations, alerts, templates and
the facts document as commits on a separate ref. Only the admin applies
one, after a config check; the daemon commits and pushes fast-forward only
and never restarts Core.

Implementation: `opsd/changes.py` (`ChangeManager`), `opsd/changepaths.py`,
`opsd/gitops.py`, `opsd/safegit.py`, `opsd/githistory.py`,
`opsd/configcheck.py`, `prompts/proposals.md`. Tests:
`tests/test_changes.py`, `tests/test_githistory.py`, `tests/test_infra.py`
`test_git_commit_and_push`, `test_git_push_refuses_when_remote_ahead`,
`tests/test_hardening.py` `test_git_pins_githubs_published_host_keys`.

## Enabling

- C1. Git is on iff the profile's `git.enabled` is true; then `author_name`
  and `author_email` are required and `proposals` lists the kinds. The
  `Repo` and the git capabilities (`change.propose`, `facts.propose`,
  `git.status/log/diff/show`) exist only then. A directory without a commit
  on `main` refuses every proposal (`git: main has no commit`). Pushing is
  gated by `limits.git_push` (default on). The deploy key
  (`/data/ssh/github_deploy`) is generated at start when git is on.
- C2. **safegit.** Every git call runs with `--no-pager`, an empty
  attribute source, `-C <repo>`, `safe.directory`, and `-c` configuration
  (which wins over the repository's) that neutralises fsmonitor, hooks,
  pagers, editors, askpass, credential helpers, attribute files, external
  diff, signing (`commit`/`tag`/`push.gpgSign` off and every `gpg.*`
  program), submodule recursion, colour, signature display, and every
  filter, diff and merge driver the repository config defines, whatever its
  name; the environment holds only `PATH`, `HOME`, `LANG`, `TZ` plus
  `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_ATTR_NOSYSTEM`, `GIT_TERMINAL_PROMPT=0`, `GIT_ALLOW_PROTOCOL=ssh`
  (the only transport, whatever `protocol.*` says), empty `GIT_ASKPASS` and
  `SSH_ASKPASS`, a no-op `GIT_EDITOR`, `GIT_LITERAL_PATHSPECS=1`,
  `GIT_OPTIONAL_LOCKS=0`. Before each call the repository's own config is
  listed without following includes, and git is refused (`refusing to run
  git: ...`) when it sets `include.*`, `includeIf.*`, `url.*`,
  `core.gitProxy`, `core.worktree`, `core.alternateRefsCommand`, any
  remote other than `origin`, a remote's `uploadpack`, `receivepack` or
  `vcs`, or a driver name no `-c` key can name. That listing and the call
  are separate processes, so the overrides are defence in depth: the agent
  never runs a git command that converts work-tree content (the only place
  a filter driver runs) or refreshes or edits the live index entry by
  entry (`update-index` re-checks racily clean entries by content). It
  never runs `add`, `commit`, `status`, `update-index`, `checkout` or a
  diff against the work tree. A change is committed from the exact bytes
  the daemon wrote (the approved contents), never read back from the work
  tree: each blob by `hash-object -w --stdin --no-filters`, the tree built
  from `main`'s tree with `mktree` (a changed file keeps its mode, a new
  one is 100644, a deleted one is removed; links and submodules are
  refused), then `commit-tree` and `update-ref refs/heads/main <new>
  <old>`, and `read-tree` brings the index level. Dirty paths come from
  `diff-index --cached`, `ls-files --stage` with each work-tree file hashed
  by `hash-object --no-filters` and its executable bit compared with the
  index mode (unless `core.fileMode` is false, as git does), and `ls-files --others` (`safegit.status_lines`). Fetch and push name the remote by URL, never `origin`: the
  `remote.origin.url` of the repository, refused unless it is a GitHub SSH
  URL (`git@github.com:<owner>/<repo>` or `ssh://git@github.com/...`). SSH
  uses the deploy key, the shipped `known_hosts` and
  `StrictHostKeyChecking=yes`. Tests `test_githistory.py`
  (`test_repo_config_cannot_set_a_signing_program`,
  `test_only_ssh_transport_is_allowed`,
  `test_push_names_a_checked_remote_url_never_the_config`,
  `test_repo_config_a_override_cannot_neutralise_refuses_git`,
  `test_filter_and_diff_drivers_of_any_name_never_run`,
  `test_credential_helpers_and_askpass_never_run`,
  `test_hostile_filter_driver_never_runs`,
  `test_a_filter_driver_added_after_the_overrides_were_built_never_runs`,
  `test_status_lines_match_git_status_on_a_plain_repo`,
  `test_a_late_driver_never_runs_while_committing_even_with_racily_clean_files`,
  `test_a_link_swapped_in_at_the_path_cannot_change_what_is_committed`,
  `test_status_lines_report_an_executable_bit_change`),
  `test_paths_use_the_shipped_known_hosts`.

## Paths

- C3. A path part is `[a-z0-9][a-z0-9_-]{0,63}`. Allowed:
  `automations/(part/){0,4}part.yaml`, `alerts/part.yaml`,
  `templates/part.yaml`, each only when its kind is enabled. The facts
  document (the profile's `docs.facts`, `docs/part.md`) is never a draft
  path: it changes only through `facts.propose`, which appends one fact
  (C5a); `change.propose` naming it is refused. Denied names (`secrets.yaml`, `ip_bans.yaml`,
  `known_devices.yaml`) and every selfguard self-path are refused; new
  directories only below `automations/`. `configuration.yaml` and
  `custom_templates` are reachable only by the update fix
  ([updates.md](updates.md) U23). Tests `test_changes.py:161-192`.

## Drafting and creation

- C4. Chat and investigate runs get the Write tool and an empty drafts
  directory; the guard allows a write only to an allowed path directly
  under it, at most 64 KB. The daemon reads each draft without following
  symlinks (regular file, one link, owned by `ops`, 64 KB) and deletes it
  after the attempt. Tests `test_changes.py:179-200,612-648`.
- C5. `change.propose` (3 per run, 10 a day; chat and investigate only)
  needs a Conventional Commits subject, at most 3 files of 64 KB, a
  non-empty `why`, at most 5 open and 5 created per day (refusals recorded,
  20 a day), and content that differs from `main`. Content rules: no
  `!secret`, `!include` or `!env_var`; no shell, REST or Python commands;
  no `hassio.*`, restart or stop; no lock, alarm or cover actions (garage
  covers included); notify only to the admin's service or `critical_alerts`;
  then the `configcheck` allowlist (automation, template and alerts-package
  shapes, allowed action domains, literal entity targets of the same
  domain that exist in the registry, media players volume and transport
  only, climate setpoints 10..30 degrees C or 50..86 degrees F, a push data
  allowlist, at most 20 000 YAML nodes, no tags, anchors, aliases, merge
  keys or duplicate keys; a `repeat` runs a literal `count` of 1..10 or a
  literal `for_each` list of at most 10 items, never `while` or `until`;
  no `webhook` trigger; a `time_pattern` trigger without `seconds`).
  Helpers that any tracked YAML file doing more than the allowlist reacts
  to (automations, scripts, packages, `configuration.yaml`; a file that
  cannot be read or is over 1 MiB makes every helper target refused) are
  never targets. An automation's `id` and alias (case-insensitive) may not
  be another tracked automation's. The old version of each file is checked
  as well: a file that already calls an action outside the allowlist (a
  lock, the alarm, a cover, a switch, a script), uses devices, areas,
  events, scenes or blueprints, or carries YAML tags is never changed by a
  proposal, and an existing alert is never removed and keeps its
  `entity_id`, `state`, `repeat`, `skip_first`, `can_acknowledge` and
  notifiers (its name, message, title and push data may change). Tests `test_changes.py:280-424`,
  `test_setpoints_outside_the_band_or_templated_are_refused`,
  `test_templates_cannot_point_a_browser_anywhere`,
  `test_a_file_that_already_does_more_is_never_rewritten`,
  `test_an_alert_or_its_notifier_is_never_removed`,
  `test_an_automation_id_or_alias_is_never_taken_twice`,
  `test_loops_webhooks_and_fast_time_patterns_are_refused`,
  `test_helpers_watched_by_any_config_yaml_cannot_be_targeted`.
- C5a. `facts.propose` (and the panel's Remember) is the only way the
  facts document changes: the daemon appends one cleaned fact of at most
  500 characters to its own section, and refuses when the document would
  grow past 16 KiB (it is read into every prompt). Test
  `test_the_facts_document_changes_only_by_one_appended_fact`.
- C6. The commit is built with plumbing only (`hash-object`, `mktree`,
  `commit-tree`, `update-ref`) on `refs/ops/proposals/<id>` with `main`'s
  head as parent, never touching the index or the working tree, never
  pushed. The row keeps each file's old and new blob and SHA-256, the
  whole diff (one unified diff per file, never cut), each file's diff
  length in its summary, the base and commit SHAs, and a 7-day expiry. A
  proposal whose diff is longer than 200 000 characters is refused, so
  the admin always reviews every file in full. Test
  `test_a_diff_is_never_cut_and_each_file_is_counted`.

## Apply, revert, reject

- C7. The panel item shows `change.shown`, the list of new-content SHA-256
  values rendered; `apply` is refused unless the sorted hashes the panel
  sends equal the proposal's. Its facts show, per file, the lines added
  and removed and the actions and target entities the change adds and
  removes, computed by the daemon from both versions. A proposal whose
  stored diff does not show every file in full (the per-file lengths do
  not add up, or a row from before them that reaches the old 60 000-
  character cut or misses a file) is never approvable: its item asks only
  to reject it, and `apply` is refused. Tests
  `test_a_change_not_shown_in_full_cannot_be_approved`,
  `test_the_review_shows_what_the_change_adds_and_removes`. The binding (id, action, commit, base,
  per-file hashes, creation time) is recomputed from the git objects and
  hashed into the approval ([approvals.md](approvals.md) A15). Tests
  `test_changes.py:426-585`.
- C8. **Apply** is one admin request in the panel (no second factor): it
  creates the change approval, confirms it as the same user, and the daemon
  executes under the git lock: policy (APPROVE tier); `observe` mode
  refuses every proposal, the facts document included; preconditions (on `main`, clean tree,
  files unchanged since the base, live files equal to git; else the
  proposal is `stale` and its ref dropped); the approval is consumed
  single-use; files are written atomically keeping owner and mode; a Core
  config check runs (skipped for document-only changes); `automation.reload`
  or `template.reload` by top directory, with `reload_custom_templates`
  first for macros; alerts and `configuration.yaml` take effect at the next
  restart, which the agent never performs; `commit_files` commits exactly
  the approved contents of those paths (C2) with the profile's author; the push fetches first and refuses
  when `origin/main` is ahead, then pushes `HEAD:refs/heads/main`. Any
  failure restores the previous bytes, reloads again and escalates with a
  push. Revert is the same pipeline with the base contents. Recovery after
  a restart finishes an interrupted apply. Tests `test_changes.py:426-585,868-901`,
  `test_git_push_refuses_when_remote_ahead`.
- C9. `reject` moves an open proposal to `rejected`, deletes the ref and
  voids its approval; expiry after 7 days does the same.

## Reading history

- C10. `git.status/log/diff/show`: revisions `HEAD` or 7..40 hex with `~N`
  and `^` only; `diff REV` compares REV with the index (never the work
  tree) and `status` lists tracked changes as `safegit.status_lines`
  computes them (C2); paths `[A-Za-z0-9_@+][A-Za-z0-9._@+/-]{0,200}` without `..`
  and within the readable set; diff and show output limited to readable
  paths, others counted as withheld; log at most 50 entries; 30-second
  timeout. Tests `test_githistory.py:51-150`.

## Fork rebases

- C11. **Proposing.** `fork.propose` (verb `propose-rebase <domain> --keep
  <ids> --reason <text>`, 1 per run, 3 a day, refund on refusal; chat,
  investigate and maintenance-plan runs only, never triage) records a
  proposal to rebase a custom integration that forks a Core one onto the
  installed Core version. The AI names only the domain, the reason and
  which of the fork's own changes to keep: hunk ids `<file>#<n>` from
  `opsctl core-compare <domain> --fork` ([capabilities.md](capabilities.md)
  K14), or `none`. No argument carries file content. The domain must be a
  custom integration on this install, never the agent (`self_path`), and
  Core must have an integration of that name at the installed version.
  Implementation `opsd/forkrebase.py`; tests `tests/test_forkrebase.py`.
- C12. **The build** (daemon only). Base: Core's files for the installed
  version, each checked against GitHub's blob SHA for that tag (K14). The
  fork's origin: the newest tag among the installed one and the `.0`
  releases of the last 18 months whose files differ least from the fork's
  (changed lines; a fork file the tag lacks counts whole; a tie goes to the
  newer tag). The fork's own changes: the unified-diff hunks (3 lines of
  context) from the origin to the fork, numbered per file. Each kept hunk
  is applied to the base by its exact origin lines and context, which must
  occur exactly once, with no two hunks touching the same lines; anything
  else refuses with the hunk id and the reason, never a guess. The result
  covers the files the fork, the base and the origin all have; every other
  fork file stays as it is and no file is added.
- C13. **Content checks.** Every `.py` file parses (`ast.parse`);
  `manifest.json` is a JSON object that keeps the fork's domain and gets
  `version` set to the Core version (a custom integration needs one); no
  import (`import x`, `from x import y`, as level, module and name) that
  neither the base nor the fork has; a relative import names a module the
  fork has and never leaves the integration; at most 1 MiB in all and
  256 KiB per file. A result identical to the fork is refused ("already
  matches").
- C14. **The record.** A proposal is a store key-value record
  (`fork:proposal:<16 hex>`, no schema change) plus its result's bytes
  under `/data/fork-rebase/<id>/new/` (directories 0700, files 0600):
  domain, Core version, origin tag, kept hunk ids, the reason (shown only
  as the agent's text), each file's SHA-256 before and after, the result
  and fork hashes, the hunks with their counts, the kept hunks' text
  (fenced) and the diff from the fork as it is to the result (32 KiB at
  most). States: `open`, `applying`, `applied`, `rolled_back`, `failed`,
  `rejected`, `stale`. A new proposal for the same fork makes an open one
  `stale`; the newest 20 are kept, and a finished one beyond them is
  deleted with its files. It never writes the fork; applying is the update
  pipeline's ([updates.md](updates.md) U26–U31), after the admin's
  approval ([approvals.md](approvals.md) A18). Reject moves an open one to
  `rejected` and voids its approval.

