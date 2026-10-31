# The panel and its API

The panel is a static page served by the daemon behind Home Assistant
ingress, admin-only. The item record, Home, History and the act endpoint are
specified in [`docs/panel-api.md`](../panel-api.md), which this document
incorporates by reference; this document owns the transport and security
rules and the routes that file only names.

Implementation: `opsd/panel.py` (HTTP server, auth, CSRF, limits),
`opsd/panelapi.py` (routes), `opsd/live.py` (event hub), the static files
under `rootfs/opt/ops/panel/`. Tests: `tests/test_panel.py`,
`tests/test_panel_shell.py`, `tests/test_items.py`
`test_every_contract_route_is_served`.

## Transport and security

- N1. A hand-rolled HTTP/1.1 server on port 8099 (`ingress_port`), one
  request per connection, GET and POST only (else 405). A connection whose
  peer is not the ingress proxy (`172.30.32.2`) is closed unread, `/healthz`
  included. Test `test_requests_not_from_the_ingress_proxy_are_refused`.
- N2. **Admin check.** Every route except `/healthz` requires the ingress
  header `x-remote-user-id` to equal, in constant time, the one admin user
  id the daemon knows: the active administrator who owns the configured
  push device (resolved hourly until known, then daily). Unknown admin,
  empty or different header: 403 `admin_only`. Home Assistant's
  `panel_admin: true` limits the sidebar entry to administrators; the daemon
  narrows that to one user. Tests `test_every_get_needs_the_admin`,
  `test_admin_identity_prefers_the_active_admins_registration`.
- N3. **CSRF.** A per-process token is embedded in the shell's meta tag and
  returned as `csrf` by `GET api/state`. Every POST MUST carry
  `Content-Type: application/json` (else 415), the header `X-Ops-CSRF`
  equal to the token (else 403 `csrf`) and a JSON object body (else 400).
  When `Sec-Fetch-Site` is present, API routes accept only `same-origin`
  and shell routes `same-origin` or `none` (else 403 `cross_site`). No CORS
  headers are ever sent. Tests `test_writes_need_csrf_json_and_same_origin`,
  `test_the_token_is_only_in_the_shell_and_state`.
- N4. **Limits.** Body over 16 KiB: 413 before reading; chunked encoding:
  501; more than 64 headers or a line over 8192 bytes: 431 / 414; a
  duplicated `content-length`, admin, CSRF or content-type header: 400; 10
  seconds per line and per request; 32 connections of which the last 2 only
  serve `/healthz` (others 503 `busy`). Tests
  `test_oversized_bodies_get_413_before_they_are_read`,
  `test_slow_requests_hit_the_overall_deadline`,
  `test_health_keeps_a_slot_under_a_connection_flood`.
- N5. **Routing.** API path segments match `[A-Za-z0-9_-]{1,64}`, at most 4,
  else 404. The shell is served for `/`, `/t/<thread or item id>` and
  `/updates`; its `<base href>` derives from the route depth, never from the
  ingress path header. Static scripts are registered by name (`changes.js`
  only). Test `test_shell_base_path_is_validated_and_the_csp_is_strict`.
