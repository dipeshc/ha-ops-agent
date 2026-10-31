# The house profile

The profile is the one place a home's specifics live. It is optional: without
it the agent is generic (no fragile pieces, no watched entities, no reloadable
integrations, git off, no house documents).

Implementation: `opsd/profile.py` (schema and loading), `opsd/guard.py`
`valid_read_entry` (document paths), `opsd/configcheck.py` `parse` (YAML).
Tests: `tests/test_profile.py`, `tests/test_guard.py`
`test_house_documents_are_readable`,
`test_a_profile_cannot_make_secrets_or_the_agent_readable`.

## Location and reading

- P1. The profile is `ops/profile.yaml` under the Home Assistant configuration
  directory. It is read once, when the daemon starts (`config.py` `Options.load`,
  called from `__main__`). A change takes effect on the next add-on restart.
- P2. `ops/` MUST be a real directory and the profile a regular file; a
  symlinked profile MUST be refused (`O_NOFOLLOW`). Test
  `test_a_symlinked_profile_is_refused`.
- P3. The file MUST be at most 64 KiB of UTF-8.
- P4. YAML is parsed by the same strict parser as the config check: one
  document, no directives, no anchors or aliases, no explicit tags
  (`!secret` included), no merge keys, no duplicate keys. The top level MUST
  be a mapping. An empty or comment-only file is a loaded, generic profile.
  Test `test_the_schema_is_strict`, `test_an_empty_profile_is_generic_but_loaded`.

## Refuse the whole file

- P5. Any schema error refuses the whole file: the daemon runs with the
  generic profile and records one problem,
  `house profile ops/profile.yaml: <reason>`. Reasons name the key and the
  problem and never echo a long value (unknown keys are cut to 40 characters).
- P6. A profile problem fails the agent closed exactly like an invalid option
  (see [configuration.md](configuration.md) C4): no acting, no automatic
  updates, no AI runs, a warning health item each check, and the reason on the
  panel's System sheet. Test `test_a_refused_profile_fails_the_agent_closed`.

## Schema

Every key is optional; an unknown key at any level refuses the file. Every
list holds at most 50 entries and MUST NOT repeat an entry.

| Key | Constraint |
| --- | --- |
| `notify_service` | empty or `mobile_app_[a-z0-9_]{1,64}` (no `notify.` prefix). The push target when the `notify.service` option is empty. |
| `self_slug` | `[a-z0-9]{1,32}_ha_ops_agent`. Only used to build panel links in pushes; selfguard protects every `*_ha_ops_agent` slug regardless. When absent the slug is derived from the container hostname, else the default `local_ha_ops_agent`. |
| `overlays` | names from `vacuums`, `localpatches`, `suppress`. Each MUST be importable as `opsd.house.<name>`; this repository ships none, so naming one refuses the file in a stock build. |
| `house_notes` | string of at most 4000 characters, no control characters except tab, newline and carriage return. Reaches the AI only as fenced data. |
| `health.power_entity`, `health.remote_entity` | empty or `binary_sensor.[a-z0-9_]{1,128}`. Watched by the daily check. |
| `updates.need_approval` | list of `update.[a-z0-9_]{1,128}`; the union with the option of the same name. |
| `updates.watched_entities` | list of `<domain>.<object>` ids that MUST still be available after an update (see [updates.md](updates.md)). |
| `updates.fragile[]` | mappings with required `name` (`[a-z0-9][a-z0-9_.-]{0,63}`, unique), `tier` (`approve`, `pinned` or `never`; `auto` is refused), `reason` (one line, at most 300 characters); at least one of `entities` (update entity ids), `repos` (`owner/name`), `domains`; optional `bumps` as a subset of `patch`, `minor`, `major`, `unknown`. |
| `updates.version_gates[]` | mappings with required `integration`, `core_from` (a version string with at least two components) and `reason`; optional `patch` needs the `localpatches` overlay. |
| `reload.domains`, `reload.deny` | lists of `[a-z0-9_]{1,64}`. A `domains` entry that is on the built-in never-reload list, or also in `deny`, refuses the file. |
| `git.enabled` | bool, default false. When true, `author_name` and `author_email` are required. |
| `git.author_name` | `[^\x00-\x1f\x7f<>]{1,64}`; default `Ops Agent`. |
| `git.author_email` | a bounded email; default `ops-agent@localhost.localdomain`. |
| `git.proposals` | subset of `automations`, `alerts`, `templates`; default all three. |
| `docs.runbook` | a path ending `.md` that passes the readable-entry rules. |
| `docs.facts` | as `runbook`, and additionally `docs/[a-z0-9][a-z0-9_-]{0,63}.md`. |
| `docs.readable` | list of readable entries: a `.md` file path, or a top-level directory name with no `/` and no `.`. |

Tests: `test_the_schema_is_strict`, `test_a_fragile_piece_can_only_hold_back`,
`test_self_slug_must_name_the_agent`, `test_git_author_cannot_inject_options`,
`test_proposal_kinds_are_the_three_the_config_check_knows`.

## Readable entries

- P7. A readable entry has at most 4 path components, each matching the
  guard's entry pattern; it MUST NOT be a selfguard self-path (the add-on's
  own directories, `.git`, the deploy key) and MUST NOT contain a denied file
  or directory name in any letter case (`secrets.yaml`, `.storage`, a
  `secrets` directory, and the rest of `guard.DENY_FILES` / `DENY_DIRS`).
  Tests `test_documents_cannot_reach_secrets_or_the_agent`,
  `test_a_secrets_directory_is_never_a_house_document`,
  `test_deny_files_in_any_letter_case`.
- P8. The profile's documents are added to the base readable set
  (`README.md` and the base readable directories) for the run copy; a profile
  can widen what the AI reads only within these rules. Without git, the run
  copy exports only the named `.md` files, never whole directories. Test
  `test_the_export_without_git_is_a_strict_allowlist`.

## Nothing weakens a safety rule

- P9. Fragile tiers only hold updates back (`approve`, `pinned`, `never`);
  there is no tier that promotes an update.
- P10. `reload.deny` only adds to the built-in never-reload list; neither
  `reload.domains` nor the `capabilities.reloadable_domains` option can name a
  never-reload domain. Tests `test_reload_deny_only_adds`,
  `test_a_profile_listing_a_never_reload_domain_is_refused`,
  `test_lock_alarm_and_garage_vendors_are_never_reloaded`.
- P11. `house_notes`, the runbook and the facts document are data for the AI
  (fenced, size-capped); they grant no capability. Proposal rules derived from
  the profile are empty unless git is enabled and never reach the AI as
  instructions. Test `test_proposal_rules_never_reach_the_agent_or_config`.

## Merging with options

- P12. Push target: the `notify.service` option, else the profile's
  `notify_service`, else no push target.
- P13. Reload allowlist: the `capabilities.reloadable_domains` option when
  non-empty, else `reload.domains`, else none. Never-lists apply in every case.
- P14. `updates.need_approval` is the union of the option and the profile.
  Test `test_the_house_profile_only_adds_need_approval`.
- P15. Available capability groups are the enabled overlays plus `git` when
  `git.enabled` is true; a capability whose group is unavailable is refused
  and not offered to the AI. Tests
  `test_without_git_proposals_and_overlays_are_refused`,
  `test_the_prompt_offers_only_what_this_install_has`.

## Example

`examples/profile.yaml` is the commented, synthetic example; it MUST parse
(`test_the_published_example_profile_is_valid`), as must the test fixture
`tests/fixtures/profile.yaml`.
