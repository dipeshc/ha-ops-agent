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
  attribute source, `-C <repo>`, `safe.directory`, and configuration that
  neutralises fsmonitor, hooks, pagers, attribute files, external diff,
  colour, signature display and every in-repo filter driver; the
  environment holds only `PATH`, `HOME`, `LANG`, `TZ` plus
  `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_ATTR_NOSYSTEM`, `GIT_TERMINAL_PROMPT=0`, `GIT_LITERAL_PATHSPECS=1`,
  `GIT_OPTIONAL_LOCKS=0`; SSH uses the deploy key, the shipped
  `known_hosts` and `StrictHostKeyChecking=yes`. Tests
  `test_githistory.py:152-178`, `test_paths_use_the_shipped_known_hosts`.

## Paths

- C3. A path part is `[a-z0-9][a-z0-9_-]{0,63}`. Allowed:
  `automations/(part/){0,4}part.yaml`, `alerts/part.yaml`,
  `templates/part.yaml`, each only when its kind is enabled; the facts
  document only when it equals the profile's `docs.facts`
  (`docs/part.md`). Denied names (`secrets.yaml`, `ip_bans.yaml`,
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
  keys or duplicate keys). Tests `test_changes.py:280-424`,
  `test_setpoints_outside_the_band_or_templated_are_refused`,
  `test_templates_cannot_point_a_browser_anywhere`.
- C6. The commit is built with plumbing only (`hash-object`, `mktree`,
  `commit-tree`, `update-ref`) on `refs/ops/proposals/<id>` with `main`'s
  head as parent, never touching the index or the working tree, never
  pushed. The row keeps each file's old and new blob and SHA-256, the diff
  (60 000 characters), the base and commit SHAs, and a 7-day expiry.

## Apply, revert, reject

- C7. The panel item shows `change.shown`, the list of new-content SHA-256
  values rendered; `apply` is refused unless the sorted hashes the panel
  sends equal the proposal's. The binding (id, action, commit, base,
  per-file hashes, creation time) is recomputed from the git objects and
  hashed into the approval ([approvals.md](approvals.md) A15). Tests
  `test_changes.py:426-585`.
- C8. **Apply** is one admin request in the panel (no second factor): it
  creates the change approval, confirms it as the same user, and the daemon
  executes under the git lock: policy (APPROVE tier); `observe` mode
  refuses non-document proposals; preconditions (on `main`, clean tree,
  files unchanged since the base, live files equal to git; else the
  proposal is `stale` and its ref dropped); the approval is consumed
  single-use; files are written atomically keeping owner and mode; a Core
  config check runs (skipped for document-only changes); `automation.reload`
  or `template.reload` by top directory, with `reload_custom_templates`
  first for macros; alerts and `configuration.yaml` take effect at the next
  restart, which the agent never performs; `commit_paths` commits exactly
  those paths with the profile's author; the push fetches first and refuses
  when `origin/main` is ahead, then pushes `HEAD:refs/heads/main`. Any
  failure restores the previous bytes, reloads again and escalates with a
  push. Revert is the same pipeline with the base contents. Recovery after
  a restart finishes an interrupted apply. Tests `test_changes.py:426-585,868-901`,
  `test_git_push_refuses_when_remote_ahead`.
- C9. `reject` moves an open proposal to `rejected`, deletes the ref and
  voids its approval; expiry after 7 days does the same.

## Reading history

- C10. `git.status/log/diff/show`: revisions `HEAD` or 7..40 hex with `~N`
  and `^` only; paths `[A-Za-z0-9_@+][A-Za-z0-9._@+/-]{0,200}` without `..`
  and within the readable set; diff and show output limited to readable
  paths, others counted as withheld; log at most 50 entries; 30-second
  timeout. Tests `test_githistory.py:51-150`.
