# Integration reloads

The one change to Home Assistant the AI may request outside an approval is
a reload of an allowlisted integration. It is bounded in code: never
anything that opens the house, once per domain per 10 minutes, a few a day.

Implementation: `opsd/reload.py`, `opsd/access.py` (the access-word
classification it shares with the health check and dashboard edits),
`opsd/policy.py` `NEVER_RELOAD_DOMAINS`,
the `integration.reload` row in `opsd/capabilities.py`. Tests:
`tests/test_reload.py`, `tests/test_profile.py` (never-list),
`tests/test_selfguard.py` `test_garage_and_access_are_never_reloaded`,
`test_reload_can_never_target_the_platform`.

- L1. **Allowlist.** The `capabilities.reloadable_domains` option when
  non-empty, else the profile's `reload.domains`, else nothing. A domain not
  on the allowlist is refused. Tests `test_default_allowlist`,
  `test_not_allowlisted_is_refused`.
- L2. **Never-list** (`NEVER_RELOAD_DOMAINS`), applied whatever the options
  or profile say, plus the profile's `reload.deny` and
  `integration.reload.<domain>` in `capabilities.never`:
  - platforms: `alarm_control_panel`, `cover`, `lock`, `siren`, `valve`;
  - lock, alarm and access vendors: `abode`, `alarmdecoder`, `alarmdotcom`,
    `august`, `elkm1`, `envisalink`, `envisalink_new`, `esphome`,
    `homekit_controller`, `konnected`, `matter`, `nest`, `nuki`, `nx584`,
    `overkiz`, `ring`, `risco`, `schlage`, `simplisafe`, `switchbot`,
    `tedee`, `tesla_fleet`, `totalconnect`, `unifiprotect`, `verisure`,
    `yale`, `yale_home`, `yale_smart_alarm`, `yalexs_ble`, `zha`, `zwave_js`;
  - garage doors and bridges: `aladdin_connect`, `gogogate2`,
    `lutron_caseta`, `myq`, `ratgdo`;
  - radios and discovery: `bluetooth`, `bluetooth_adapters`, `dhcp`, `mqtt`,
    `network`, `otbr`, `ssdp`, `thread`, `unifi`, `usb`, `zeroconf`;
  - core plumbing: `alexa`, `api`, `auth`, `backup`, `cloud`, `frontend`,
    `google_assistant`, `hassio`, `homeassistant`, `homekit`, `http`,
    `mobile_app`, `onboarding`, `person`, `recorder`, `websocket_api`, `zone`.
  A profile that allowlists any of these is refused as a whole. Tests
  `test_denylist_wins_even_when_allowlisted`,
  `test_lock_alarm_and_garage_vendors_are_never_reloaded`, `test_admin_can_block_a_domain`.
- L3. **Live checks** before any reload: the domain's enabled config
  entries come from `config_entries/get` (entry ids validated, never taken
  from the caller); its entities are the registry rows of that platform or
  those entries. The reload is refused if any entity is a `lock.*` or
  `alarm_control_panel.*`, a `cover` whose device class is not blind,
  shade, curtain, awning or shutter, or a switch, button, valve or cover
  whose id or names match the access-word list (after discounting child
  locks, key locks and control panels); it is refused while any of the
  domain's vacuums is cleaning, returning or paused; an odd reply fails
  closed. Tests `test_a_domain_providing_a_lock_or_garage_cover_is_refused`,
  `test_a_cover_without_a_safe_class_is_protected`,
  `test_switches_named_like_access_are_protected`,
  `test_a_room_named_garage_is_not_an_access_control`, `test_odd_replies_fail_closed`,
  `test_never_while_a_vacuum_is_busy`.
- L4. **Rate.** Once per domain per 10 minutes, persisted in the store
  (`kv` `reload.last`, [storage.md](storage.md) S3; no store, corrupt or
  naive timestamps refuse, future ones are ignored, entries older than 2
  days pruned), marked at the first entry actually posted; concurrent
  requests reload once. Quota 2 per run and 6
  per day, taken only after every check passed and refunded on a refusal.
  Tests `test_once_per_domain_per_10_minutes`, `test_concurrent_requests_reload_once`,
  `test_socket_daily_quota`, `test_socket_refusals_do_not_burn_the_daily_quota`,
  `test_reserve_runs_only_after_every_check`.
- L5. **Execution.** `POST /core/api/config/config_entries/entry/<id>/reload`
  per entry (30 seconds each, 100 seconds overall), then up to 60 seconds
  waiting for the entities that were available before; entries not `loaded`
  afterwards are reported. An entity lost by the reload fails it; always
  unavailable entities neither fail nor hold it; a failed reload still
  counts against the rate. The result (`domain, ok, entries, attempted,
  skipped, recovered, still_unavailable, newly_unavailable, problems,
  message`) goes to the run's notes and the audit row. Tests
  `test_reload_waits_for_entities_and_reports`,
  `test_an_entity_lost_by_the_reload_fails_it`, `test_a_hung_entry_times_out`,
  `test_deadline_reports_a_partial_reload`.
- L6. **Mode and origin.** In `observe` mode the request is report-only and
  nothing is posted. Only the `triage`, `chat` and `investigate` profiles
  carry the capability; the chat origin may reload because RELOAD is in the
  chat effect set. Tests `test_act_mode_executes_observe_mode_only_reports`,
  `test_socket_observe_mode_reports_only`, `test_profiles`,
  `test_socket_chat_run_may_reload`.
