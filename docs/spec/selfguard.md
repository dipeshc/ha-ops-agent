# Selfguard

The agent never changes itself, the Supervisor or the host, and never
targets a lock, an alarm or a door, whatever any input says. Selfguard is
the code-level allowlist under every Supervisor, Core REST and websocket
call, every update job, reload, proposal path and dashboard save.

Implementation: `opsd/selfguard.py`; enforced in `opsd/supervisor.py`
(`SupervisorClient.request`), `opsd/ws.py` (`send_json`),
`opsd/updexec.py`, `opsd/changepaths.py`, `opsd/guard.py`,
`opsd/dashboards.py`. Tests: `tests/test_selfguard.py`,
`tests/test_dashboards.py`.

## HTTP

- G1. GET and HEAD always pass. A POST passes only when the approved OS
  flow takes it (G4) or its path matches one allowlisted pattern and its
  body is exactly what the agent sends; a matching path with a different
  body is refused. Anything else raises before any network I/O and reaches
  callers as `ApiError(403, "refused")`. Tests
  `test_http_self_targets_are_refused`, `test_the_client_refuses_before_any_network`.
- G2. Allowed POSTs: `/core/update` with `{"version": <non-empty>,
  "backup": false}`; `/store/addons/<slug>/update` with `{"backup": false}`
  and a slug that is not the agent; `/core/check`, `/core/restart`,
  `/core/api/config/core/check_config`,
  `/core/api/config/config_entries/entry/<id>/reload`,
  `/core/api/services/automation/reload`,
  `/core/api/services/template/reload`,
  `/core/api/services/homeassistant/reload_custom_templates` with an empty
  body; `/core/api/services/update/install` with keys within `entity_id`
  and `version`, an unprotected `update.*` entity and a plain version
  string; `/core/api/services/input_boolean/turn_on|turn_off` for the AI
  switch only; `/core/api/services/notify/mobile_app_<x>`;
  `/core/api/states/sensor.ops_agent[_x]`. Never: the agent's own add-on,
  the Supervisor, other add-ons' start/stop/install, backups and restores,
  `hassio.*` services, Core stop, host reboot or shutdown outside G4. Tests
  `test_ordinary_calls_still_pass`, `test_update_executor_calls_still_reach_the_supervisor`,
  `test_reload_and_check_config_still_reach_core`.
- G3. **Self identity.** The slug `local_ha_ops_agent`, `self`, and any
  `[a-z0-9]{1,32}_ha_ops_agent`; the entities `update.ha_ops_agent_update`
  and `update.home_assistant_supervisor_update`, including renamed or
  numbered copies of the agent, Supervisor and OS entities; the title "ha
  ops agent". `check_update` refuses the Supervisor kind and the agent for
  every update job; the OS only passes through its flow. Tests
  `test_update_jobs_never_target_the_agent_or_the_supervisor`,
  `test_a_renamed_agent_update_entity_is_refused`,
  `test_no_capability_argument_can_carry_a_self_slug_into_a_url`.
- G4. **Approved OS flow.** `approved_os_flow(path)` for `/os/update` or
  `/host/reboot` is a context-variable scope that is single-use, closes
  when the block ends, is inherited only by tasks and threads created
  inside it, and admits exactly the one call it names: `/os/update` with
  `{"version": <non-empty>}` or `/host/reboot` with an empty body (never
  `force`). It is entered only by the update executor's OS install and
  reboot after the job check and the approval proof. Tests
  `test_the_os_flow_lets_through_exactly_the_one_call_it_names`,
  `test_the_os_flow_is_single_use`, `test_the_os_flow_does_not_leak_into_other_tasks`,
  `test_the_os_flow_names_only_the_update_and_the_reboot`.

## Websocket

- G5. Every non-`auth` message passes `check_ws` before it is written.
  Allowed commands: `ping`, `subscribe_events` (only
  `mobile_app_notification_action`), `subscribe_trigger` (only the state
  trigger on the AI switch), `backup/info`,
  `backup/generate_with_automatic_settings`, `config_entries/get`,
  `config/entity_registry/list|get`, `config/area_registry/list`,
  `config/auth/list`, `repairs/list_issues`, `system_log/list`,
  `homeassistant/expose_entity/list`, `hacs/repository/info`,
  `update/release_notes`, `history/history_during_period`,
  `recorder/statistics_during_period`, `vacuum/get_segments`,
  `input_boolean/list`, `input_boolean/create` (only the AI switch's name),
  and the dashboard reads `lovelace/config` (only a `url_path`),
  `lovelace/dashboards/list` and `lovelace/resources` (no arguments). The
  one write is `lovelace/config/save` under G8; every other `lovelace/...`
  command is refused. Tests `test_ws_self_targets_are_refused`,
  `test_every_websocket_send_path_refuses_before_writing`,
  `test_the_agents_own_subscriptions_and_switch_pass`.

## Paths

- G6. `self_path` (normalised, case-insensitive) denies the prefixes
  `addons/`, `.git/`, `.git`, `/data`, `/ssl`, `/backup`, `/addons`,
  `data/`, `ssl/`, `backup/`, `ops/`, any `/.git/` segment, and the
  substrings `ha-ops-agent`, `ha_ops_agent`, `github_deploy`. It applies to
  every proposal path and to every readable entry of the profile, so the
  house profile and the agent's own files can never be proposed or read.
  Tests `test_proposals_can_never_name_the_agent`,
  `test_the_house_profile_is_never_a_proposal_path`,
  `test_a_profile_cannot_make_secrets_or_the_agent_readable`.
- G7. Reloads can never target a platform or vendor on the never-list
  ([reload.md](reload.md)); approval requests for the agent or the
  Supervisor are refused. Tests `test_reload_can_never_target_the_platform`,
  `test_update_approval_requests_for_the_agent_are_refused`.

## Dashboards

- G8. **Approved dashboard write.** `lovelace/config/save` passes only
  inside `approved_dashboard_write(url_path, generated=...)`, with exactly
  `{url_path, config}` (`config` a mapping) and that scope's url_path. The
  scope is a context variable like G4: single use, closed when the block
  ends, and admits one save. It is never opened for the default dashboard
  (no url_path), a url_path longer than 100 characters or not lower case
  with a hyphen (`[a-z0-9][a-z0-9_]*(?:-[a-z0-9_]+)+`), or one listed in the
  profile's `dashboards.generated`; refusal raises `SelfTargetError`. Only
  `dashboards.DashboardWriter` enters it, after consuming the admin's
  dashboard approval, or to restore the backup after a failed save of that
  plan ([maintenance.md](maintenance.md) M21–M23). Tests
  `test_the_save_is_refused_outside_an_approved_scope`,
  `test_the_scope_lets_exactly_one_save_of_its_own_dashboard_through`,
  `test_the_scope_is_never_opened_for_the_default_a_generated_or_a_malformed_dashboard`,
  `test_a_generated_dashboard_is_refused_even_with_a_forged_approval`.
