# Project profile — `ha-ops-agent`

The prose half of this project's delivery-team configuration. The machine-read
half (gate commands, backlog location, engine, limits) is in `config.toml`
beside it. Every role reads this file first and treats it as authoritative.

---

## Repo

- **Package manager / runtime:** `uv` + Python 3.14 for development only. The
  add-on image runs the same code under Alpine's `python3` with `py3-yaml` and
  installs nothing from `pyproject.toml`; the only runtime dependency outside
  the standard library is PyYAML.
- **Layout:**
  - `ha_ops_agent/`: the Home Assistant add-on (`config.yaml`, `Dockerfile`,
    `build.yaml`, `translations/`, `rootfs/`).
  - `ha_ops_agent/rootfs/opt/ops/opsd/`: the daemon, one flat Python package
    (`opsd`). Every module is listed in `docs/spec/README.md`.
  - `ha_ops_agent/rootfs/opt/ops/{bin,panel,prompts,ssh}/`: the `opsctl`
    client and guard hook, the ingress panel (static HTML/JS), the AI prompts
    and result schemas, pinned GitHub host keys.
  - `ha_ops_agent/rootfs/etc/`: s6 init and the AI provider's managed settings.
  - `tests/`: pytest suite against a fake Home Assistant (`tests/conftest.py`,
    `tests/fixtures/`). No network, no Supervisor, no AI provider.
  - `docs/spec/`: the specification. `docs/panel-api.md`: the panel wire
    contract, referenced by the spec. `docs/backlog/`: work items.
  - `examples/profile.yaml`: the commented example house profile.

## Quality gate

The commands are in `config.toml` `[gate]`: `ruff format --check`,
`ruff check`, `mypy --strict` over `opsd`, and `pytest` with coverage.

- **Not covered:** the gate never builds the add-on image (CI builds it for
  both architectures without publishing; a change to the `Dockerfile`,
  `build.yaml` or pinned packages is verified only there), never runs against
  a real Supervisor, Core, websocket or AI provider (all are faked), never
  exercises the panel in a browser (its static files are only parsed for
  validity), and never runs the s6 init script. Green means the four commands
  passed, nothing more.
- **Notes:** the suite runs in well under a minute with no services. Tests
  use the real clock (`tests/conftest.py` `NOW`). Overlays under
  `opsd.house.*` are not part of this repository; mypy ignores their imports.

## Backlog

The directory is in `config.toml` `[backlog]`.

- **Conventions doc:** `docs/backlog/README.md`.
- **Item lifecycle:** the landing commit deletes the item file.

## Specification source of truth

- **Docs are spec?** Yes. `docs/spec/` is the specification of the daemon,
  the panel, the add-on options and the house profile. A doc-vs-code mismatch
  means the **code** is wrong. A change that alters externally observable
  behaviour edits the spec in the same commit; a missing spec edit is a review
  defect. Roles may sharpen a spec statement, never re-decide one.
- **Docs root:** `docs/spec/`. `README.md`, `ha_ops_agent/DOCS.md`,
  `ha_ops_agent/CHANGELOG.md` and `examples/profile.yaml` are user-facing
  documentation that follows the spec; they are not spec themselves and must
  not contradict it.
- **Decision records:** `n/a`.
- **Conventions doc:** `n/a`: match the surrounding code and the git log.
  Conventional Commits (`type(scope): description`, imperative mood, one
  logical change per commit).

## Repo-wide invariant guards

All of these run in the full gate; they are listed so triage and reviewers
know which a change touches. `config.toml` `[gate] guards` carries the
commands and triggers.

- `tests/test_selfguard.py`: the agent never targets itself, the Supervisor or
  the host through any Supervisor, Core REST or websocket call, update job,
  reload or proposal path, except the single-use approved OS update flow
  (`docs/spec/selfguard.md`). Run whenever a call site or a URL-building
  function changes.
