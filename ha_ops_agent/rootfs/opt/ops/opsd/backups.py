"""Standalone backups: "Back up now" in the panel and the act-mode self-heal.

Both run Core's `backup/generate_with_automatic_settings` through the audited
`backup.create` executor (the admin's encryption key and agents; the agent
never handles the key), wait for Core's backup manager to finish, and check
the result with `verify_backup` (encrypted on this machine, Home Assistant,
its database and every installed add-on). One backup at a time.

- "Back up now" is the admin's high-impact panel action (`panelapi._run`), at
  most one start an hour, in any mode, and not while a complete backup is
  under 3 hours old.

Each backup counts toward Core's retention (and uploads to every agent), so
repeated starts could push older restore points out: at most `DAILY_CAP`
standalone backups a day, the panel and the self-heal together.
- The self-heal is the agent's own AUTO-tier action, only in `agent.mode: act`,
  only when no complete backup finished in the last 26 hours and Core's
  automatic backup schedule is configured, at most once a day. In observe
  mode the health item says so and the panel offers "Back up now".

The latest result is kept (`LAST_KEY`) for the panel and the health check.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from opsd.health import parse_time
from opsd.store import Store, StoreError
from opsd.updexec import ExecutorRefused, Executors, UpdateOps, verify_backup

_LOGGER = logging.getLogger(__name__)

LAST_KEY = "backup.last"
SELF_HEAL_KEY = "backup.self_heal_at"
MANUAL_GAP = timedelta(hours=1)
SELF_HEAL_GAP = timedelta(hours=24)
# No complete backup finished within this long: the self-heal may run.
SELF_HEAL_AFTER_H = 26.0
TIMEOUT = timedelta(minutes=40)
IDLE_GRACE = timedelta(seconds=90)
POLL_S = 15.0
RECENT_FAILURE = timedelta(days=7)
DAILY_CAP = 3
COUNTER = "standalone_backups"
LATEST_KEY = "backup.latest_complete_at"
# A complete backup newer than this makes "Back up now" pointless.
FRESH = timedelta(hours=3)

Note = Callable[..., Any]


async def wait_for_backup(
    ops: UpdateOps,
    existing: set[str],
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    give_up_after: timedelta = TIMEOUT,
) -> dict[str, Any] | None:
    """The new automatic-settings backup once Core's backup manager is idle again, or None."""
    limit = clock() + give_up_after
    idle_since: datetime | None = None
    while True:
        try:
            state = (await ops.backup_manager()).get("state")
            fresh = [b for b in await ops.core_backups()
                     if str(b.get("backup_id")) not in existing and b.get("with_automatic_settings")]  # fmt: skip
        except Exception:  # noqa: BLE001 - keep waiting until the deadline
            state, fresh = "unknown", []
        if fresh and state == "idle":
            # Core reports local offsets (e.g. +02:00): compare instants, not strings.
            return max(fresh, key=lambda b: parse_time(b.get("date")) or datetime.min.replace(tzinfo=UTC))
        if clock() >= limit:
            return None
        idle_since = (idle_since or clock()) if state == "idle" else None
        if idle_since is not None and clock() - idle_since >= IDLE_GRACE:
            return None
        await sleep(POLL_S)


