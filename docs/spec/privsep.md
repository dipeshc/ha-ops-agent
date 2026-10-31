# Privilege separation and the self-test

The daemon runs as root inside the add-on and holds the Supervisor token.
The AI runs as the unprivileged `ops` account and can reach only the run
copy, its own state directory and the opsctl socket. The boundary is set at
start, checked before every run and checked again by the self-test.

Implementation: `rootfs/etc/s6-overlay/scripts/init-ops.sh`,
`opsd/privsep.py`, `opsd/selftest.py`, `opsd/rpc.py` (peer check),
`ha_ops_agent/Dockerfile`. Tests: `tests/test_privsep.py`,
`tests/test_selftest.py`, `tests/test_opsctl.py`, `tests/test_hardening.py`.

## Accounts and files

- V1. The image creates group and user `ops` (uid and gid 1001, shell
  `/sbin/nologin`, home `/data/claude-ops/home`), strips every setuid and
  setgid bit, and runs the daemon as root with `python3 -I` and umask 077.
- V2. `init-ops.sh` at every start: `/data` is 0711 and every entry in it
  except `claude-ops` loses group and other bits; `/data/claude-ops` and its
  `home` are `ops:ops` 0700; `/mnt/ha` and `/run/ops` are 0700;
  `/run/s6/container_environment` is 0700; `/run/ops-claude` is root 0755
  and `/run/ops-sock` root:ops 0750; `/run/ops-drafts` is removed; a deploy
  key is generated only when the profile enables git. The config mount is
  `/mnt/ha/config` (read-write) and the backup mount `/mnt/ha/backup`
  (read-only); neither is reachable by `ops`.
- V3. The AI process is started as `ops` with no supplementary groups, a
  new session, `no_new_privs`, `nice 10`, and the environment allowlist in
  [ai-runner.md](ai-runner.md) AP3. Test `test_runner_passes_user_and_group`.

## The pre-run check

- V4. Before every run, `isolation_problems` resolves symlinks and checks
  the execute bits of every ancestor and the read (file) or execute
  (directory) bit of each sensitive path against the `ops` uid and gids:
  `options.json`, `/data/secrets`, the deploy key, `state.json`, `ops.db`,
  its pre-upgrade copy `ops.db.v18.bak` ([storage.md](storage.md) S12),
  `/mnt/ha/config`, its `secrets.yaml` and `.storage`,
  `/run/s6/container_environment`, `/run/ops`. Any reachable path, a missing
  `ops` account or a failed self-test refuses the run (`isolation_failed`).
  Tests `test_reachable_by_follows_permission_bits`,
  `test_isolation_problems_reports_exposed_paths`,
  `test_directory_with_execute_only_counts_as_reachable`,
  `test_isolation_failure_blocks_claude`.

## The socket boundary

- V5. The daemon reads `SO_PEERCRED` on every connection: a root peer or a
  uid outside the allowed set is refused before reading; a mutating verb
  needs an active run whose root process is an ancestor of the peer's pid.
  The directory mode (root:ops 0750) keeps every other uid from connecting.
  Tests `test_rpc_rejects_unexpected_peer`, `test_socket_denies_unknown_peer_and_overload`.

## The self-test

- V6. Runs at daemon start and on `POST api/selftest`; the result is written
  to `/data/selftest.json`, logged and published on the status sensor. The
  AI never runs unless the last result is `ok`.
- V7. Checks: the `ops` uid is 1001; a deliberately exposed probe file is
  flagged (the check itself trips); nothing else is flagged; a probe run as
  `ops` under `no_new_privs` cannot read `options.json`, `/data/secrets`,
  the deploy key, the relay cache, `state.json`, `ops.db` and its
  pre-upgrade copy, `/mnt/ha`, the
  config, `secrets.yaml`, `.storage/auth`, `.storage/core.config_entries`,
  the backup mount, the daemon's and PID 1's `/proc/*/environ`, `/run/ops`
  or the s6 environment, while it can read the probe; the probe runs with
  the `ops` identity and no extra groups; `no_new_privs` is set; no setuid
  or setgid file exists outside `/proc`, `/sys`, `/dev`, `/run`, `/mnt/ha`
  and `/data`; only the two expected mounts exist (no `/config`,
  `/homeassistant`, `/share`, `/ssl`, `/media`, `/addons`,
  `/addon_configs`, `/backup`); `/data` modes are as in V2; the guard hook
  denies `cat /etc/passwd` as `ops`; the provider binary runs as `ops`;
  `ops.db` (and its pre-upgrade copy while it exists) is root 0600; uid 65534 cannot connect to the socket and `ops`
  outside a run gets `ok: false` for a mutating verb; every registry
  reference resolves and the parser's verbs equal the registry's. Tests
  `tests/test_selftest.py`.