- `tests/test_privsep.py` and `tests/test_guard.py`: the AI process runs as
  the unprivileged `ops` account, reaches only the secret-free export and the
  drafts directory, never a secrets file or directory in any letter case, and
  the guard hook fails closed (`docs/spec/privsep.md`, `docs/spec/ai-runner.md`).
- `tests/test_capabilities.py`, `tests/test_rpc_effects.py`,
  `tests/test_opsctl.py`: the capability registry is the single source of the
  `opsctl` verbs, the guard parser and the RPC schema; the socket refuses
  unknown peers and oversize requests (`docs/spec/capabilities.md`).
- `tests/test_reload.py`: locks, alarms, garage doors, access covers, radios
  and core plumbing are never reloaded, whatever the options or the profile
  say; once per domain per 10 minutes (`docs/spec/reload.md`).
- `tests/test_approvals.py`, `tests/test_items.py`, `tests/test_panel.py`: an
  approval is admin-only, bound to the shown plan hash and revision, single
  use and expiring; the panel's CSRF and admin checks (`docs/spec/approvals.md`,
  `docs/spec/panel.md`).
- `tests/test_hardening.py`, `tests/test_infra.py`: pinned base images and
  packages, pinned GitHub host keys, the fetch allowlist, fresh AI state per
  run, triage fencing of untrusted text, the guard decision log.
- `tests/test_profile.py`, `tests/test_options.py`: the strict profile schema,
  fail-closed options, and that no profile or option can loosen a built-in
  limit (`docs/spec/profile.md`, `docs/spec/configuration.md`).

## UI surfaces

- **How to boot the app locally:** `n/a`. The panel is served by the daemon
  behind Home Assistant ingress and needs the Supervisor; there is no
  standalone dev server. Visual QA happens on a real install.
- **Routes to sweep:** `n/a`.
- **Viewports:** `n/a`.
- **Roles / auth states:** `n/a`.
- **Themes:** `n/a`.

## Exploration log

- **Location:** `n/a` (defaults to `docs/spec/exploration.md` if ever used).

## Cross-surface parity

`n/a`: a single surface (the daemon and its panel).

## Sanctioned direct-write paths

- `docs/backlog/`, `.delivery-team/`.

## Project-specific content rules

- **No personal or house data in the repository.** Examples, fixtures, tests,
  docs and commit messages use synthetic names only (made-up integrations,
  entities, people, hosts, slugs). Nothing may come from a real install: no
  real entity ids, device names, addresses, tokens, backup names, camera URLs,
  logs or push texts, and no references to private documents or private
  repositories. `tests/fixtures/` is the sample house; add to it rather than
  pasting from a live system.
- **LLM-agnostic wording.** Docs, code comments, panel text and commit
  messages say "the agent", "the AI" or "the model". Name a provider or a
  product (Claude, Claude Code, Anthropic) only where the behaviour is
  genuinely provider-specific: the runner module for that provider, its
  token or API-key handling, its managed settings, and the `llm.provider`
  option values. `docs/spec/ai-runner.md` owns that section.
- **Untrusted text is data.** Any new prompt, push or log path that carries
  device names, logs, release notes or chat text fences it as data; it is
  never interpolated into instructions.
- **No AI attribution.** Commit messages and pull requests carry no
  `Co-Authored-By`, `Generated with`, session links or similar trailers.
- **Commit identity:** `Dipesh Chauhan <me@dipeshc.com>`.
- **Licensing:** Apache-2.0 headers are not required; `NOTICE` lists
  third-party attributions and must be updated when a dependency is added.

## Verification environment

- A plain checkout with `uv sync --locked` runs the whole gate. No
  credentials, services or data sets are needed, and nothing in the suite
  reaches the network (`tests/test_infra.py` enforces the fetch allowlist).
- The image build needs Docker with buildx (CI does it with QEMU for
  `aarch64`); it is not part of the gate.
- Live verification (a real Supervisor, locks, an AI token) is owner-only and
  happens on a private install; roles never have it and must say so instead
  of claiming it.
- Commits on `main` are signed by the owner; role commits need not be.
