# The panel and its API

The panel is a static page served by the daemon behind Home Assistant
ingress, admin-only. This document owns the transport and security rules,
the item record (the contract between the panel and the daemon), and every
route.

Implementation: `opsd/panel.py` (HTTP server, auth, CSRF, limits),
`opsd/panelapi.py` (routes), `opsd/items.py` (`ItemBook.view`),
`opsd/live.py` (event hub), the static files under `rootfs/opt/ops/panel/`.
Tests: `tests/test_panel.py`, `tests/test_panel_shell.py`,
`tests/test_items.py` `test_every_contract_route_is_served`.

## Transport and security

- N1. A hand-rolled HTTP/1.1 server on port 8099 (`ingress_port`), one
  request per connection, GET and POST only (else 405). A connection whose
  peer is not the ingress proxy (`172.30.32.2`) is closed unread, `/healthz`
  included. Test `test_requests_not_from_the_ingress_proxy_are_refused`.
- N2. **Admin check.** Every route except `/healthz` MUST require the
  ingress header `x-remote-user-id` to equal, in constant time, the one
  admin user id the daemon knows: the active administrator who owns the
  configured push device (resolved hourly until known, then daily). Unknown
  admin, empty or different header: 403 `admin_only`. Home Assistant's
  `panel_admin: true` limits the sidebar entry to administrators; the daemon
  narrows that to the one user. Other administrators are refused. Tests
  `test_every_get_needs_the_admin`,
  `test_admin_identity_prefers_the_active_admins_registration`.
- N3. **CSRF.** A per-process token is embedded in the shell's meta tag and
  returned as `csrf` by `GET api/state`. Every POST MUST carry
  `Content-Type: application/json` (else 415 `json_required`), the header
  `X-Ops-CSRF` equal to the token (else 403 `csrf`) and a JSON object body
  (else 400 `bad_json`). When `Sec-Fetch-Site` is present, API routes
  accept only `same-origin` and shell routes `same-origin` or `none` (else
  403 `cross_site`). No CORS headers are ever sent. Tests
  `test_writes_need_csrf_json_and_same_origin`,
  `test_the_token_is_only_in_the_shell_and_state`.
- N4. **Limits.** Body over 16 KiB: 413 before reading; chunked encoding:
  501; more than 64 headers or a line over 8192 bytes: 431 / 414; a
  duplicated `content-length`, admin, CSRF or content-type header: 400; 10
  seconds per line and per request; 32 connections of which the last 2 only
  serve `/healthz` (others 503 `busy`); at most 8 query fields. Tests
  `test_oversized_bodies_get_413_before_they_are_read`,
  `test_slow_requests_hit_the_overall_deadline`,
  `test_health_keeps_a_slot_under_a_connection_flood`.
- N5. **Routing.** API paths are `api/...` relative to the panel; segments
  match `[A-Za-z0-9_-]{1,64}`, at most 4, else 404 `not_found`. An item's
  URL `id` is therefore an opaque handle, never the thread key. The shell
  is served for `/`, `/t/<thread or item id>` and `/updates`; its
  `<base href>` derives from the route depth, never from the ingress path
  header. Static scripts are registered by name (`changes.js` only). Test
  `test_shell_base_path_is_validated_and_the_csp_is_strict`.
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
- N8. **Text, never HTML.** The shell is static with `{{BASE}}`, `{{CSRF}}`
  and `{{NONCE}}` substituted; all data travels as JSON and the panel
  renders every string as a text node. Every string is daemon text except
  `agent.text`, the one model-written field. Tests
  `test_model_output_and_titles_only_ever_leave_as_json`,
  `test_model_output_renders_as_text_only`, `test_item_detail_renders_every_field_as_text`.
- N9. A store failure answers 503 `store_unavailable`; an unknown route 404
  `not_found`; any `{"error": ...}` body is shown by the panel as an error.
  Times are ISO 8601 UTC; the panel formats them in the house time zone
  (`api/state` `tz`).