class Backups:
    def __init__(
        self,
        *,
        ops: UpdateOps,
        executors: Executors,
        store: Store,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        publish: Callable[[], object] = lambda: None,
    ) -> None:
        self.ops = ops
        self.executors = executors
        self.store = store
        self._clock = clock
        self._sleep = sleep
        self._publish = publish
        self._lock = asyncio.Lock()
        self.activity: Note | None = None

    # ------------------------------------------------------------- state

    def last(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.store.kv_get(LAST_KEY) or "{}")
        except StoreError, ValueError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, record: dict[str, Any]) -> None:
        self.store.kv_set(LAST_KEY, json.dumps(record), now=self._clock())
        self._publish()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def _day(self) -> str:
        return self._clock().astimezone(UTC).date().isoformat()

    def capped(self) -> bool:
        """`DAILY_CAP` standalone backups already started today (UTC)."""
        return self.store.count(self._day(), COUNTER) >= DAILY_CAP

    def observe(self, backups: dict[str, Any], taken_at: datetime) -> None:
        """Remember when the newest complete backup was made (from a health snapshot)."""
        age = backups.get("newest_complete_age_h")
        if isinstance(age, (int, float)):
            self._latest(taken_at - timedelta(hours=float(age)))

    def _latest(self, at: datetime) -> None:
        known = parse_time(self.store.kv_get(LATEST_KEY) or None)
        if known is None or at > known:
            self.store.kv_set(LATEST_KEY, at.isoformat(timespec="seconds"), now=self._clock())

    def fresh_minutes(self) -> int | None:
        """Minutes since the newest complete backup, if that is under `FRESH`."""
        at = parse_time(self.store.kv_get(LATEST_KEY) or None)
        if at is None or self._clock() - at >= FRESH:
            return None
        return max(0, int((self._clock() - at).total_seconds() // 60))

    def manual_refusal(self) -> str | None:
        """Why "Back up now" cannot start now, or None."""
        if self.running:
            return "A backup is already running."
        if self.capped():
            return f"At most {DAILY_CAP} backups a day from here (they count toward Home Assistant's retention)."
        fresh = self.fresh_minutes()
        if fresh is not None:
            return f"Latest backup {fresh} min ago."
        later = self.next_manual_at()
        if later is not None:
            return f"One backup an hour: try again after {later:%H:%M} UTC."
        return None

    def next_manual_at(self) -> datetime | None:
        """When "Back up now" is allowed again (one start an hour), or None if it is allowed now."""
        started = parse_time(self.last().get("started_at"))
        if started is None or self._clock() - started >= MANUAL_GAP:
            return None
        return started + MANUAL_GAP

    def view(self) -> dict[str, Any]:
        later = self.next_manual_at()
        return {"last": self.last(), "running": self.running,
                "next_at": later.isoformat(timespec="seconds") if later else None,
                "fresh_minutes": self.fresh_minutes(), "capped": self.capped(),
                "blocked": self.manual_refusal()}  # fmt: skip

    def recent_failure(self) -> str | None:
        """Why the latest backup the agent started failed, if that was in the last week."""
        last = self.last()
        at = parse_time(last.get("finished_at"))
        if last.get("state") != "failed" or at is None or self._clock() - at > RECENT_FAILURE:
            return None
        return str(last.get("detail") or "unknown reason")

    def self_heal_due(self, backups: dict[str, Any], *, acting: bool) -> bool:
        """Act mode, Core's automatic schedule configured, nothing complete in 26 h, none tried in 24 h."""
        if not acting or self.running or self.capped() or not (backups.get("automatic") or {}).get("configured"):
            return False
        age = backups.get("newest_complete_age_h")
        if age is not None and age <= SELF_HEAL_AFTER_H:
            return False
        tried = parse_time(self.store.kv_get(SELF_HEAL_KEY) or None)
        return tried is None or self._clock() - tried >= SELF_HEAL_GAP

    # ------------------------------------------------------------- run

    async def run(self, *, by: str, admin: bool) -> dict[str, Any]:
        """One backup, start to verified: the recorded result (`state` ok or failed)."""
        if self._lock.locked():
            return {"state": "busy", "detail": "a backup is already running"}
        async with self._lock:
            if not self.store.bump(self._day(), COUNTER, limit=DAILY_CAP):
                return {"state": "capped", "detail": f"at most {DAILY_CAP} standalone backups a day"}
            now = self._clock()
            if not admin:
                self.store.kv_set(SELF_HEAL_KEY, now.isoformat(timespec="seconds"), now=now)
            record: dict[str, Any] = {"state": "running", "by": by, "started_at": now.isoformat(timespec="seconds")}
            self._save(record)
            try:
                existing = {str(b.get("backup_id")) for b in await self.ops.core_backups()}
                await self.executors.backup(standalone=True, admin=admin)
                found = await wait_for_backup(self.ops, existing, clock=self._clock, sleep=self._sleep)
                if found is None:
                    raise ExecutorRefused("the backup did not complete")
                verified = verify_backup(found, addons=await self.ops.installed_addons())
            except ExecutorRefused as err:
                record |= {"state": "failed", "detail": str(err)[:200]}
            except Exception as err:
                _LOGGER.exception("backup failed")
                record |= {"state": "failed", "detail": f"{type(err).__name__}: {str(err)[:160]}"}
            else:
                record |= {"state": "ok", "name": verified.name, "detail": "encrypted, complete"}
                self._latest(self._clock())
            record["finished_at"] = self._clock().isoformat(timespec="seconds")
            self._save(record)
            self._note(record)
            return record

    def _note(self, record: dict[str, Any]) -> None:
        if self.activity is None:
            return
        ok = record["state"] == "ok"
        title = f"Backup ({'Back up now' if record['by'].startswith('panel') else 'self-heal'}):" \
                f" {'done' if ok else 'failed'}"  # fmt: skip
        try:
            self.activity("backup", title, str(record.get("name") or record.get("detail") or ""), needs_admin=not ok)
        except Exception:
            _LOGGER.exception("activity note failed")