- N6. **Headers.** JSON and script responses: `default-src 'none';
  frame-ancestors 'none'`. The shell: a per-page nonce for its one script
  and one style, `connect-src 'self'`, `img-src 'self' data:`, `base-uri
  'self'`, `form-action 'none'`, `frame-ancestors 'self'`. All responses:
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy:
  same-origin`, `Connection: close`. The shell has no inline handlers, no
  external resources, no `innerHTML` or `eval` sinks; size budgets under 70
  KB raw and 20 KB gzip (changes.js under 12 KB gzip). Tests
  `test_shell_is_csp_compatible`, `test_shell_size_budget`.
- N7. `/healthz` (the watchdog target) is the only unauthenticated route and
  answers `{"ok": true}` and nothing else. Test
  `test_healthz_is_the_only_open_route_and_says_nothing`.
- N8. Model text never enters HTML: the shell is static with `{{BASE}}`,
  `{{CSRF}}` and `{{NONCE}}` substituted; all data travels as JSON and the
  panel renders strings as text nodes. Tests
  `test_model_output_and_titles_only_ever_leave_as_json`,
  `test_model_output_renders_as_text_only`.
- N9. A store failure answers 503 `store_unavailable`; an unknown route 404
  `not_found`.

## Routes

All under `api/`, admin and ingress checks on every one, CSRF on every POST.

| Route | Behaviour |
| --- | --- |
| `GET state` | `status` (off, paused, working, waiting, ready), `ai {on, state, backoff_until}`, `usage` (see [budget.md](budget.md)), `run`, `queue`, `chat {used, cap: 30, thread}`, `options` notice, `held {count, until, undelivered[]}`, `tz`, `version`, `store`, `quiet {from, to}`, `mode`, `updates {auto, window}`, `needs_count`, `csrf`. |
| `GET ops` | Open health items (redacted) merged with their item state, `needs_count`, the last 30 history lines, `status`, `profile {path, loaded, problems, git, overlays, push_target, notify_problem}`. |
| `GET usage` | 169 hourly points `{t, s5, s7}` over 7 days, `max_share_percent`, `share_stop_percent`, `today`. |
| `POST ai {on}` | `on: false` pauses the AI at once; `on: true` is a high-impact admin action (`panel.ai_on`), refused unless the user is the admin. |
| `POST run` | Requests a manual health cycle; `{started}`. |
| `POST selftest` | Runs the self-test ([privsep.md](privsep.md)); `{result}`. |
| `GET updates` | `{control, backup}`; 503 without the update manager. |
| `POST updates/auto {on}` | Off pauses at once; on answers 409 `option_off` when `updates.auto_install` is off, else resumes (clearing a failed-night block) as a high-impact action. |
| `GET suppressions`, `POST suppressions/<id>/revoke` | Empty without the `suppress` overlay. |
| `POST backups/now` | 409 `backup_running`, 429 `backup_rate` with the reason, else starts a standalone backup ([updates.md](updates.md) U24). |
| `GET home`, `GET items/<id>`, `GET history`, `POST items/<id>/act` | As in `docs/panel-api.md`. The act endpoint audits `item.act` with the admin's user id; the high-impact kinds (approve_update, approve_reboot, approve_change, updates_on, ai_on) also audit `panel.<kind>`. A `not_done` refusal is answered as 409 `not_available`. |
| `GET diagnostics` | The redacted export ([diagnostics.md](diagnostics.md)). |
| `GET chat`, `GET threads/<t>`, `POST threads/<t>/messages|retry|autoretry|read|cancel|remember` | See [chat.md](chat.md). Only the main thread is reachable. |
| `POST presence {thread, visible}` | Admin-only, token bucket of 4 with 1 per second refill (429 `slow_down`); a 20-second watching lease. |
| `GET events`, `GET poll?after=` | Live events, below. |

Not served (404): `api/tasks`, `api/changes`, `api/facts`, `api/activity`,
`api/ack`, `api/approvals*`, `api/updates/plan|holds/clear|skip|notes`,
`api/confirm/<cid>`, `api/items/<id>/withdraw`.

## Live events

- N10. `GET events` is server-sent events: `retry: 3000`, `hello {seq}` on a
  fresh start, replay from `Last-Event-ID` or `after`, `reset {seq}` on a
  gap, `ping {}` every 15 seconds, streams end after 30 minutes, and the
  admin is re-checked at every ping and at least every 60 seconds. `GET
  poll` returns the same events, holding up to 25 seconds. At most 4 live
  connections (SSE and waiting polls together), else 429
  `too_many_live_connections`. Event kinds: `state`, `status`, `turn`,
  `thread`, `item`, `tasks`, `updates`, `changes`, `facts`, `activity`.
  Tests `test_sse_streams_a_turn_from_queued_to_reply`,
  `test_live_connections_are_capped_and_freed`, `test_sse_resume_and_gap`,
  `test_sse_rechecks_the_admin_even_while_events_flow`.
- N11. The page shows the freshness pill amber after 40 seconds without a
  ping and red after 120, and falls back to long-polling after 5 seconds
  without `hello` or 40 without a ping.