## The item

The record behind Home, History and the item page
([items-and-pushes.md](items-and-pushes.md) owns the derivation rules).

```jsonc
{
  "id": "u-7f3a91c2",                 // URL-safe handle, [a-z]-[0-9a-z]{1,40}
  "key": "updates:update.home_assistant_operating_system@17.1",
  "kind": "update",                   // update | reminder | investigation | proposal | check | backup | reload | window
  "kicker": "Operating system update",// optional subtype label; the panel falls back to a label for `kind`
  "ref": "#18",                       // optional short reference shown beside the kicker
  "title": "Home Assistant OS 17.0 → 17.1",
  "state": "waiting",                 // proposed | waiting | scheduled | running | done | failed | cancelled | expired
  "gist": "installed, the host needs a reboot to finish",
  "at": "2026-01-15T04:12:00Z",       // the time a row shows: needs_since, the due/run time, or resolved_at
  "quiet": false,                     // routine entry: History folds a day's quiet items into one line
  "revision": 7,                      // bumped on every material change; actions must send it
  "as_of": "2026-01-15T09:41:00Z",    // last daemon observation behind state and facts
  "needs_since": "2026-01-15T04:12:00Z", "resolved_at": null,
  "created_at": "2026-01-11T04:00:00Z", "updated_at": "2026-01-15T04:12:00Z",

  "need": {                           // non-null only while the item is in Needs you
    "kind": "approval",               // approval | confirm | physical | answer | ack (0.17.0 produces approval and ack)
    "text": "17.1 is installed and waiting for one reboot. …",  // what happens, when, cost, what cannot be undone
    "primary": { "id": "reboot_window", "label": "Reboot tonight at 04:00" },
    "secondary": [ { "id": "reject", "label": "Reject", "style": "text" } ],
    "expires_at": null
  },
  "about": "Runs tonight at 04:00 through the normal pipeline.",  // optional action-block text when need is null
  "actions": [ { "id": "withdraw", "label": "Withdraw approval", "style": "text" } ],  // available outside a need
  "phase": { "names": ["Backup", "Install", "Reboot", "Verify"], "current": 2,
             "started_at": "2026-01-15T03:59:00Z", "usual_s": 480 },  // null unless phased; current = names.length when all done
  "follows": { "id": "u-7f3a91c2", "title": "Home Assistant OS 17.0 → 17.1", "state": "waiting" },  // or null
  "facts": [ { "label": "Supervisor", "value": "OS 17.0 running, 17.1 pending reboot", "at": "2026-01-15T09:41:00Z" } ],
  "agent": { "text": "Nothing went wrong: …", "sources": ["Supervisor OS info", "host log 04:00–04:15"],
             "not_read": [], "run_id": "r-…" },  // or null; the only model-written field
  "change": { "diff": "@@ … @@\n+ …", "files": ["docs/facts.md"], "shown": ["<sha256>"] },  // proposals only
  "footnote": "Approval 6f3a · bound to OS 17.0 → 17.1, plan #18 · used once",
  "events": [                         // detail responses only, oldest first
    { "at": "2026-01-11T04:00:00Z", "actor": "daemon", "text": "Proposed: 17.1 needs your approval", "revision": 1 }
  ]
}
```

- N10. An action is `{id, label, style?}`; `id` matches `[a-z_]{1,32}` and
  is meaningful only on this item; `style` is `secondary` (default), `text`
  or `danger`. `need.primary` is always the one filled button. `actor` is
  `you`, `daemon` or `agent`. The panel shows only the state vocabulary
  above; any other value is a bug.
- N11. **Needs you** is exactly `state == waiting`, or `failed` with
  `need.kind == "ack"`; `need` is non-null exactly then. The daemon
  decides; the panel never counts or infers it. "Installed, reboot pending"
  is `waiting` with a need and never `failed`; its only action is
  `reboot_window`.

