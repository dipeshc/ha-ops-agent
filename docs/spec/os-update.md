# The operating-system update and reboot

An operating-system update is the one case where the agent touches the host.
It always needs an approval, the reboot needs a second one, and both pass
through the single-use approved OS flow in [selfguard.md](selfguard.md).

Implementation: `opsd/updates.py` (`_await_os_install`, `_reboot`,
`_approved_reboots`, `reconcile`), `opsd/updexec.py` (`install` case `os`,
`reboot_for_os`), `opsd/approvals.py` (reboot approvals). Tests:
`tests/test_os_update.py`, `tests/test_selfguard.py` (OS flow),
`tests/test_approvals.py` `test_the_operating_system_can_be_approved_and_is_never_rolled_back`.

- O1. OS updates are always tier `approve`; `approve_now` is not offered.
  The executor requires a consumed, proven approval whatever the plan says
  and posts `POST /os/update {version}` only inside `approved_os_flow`.
- O2. After the install the daemon reads `/os/info` (version,
  `version_pending`, boot slot), `/host/info` (boot timestamp) and
  `/resolution/info` (`reboot_required`). A pending version moves the job to
  `rebooting` with 12 lines of Supervisor log evidence; the new version
  already active moves it to verifying; the old version with nothing pending
  is a failure. Test `test_os_phase_reads_the_supervisor_as_it_reports`.
- O3. **Reboot in the same run** happens only when: the gate still allows
  it, at least 15 minutes of the window remain (checked before and after
  waiting for quiet), the install state is pending and the boot timestamp is
  readable. Before the call the job records `reboot{requested_at,
  boot_before, slot_before}` so a daemon restart never reboots twice. The
  executor requires a proven approval, `version_pending == to` and
  `version == from`, then `POST /host/reboot {}` (never `force`) inside the
  flow. Otherwise the job is `reboot_pending` and the admin gets an item.
  Tests `test_install_success_reboot_required_is_installed_then_the_daemon_reboots_once`,
  `test_no_reboot_after_the_window`, `test_no_reboot_when_updates_were_paused_meanwhile`,
  `test_only_the_executor_scope_reaches_the_reboot`.
- O4. After the reboot call the daemon waits up to 15 minutes for a new boot
  timestamp or the active version. The run survives the daemon's own
  restart and the next start finishes it. Active: verify as a normal job.
  Back on the old version: `failed`, the version held, updates paused, an
  urgent push. Tests `test_the_run_survives_the_reboot_and_the_next_start_finishes_it`,
  `test_coming_back_on_the_old_version_is_a_real_failure_and_escalates`.
- O5. **Reboot pending.** The item is `waiting` with the single primary
  action `reboot_window` ("Reboot tonight at HH:MM"); it is never `failed`.
  The action creates a reboot approval bound to the job id, entity,
  versions, state and finish time; confirming it is the requester's act.
  After the next nightly run, `_approved_reboots` creates a one-job run with
  a fresh probe, consumes the approval and reboots as in O3. Test
  `test_an_approved_reboot_runs_last_in_a_run_of_its_own`.
- O6. **Reconcile** (at start and before every health check): an OS job
  `reboot_pending` within 14 days whose version is now active becomes
  succeeded; within 48 hours its health is compared with the run's probe and
  a regression is `rollback_failed`, held, paused and pushed. An OS update is
  never rolled back by the agent. Tests
  `test_a_host_that_does_not_reboot_leaves_it_installed_and_pending`,
  `test_a_real_failure_is_not_reconciled`.
- O7. The host-reboot health item and the `reboot_required` repair follow
  the OS update item and resolve with it ([items-and-pushes.md](items-and-pushes.md) I11).
