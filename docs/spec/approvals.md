# Approvals

An approval is the admin's explicit, bounded consent to one action the agent
showed them. It is bound to the exact plan, used once and expires.

Implementation: `opsd/approvals.py`, the act endpoint in `opsd/panelapi.py`,
consumers in `opsd/updexec.py`, `opsd/updates.py` and `opsd/changes.py`.
Tests: `tests/test_approvals.py`, `tests/test_items.py`
`test_approve_then_withdraw_binds_each_action_to_its_revision`.

## Who and where

- A1. Only the admin can approve, reject or withdraw, and only
  through the panel: the ingress user MUST be the one admin user id the
  daemon knows (see [panel.md](panel.md) N2) and every write carries the CSRF
  token. No `api/approvals*` route exists (404).
- A2. The AI has no capability that creates, confirms or consumes an
  approval, or that asks for one. Chat may read the update plan and
  recommend; the approval is the admin's act on the item in the panel.
- A3. There is no separate phone confirmation: the admin's one panel action
  grants the approval. A push about an update that needs approval only opens
  the panel; it carries no action buttons. Test
  `test_approvals_ask_once_with_a_link_to_the_panel`.

## Records

- A4. An approval is an `approvals` record of kind `approval`
  ([storage.md](storage.md) S3); every approval read and decision names that
  kind, so a change proposal or a house fact in the same table is never
  read as one. States: `approved`, `rejected`, `expired`, `used`, `void`
  (a store CHECK; the upgrade to schema 20 voids any `pending` one,
  [storage.md](storage.md) S11). The live state is `approved`. A new
  approval for the same subject voids any earlier live one (`superseded`).
- A5. Ids are 32 hex characters (`secrets.token_hex(16)`). Every approval
  expires 72 hours after creation; `expire()` moves live approvals past their
  expiry to `expired`.
- A6. Every decision writes an audit row (`channel=approvals`,
  `capability=approval.<approved|rejected|refused|void|...>`, tier
  `approve`) and the panel writes `item.act` with the admin's user id.
  Test `test_one_approval_binding_replay_and_expiry`.

## Update approvals

- A7. Only an approvable plan item can be approved: tier APPROVE, kind in
  `core`, `addon`, `hacs`, `os`, `firmware` (never the Supervisor or the
  agent itself), both versions known. `run_when` is `window` or `now`
  (`approve` / `approve_now`; `approve_now` is not offered for OS updates).
  A record is an update approval exactly when its `plan.kind` is one of
  those kinds.
- A8. **Binding.** `plan_hash` is the SHA-256 of the plan item's entity id,
  kind, title, installed and latest versions, tier, bump, reason, review and
  held flag, plus the plan run id and the creation time. `bound_update()` recomputes
  it from the plan item stored in the update run row (not from the
  approval's own copy), requires the same installed/latest versions and
  compares in constant time. Tests `test_the_hash_binds_entity_versions_and_plan`,
  `test_the_approval_is_checked_against_its_plan_row`.
- A9. Granting MUST refuse (audited `approval.refused`, the reason shown
  to the admin, nothing recorded as approved) when the live update entity
  is missing, not `on`, or no longer offers exactly the shown versions (the
  daemon fetches the live entity first). Test
  `test_a_version_change_voids_the_approval`.
- A10. **Single use.** The job's creation consumes it with a compare-and-set
  `approved -> used` recording the job id, and only when the approval is
  unexpired (else `expired`), still bound, and the job is for the same
  entity and versions (else `void`); `proves_update()` requires state `used`,
  the same job, a `decided_by`, a still-valid binding and the same entity and
  versions. Tests `test_replay_is_refused_and_an_approval_is_used_once`,
  `test_executors_need_a_consumed_approval_for_the_approve_tier`,
  `test_using_an_approval_checks_its_expiry_its_plan_and_the_job`.
- A11. The nightly plan annotates items with `approved` only for approved,
  still-approvable items (and, for a now-run, only `run_when == now`), and is
  the only pass that voids mismatched approvals (`approval.void` by
  `daemon`). An approval matches tonight's item only with the same installed
  and latest versions, a valid binding, and the same kind, title, add-on slug
  and HACS repository as the plan item it was given for. Tests
  `test_now_only_runs_approved_items_not_the_auto_ones`,
  `test_an_approval_is_void_when_what_it_installs_changed`.
- A12. Reject records a `rejected` approval; a rejection within
  the last 7 days shows the item as `cancelled` ("rejected by you").
- A13. Withdraw voids the live approval named by the item (`withdrawn by
  you`); withdrawing a non-live approval answers 409 `not_pending`.

## Change (proposal) approvals

- A14. A change approval (`apply` or `revert`) uses subject
  `change:<id>:<action>`, always `run_when = now`, the binding hash of A16,
  superseding and the 72-hour expiry. The binding is computed from git when
  the admin approves; consume re-checks state, decider, expiry and the
  binding recomputed right before writing. Update paths never accept a
  change approval and vice versa. Tests
  `test_one_approval_binding_replay_and_expiry`,
  `test_tampering_voids_the_approval`,
  `test_a_binding_change_after_the_tap_stops_the_apply`.

## Reboot approvals

- A15. An OS update that is installed and waiting for a reboot needs its own
  approval, subject `reboot:<entity>@<version>`, bound to the job id, entity,
  versions, state and finish time; it is consumed just before the reboot step
  and the executor still needs the job's own update approval. See
  [os-update.md](os-update.md).

## Bound approvals

- A16. Change, reboot and fork rebase approvals share one binding hash: the
  SHA-256 of the canonical JSON of `{<kind>: binding, created_at}`, compared
  in constant time against the binding the daemon recomputes right before
  use. Consume is a compare-and-set `approved -> used`, refused past expiry
  (`expired`) or with a binding that moved (`void`); a record of another
  kind is never accepted.

## Fork rebase approvals

- A17. A fork rebase proposal ([proposals.md](proposals.md) C11–C14) uses
  subject `fork_rebase:<proposal>`, `plan.kind: fork_rebase`, `run_when`
  `window` ("Rebase tonight") or `now` ("Rebase now"), superseding and the
  72-hour expiry. Its binding (A16) is the proposal id, the domain, the Core
  version, the origin tag, the kept hunk ids, the hash of the stored
  result's bytes and the hash of the fork's files as they are. The admin's
  one panel action grants it only when the shown `change` hash is the
  proposal's result, the agent acts, and the binding recomputed from disk
  and the live Core version equals the proposal's. It is consumed when the
  update job is created (`used` by that job); `proves` is the executor's
  check that the job consumed it and it still binds. Update, change and
  reboot paths never accept it, and the reverse. Test
  `test_the_approval_is_bound_to_the_fork_core_and_result`.