| state | Home section | panel word |
| --- | --- | --- |
| `waiting`, or `failed` with a need | Needs you | Needs you / Failed |
| `running` | In progress | Running |
| `proposed`, `scheduled` | Coming up | Proposed / Scheduled |
| `done`, `failed`, `cancelled`, `expired` resolved today (house time) | Today | Done / Failed / Cancelled / Expired |

## Routes

All under `api/`, admin and ingress checks on every one, CSRF on every POST.

| Route | Behaviour |
| --- | --- |
| `GET state` | `status` (off, paused, working, waiting, ready), `ai {on, state, backoff_until}`, `usage` (see [budget.md](budget.md)), `run {profile, thread}` or null, `queue`, `chat {used, cap: 30, thread}`, `options` (a notice or null), `held {count, until, undelivered[]}` or null, `tz`, `version`, `store`, `quiet {from, to}` (a side is null when unset), `mode`, `updates {auto, window}`, `needs_count`, `csrf`. |
| `GET ops` | `items` (open health items, redacted, each merged with `{item, item_state, needs}`), `needs_count`, `history` (the last 30 cycle records, newest first), `status`, `profile {path, loaded, problems, git, overlays, push_target, notify_problem}`. |
| `GET usage` | 169 hourly points `{t, s5, s7}` over 7 days, `max_share_percent`, `share_stop_percent`, `today`. |
| `POST ai {on}` | `on: false` pauses the AI at once; `on: true` is a high-impact admin action (`panel.ai_on`), refused unless the user is the admin; 400 `bad_ai`, 503 without the usage guard. Returns `{ok, state}`. |
| `POST run` | Requests a manual health cycle; `{started}`. |
| `POST selftest` | Runs the self-test ([privsep.md](privsep.md)); `{result}`. |
| `GET updates` | `{control, backup}`; 503 without the update manager. |
| `POST updates/auto {on}` | Off pauses at once; on answers 409 `option_off` when `updates.auto_install` is off, is a no-op when not paused, else resumes (clearing a failed-night block) as a high-impact action. 400 `bad_updates`. |
| `GET suppressions`, `POST suppressions/<id>/revoke` | `{rules: [], problems: [], max: 0}` without the `suppress` overlay; revoke is 404 without it. |
| `POST backups/now` | 503 without backups, 409 `backup_running`, 429 `backup_rate` with the reason, else starts a standalone backup ([updates.md](updates.md) U24). |
| `GET diagnostics` | The redacted export ([diagnostics.md](diagnostics.md)). |
| `GET chat`, `GET threads/<t>?before=`, `POST threads/<t>/messages|retry|autoretry|read|cancel|remember`, `POST presence` | See [chat.md](chat.md). Only the main thread is reachable; `cancel` cancels an investigation, not a message. Presence is rate-limited (a token bucket of 4, 1 per second; 429 `slow_down`). |
| `GET events`, `GET poll?after=` | Live events (N17). |

### `GET api/home`

```jsonc
{
  "as_of": "…",
  "needs_count": 2,                   // == sections.needs.length; also the nav badge
  "sections": { "needs": [Item], "running": [Item], "coming": [Item], "today": [Item] },
  "calm": "Checked at 05:30, nothing new since the baseline. …",  // the All-quiet text; null when needs is not empty
  "strip": [ { "label": "Health", "value": "checked 05:30, nothing new" },
             { "label": "Backup", "value": "03:59, encrypted" },
             { "label": "Next window", "value": "tonight 04:00" },
             { "label": "AI", "value": "on, 1.4 % of 7-day budget" } ]
}
```

- N12. Items in the sections omit `events`. Needs you is ordered oldest
  `needs_since` first, the other sections by `at`.

### `GET api/items/<id>`

- N13. `200 {"item": Item}` with `events`; unknown id: `404 {"error":
  "no_such_item"}`.

### `GET api/history?filter=<f>&before=<cursor>`

