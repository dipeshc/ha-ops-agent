# Panel API: items, Home, History

The contract between the panel (`rootfs/opt/ops/panel/`) and the daemon
(`opsd/panelapi.py`) for the item record. The chat and System endpoints are
listed at the end.

An unknown route answers `404 {"error": "not_found"}`; any error, such as
`no_such_item`, is shown as an error.

## Conventions

- Paths are relative to the panel (`api/...`), behind the existing admin
  check. POSTs carry `X-Ops-CSRF`, as today.
- Path segments must match `[A-Za-z0-9_-]{1,64}` (`panel.py` `_PART_RE`),
  so an item's URL `id` is an opaque handle, not the thread key. The key
  (`updates:update.home_assistant_operating_system@17.1`) is the separate
  `key` field.
- Times are ISO 8601 UTC. The panel formats them in the house time zone
  (`api/state` `tz`).
- Every string is daemon text. The panel renders it as text nodes, never as
  HTML. The model's words appear only in `agent.text`.
- The panel shows only the state vocabulary below; any other `state` value
  is a bug.

## The item

```jsonc
{
  "id": "u-7f3a91c2",                 // URL-safe handle, [A-Za-z0-9_-]{1,64}
  "key": "updates:update.home_assistant_operating_system@17.1",
  "kind": "update",                   // update | reminder | investigation | proposal | check | backup | reload | window | maintenance
  "kicker": "Operating system update",// optional subtype label; the panel falls back to a label for `kind`
  "ref": "#18",                       // optional short reference shown beside the kicker
  "title": "Home Assistant OS 17.0 → 17.1",
  "state": "waiting",                 // proposed | waiting | scheduled | running | done | failed | cancelled | expired
  "gist": "installed, the host needs a reboot to finish",
  "at": "2026-01-15T04:12:00Z",       // the time a row shows: needs_since, the due/run time, or resolved_at
  "quiet": false,                     // routine entry: History folds a day's quiet items into one line
  "revision": 7,                      // bumped on every change; actions must send it
  "as_of": "2026-01-15T09:41:00Z",    // last daemon observation behind state and facts
  "needs_since": "2026-01-15T04:12:00Z", "resolved_at": null,
  "created_at": "2026-01-11T04:00:00Z", "updated_at": "2026-01-15T04:12:00Z",

  "need": {                           // non-null only while the item is in Needs you (see below)
    "kind": "approval",               // approval | confirm | physical | answer | ack
    "text": "17.1 is installed and waiting for one reboot. …",  // what happens, when, cost, what cannot be undone
    "primary": { "id": "reboot_now", "label": "Reboot now" },
    "secondary": [ { "id": "reboot_window", "label": "Reboot tonight at 04:00" },
                   { "id": "reject", "label": "Reject", "style": "text" } ],
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

An action is `{id, label, style?}`. `id` matches `[a-z_]{1,32}` and is
only meaningful on this item. `style` is `"secondary"` (the default),
`"text"` (a quiet text button) or `"danger"` (Stop). `need.primary` is
always the one filled button. `actor` is `you`, `daemon` or `agent`.

### Needs you

An item is in Needs you if and only if its `state` is `waiting`, or
`failed` with `need.kind = "ack"`. `need` is non-null exactly then. The
daemon decides; the panel never counts or infers it.

| state | Home section | panel word |
| --- | --- | --- |
| `waiting`, or `failed` with a need | Needs you | Needs you / Failed |
| `running` | In progress | Running |
| `proposed`, `scheduled` | Coming up | Proposed / Scheduled |
| `proposed` maintenance replacement candidates and suggestions | Suggestions | Proposed |
| `done`, `failed`, `cancelled`, `expired` resolved today (house time) | Today | Done / Failed / Cancelled / Expired |

"Installed, reboot pending" is `waiting` with a need. It is never
`failed`.

## Endpoints

### `GET api/home`

```jsonc
{
  "as_of": "…",
  "needs_count": 2,                   // == sections.needs.length; also the nav badge
  "sections": { "needs": [Item], "running": [Item], "coming": [Item], "suggestions": [Item], "today": [Item] },
  "calm": "Checked at 05:30, nothing new since the baseline. …",  // the All-quiet text; null when needs is not empty
  "strip": [ { "label": "Health", "value": "checked 05:30, nothing new" },
             { "label": "Backup", "value": "03:59, encrypted" },
             { "label": "Next window", "value": "tonight 04:00" },
             { "label": "AI", "value": "on, 1.4 % of 7-day budget" } ]
}
```

Items in the sections omit `events`. Each section is ordered by the daemon:
Needs you oldest `needs_since` first, the others by `at`.

### `GET api/items/<id>`

`200 {"item": Item}` with `events`. Unknown id: `404 {"error": "no_such_item"}`.

### `GET api/history?filter=<f>&before=<cursor>`

`f` is `all` (default), `needs`, `changes` (proposals, updates, reloads)
or `checks` (checks, backups). Newest first, 50 per page.

```jsonc
{ "items": [Item], "counts": { "needs": 2 }, "more": true, "cursor": "opaque" }
```

`counts.needs` is the same number as `needs_count`. The panel groups the
items by day in the house time zone, and folds each day's `quiet` items
into one line.

### `POST api/items/<id>/act`

Body: `{"action": "<action id>", "revision": 7}`. `action` must be the id of
`need.primary`, one of `need.secondary`, or one of `actions`. The daemon
re-checks everything; the panel's copy is never trusted.

| status | body | meaning |
| --- | --- | --- |
| 200 | `{"item": Item}` | done or recorded |
| 409 | `{"error": "revision", "message": "The plan changed: …", "item": Item}` | stale revision; the panel shows the message and the new item |
| 409 | `{"error": "not_available", "message": …}` | the action is not offered any more |
| 409 | `{"error": "not_done", "message": …}` | the daemon refused it (it changed or expired meanwhile) |

Every action is one admin request (ingress admin header and CSRF token),
bound to the item's revision and, for an approval, to the plan item's hash.
High-impact actions are audited as `panel.<action>` with the admin's user
id.

## Live updates

On the existing `api/events` stream (and `api/poll`):

- `item` `{"id", "revision", "state", "needs_count"}` whenever an item
  changes. The panel refetches the open views and ignores revisions older
  than the one it holds.
- `hello`, `reset` and `ping` (every 15 s) are unchanged. The freshness pill
  is green while pings arrive, amber after 40 s without one, and red after
  2 minutes.

## Unchanged endpoints the panel keeps using

- Chat: `GET api/chat`; `GET api/threads/<thread>?before=`;
  `POST api/threads/<thread>/messages|retry|autoretry|read|cancel|remember`;
  `POST api/presence`. (`threads` here are chat threads, not items.) A
  turn's `meta.item` may name an item id, and the panel embeds that item's
  card.
- System sheet: `GET api/state`, `api/ops` (`status`, and
  `profile`: `{path, loaded, problems, git, overlays, push_target}`),
  `api/diagnostics` (the redacted diagnostics export),
  `api/updates` (`control`, `backup`), `api/usage`, `api/suppressions`
  (empty without the suppress house overlay);
  `POST api/ai`, `api/updates/auto`, `api/backups/now`, `api/run`,
  `api/selftest`, `api/suppressions/<id>/revoke`.

Small additions to `api/state`, all optional (the panel omits what is
missing):

- `"mode": "act"` and `"updates": {"auto": true, "window": "04:00"}` for
  the rail's footer ("Act mode · AI on", "Updates automatic at 04:00");
- `"quiet": {"from": "22:00", "to": "07:30"}` for the System sheet.

There is no digest time: nothing pushes unless the admin is needed.

Not served (the daemon answers 404): `api/tasks`, `api/changes`, `api/facts`,
`api/activity`, `api/ack`, `api/approvals*`, `api/changes/<id>/request|reject`,
`api/updates/plan|holds/clear|skip|notes`, `api/confirm/<cid>` and
`api/items/<id>/withdraw`.
