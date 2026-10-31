# Approvals

An approval is the admin's explicit, bounded consent to one action the agent
showed them. It is bound to the exact plan, used once and expires.

Implementation: `opsd/approvals.py`, the act endpoint in `opsd/panelapi.py`,
consumers in `opsd/updexec.py`, `opsd/changes.py` and `opsd/dashboards.py`.
Tests: `tests/test_approvals.py`, `tests/test_dashboards.py`, `tests/test_items.py`
`test_approve_then_withdraw_binds_each_action_to_its_revision`.

## Who and where

- A1. Only the admin can request, approve, reject or withdraw, and only
  through the panel: the ingress user MUST be the one admin user id the
  daemon knows (see [panel.md](panel.md) N2) and every write carries the CSRF
  token. No `api/approvals*` route exists (404).
- A2. The AI has no capability that creates, confirms or consumes an
  approval. Chat may ask the admin to approve an update; the request and the
  approval are both admin acts in the panel.
- A3. There is no separate phone confirmation: request and approval happen
  in the same panel action. A push about a pending approval only opens the
  panel; it carries no action buttons. Test
  `test_approvals_ask_once_with_a_link_to_the_panel`.
- A4. Confirm MUST refuse unless the confirming user equals `requested_by`
  (constant-time comparison). Test `test_only_the_admin_who_asked_can_confirm`.

## Records

- A5. An approval is an `approvals` record of kind `approval`
  ([storage.md](storage.md) S3); every approval read and decision names that
  kind, so a request, a change proposal or a house fact in the same table is
  never read as one. States: `pending`, `approved`, `rejected`, `expired`,
  `used`, `void`. Live states are `pending` and `approved`. At most 20 approvals may be
  pending; a new request for the same subject voids any earlier live one
  (`superseded`).
- A6. Ids are 32 hex characters (`secrets.token_hex(16)`). Every approval
  expires 72 hours after creation; `expire()` moves live approvals past their
  expiry to `expired`.
- A7. Every decision writes an audit row (`channel=approvals`,
  `capability=approval.<requested|approved|rejected|void|...>`, tier
  `approve`) and the panel writes `item.act` with the admin's user id.

## Update approvals

- A8. Only an approvable plan item can be requested: tier APPROVE, kind in
  `core`, `addon`, `hacs`, `os`, `firmware` (never the Supervisor or the
  agent itself), both versions known. `run_when` is `window` or `now`
  (`approve` / `approve_now`; `approve_now` is not offered for OS updates).
- A9. **Binding.** `plan_hash` is the SHA-256 of the plan item's entity id,
  kind, title, installed and latest versions, tier, bump, reason, review and
  held flag, plus the plan run id and the creation time. `bound()` recomputes
  it from the plan item stored in the update run row (not from the
  approval's own copy), requires the same installed/latest versions and
  compares in constant time. Tests `test_the_hash_binds_entity_versions_and_plan`,
  `test_the_approval_is_checked_against_its_plan_row`.
- A10. Confirm MUST: refuse change and reboot kinds; require state `pending`;
  mark `expired` if past expiry; mark `void` when the live update entity is
  missing, not bound, not `on`, or its versions changed (the daemon fetches
  the live entity before confirming). Only then `pending -> approved`. Test
  `test_a_version_change_voids_the_approval`.
- A11. **Single use.** The executor consumes with a compare-and-set
  `approved -> used` recording the job id; `proves()` requires state `used`,
  the same job, a `decided_by`, a still-valid binding and the same entity and
  versions. Tests `test_replay_is_refused_and_an_approval_is_used_once`,
  `test_executors_need_a_consumed_approval_for_the_approve_tier`.
- A12. The nightly plan annotates items with `approved` only for approved,
  still-approvable items (and, for a now-run, only `run_when == now`), and is
  the only pass that voids mismatched approvals (`approval.void` by
  `daemon`). Test `test_now_only_runs_approved_items_not_the_auto_ones`.
- A13. Reject creates and immediately rejects a request; a rejection within
  the last 7 days shows the item as `cancelled` ("rejected by you").
- A14. Withdraw voids the live approval named by the item (`withdrawn by
  you`); withdrawing a non-live approval answers 409 `not_pending`.

## Change (proposal) approvals

- A15. `request_change(binding, apply|revert)` uses subject
  `change:<id>:<action>`, always `run_when = now`, hash
  `change_hash(binding, created_at)`, the same pending cap, superseding and
  72-hour expiry. Confirm re-binds against git and consume re-checks state,
  decider, proposal id, expiry and binding right before writing. Update
  paths never accept change or dashboard approvals and vice versa.

## Reboot approvals

- A16. An OS update that is installed and waiting for a reboot needs its own
  approval, subject `reboot:<entity>@<version>`, bound to the job id, entity,
  versions, state and finish time; it is consumed just before the reboot step
  and the executor still needs the job's own update approval. See
  [os-update.md](os-update.md).

## Dashboard approvals

- A17. A dashboard migration plan ([maintenance.md](maintenance.md) M21)
  uses subject `dashboard:<finding>`, `plan.kind: dashboard`, always
  `run_when = now`, the same pending cap, superseding and 72-hour expiry.
  `plan_hash` is `dashboard_hash(binding, created_at)`: the SHA-256 of the
  canonical JSON of the finding, url_path, the dashboard JSON's SHA-256
  before and after the edit, the edit, and the changes' count and paths.
  The admin's one panel request (`apply_dashboard`) requests and confirms
  it; confirm requires state `pending` and the confirming user equal to
  `requested_by`, marks `expired` past expiry and `void` when the binding
  recomputed from the dashboard as Home Assistant serves it no longer
  matches. `consume_dashboard` re-checks state `approved`, a decider,
  expiry and the recomputed binding right before the save and marks it
  `used` (compare-and-set). Update, change and reboot paths never accept
  it. Test `test_confirmation_needs_the_admin_who_asked_and_an_unchanged_dashboard`.