- N14. `f` is `all` (default), `needs`, `changes` (proposals, updates,
  reloads) or `checks` (checks, backups); 400 `bad_filter` / `bad_cursor`.
  Newest first, 50 per page, keyset cursor on `(at, id)`:
  `{ "items": [Item], "counts": { "needs": 2 }, "more": true, "cursor": "opaque" }`.
  `counts.needs` equals `needs_count`. The panel groups the items by day in
  the house time zone and folds each day's `quiet` items into one line.
  Test `test_history_pages_newest_first`.

### `POST api/items/<id>/act`

- N15. Body `{"action": "<action id>", "revision": 7}`. `action` MUST be the
  id of `need.primary`, one of `need.secondary`, or one of `actions`. The
  daemon re-checks everything; the panel's copy is never trusted.

| status | body | meaning |
| --- | --- | --- |
| 200 | `{"item": Item}` | done or recorded |
| 400 | `{"error": "bad_action" \| "bad_revision"}` | malformed |
| 404 | `{"error": "no_such_item"}` | unknown id |
| 409 | `{"error": "revision", "message": "The plan changed: …", "item": Item}` | stale revision; the panel shows the message and the new item |
| 409 | `{"error": "not_available", "message": …}` | the action is not offered any more, or the daemon refused it because it changed or expired meanwhile |

- N16. Every action is one admin request (ingress admin header and CSRF
  token), bound to the item's revision and, for an approval, to the plan
  item's hash ([approvals.md](approvals.md)). Every action is audited as
  `item.act` with the item id, action, revision and the admin's user id;
  the high-impact kinds (approve_update, approve_reboot, approve_change,
  updates_on, ai_on) also audit `panel.<kind>` with their outcome. Actions
  in 0.17.0: `ack`, `resume_updates`, `withdraw`, update `approve`,
  `approve_now`, `reject`, `reboot_window`, change `apply`, `reject`,
  `revert`, fact `dismiss`, health `accept`, `snooze`, task `cancel`,
  `stop`. Test `test_approve_then_withdraw_binds_each_action_to_its_revision`.

Not served (404): `api/tasks`, `api/changes`, `api/facts`, `api/activity`,
`api/ack`, `api/approvals*`, `api/updates/plan|holds/clear|skip|notes`,
`api/confirm/<cid>`, `api/items/<id>/withdraw`.

## Live events

- N17. `GET events` is server-sent events: `retry: 3000`, `hello {seq}` on a
  fresh start, replay from `Last-Event-ID` or `after`, `reset {seq}` on a
  gap, `ping {}` every 15 seconds, streams end after 30 minutes, and the
  admin is re-checked at every ping and at least every 60 seconds. `GET
  poll` returns the same events, holding up to 25 seconds, and answers
  `{seq, events: []}` without `after` and `{seq, reset: true}` on a gap. At
  most 4 live connections (SSE and waiting polls together), else 429
  `too_many_live_connections`. Event kinds: `state`, `status`, `turn`,
  `thread`, `item {id, revision, state, needs_count}` whenever an item
  changes (the panel refetches the open views and ignores revisions older
  than the one it holds), `tasks`, `updates`, `changes`, `facts`,
  `activity`. Tests `test_sse_streams_a_turn_from_queued_to_reply`,
  `test_live_connections_are_capped_and_freed`, `test_sse_resume_and_gap`,
  `test_sse_rechecks_the_admin_even_while_events_flow`, `test_long_poll_returns_the_same_events`.
- N18. The page shows the freshness pill green while pings arrive, amber
  after 40 seconds without one and red after 120, and falls back to
  long-polling after 5 seconds without `hello` or 40 without a ping. A
  turn's `meta.item` may name an item id, and the panel embeds that item's
  card in the chat.
- N19. There is no digest time: the panel never pushes; pushes are decided
  by the item book and the daemon ([items-and-pushes.md](items-and-pushes.md)).
