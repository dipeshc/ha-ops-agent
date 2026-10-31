"""Reload one integration's config entries, the agent's own first repair.

The agent may reload an integration whenever it judges that it is needed: for a config
entry stuck in `setup_retry`, when the admin asks in the chat, or before an overlay's
check that reads an integration's cache. A reload changes no
configuration; at worst one integration's entities are unavailable for a few seconds.

Everything that bounds the power lives here, in code, and fails closed:

- Allowlist: only a domain in the admin's `capabilities.reloadable_domains`, else the house
  profile's `reload.domains` (none without a profile).
- Never, even if allowlisted: `DENY_DOMAINS` (locks, the alarm, anything that opens the
  house, Bluetooth, the bridges and the plumbing the house and the agent depend on), the
  house profile's `reload.deny`, a domain the admin blocks with
  `integration.reload.<domain>` in `capabilities.never`, and any domain whose entities
  include a lock, an alarm panel, a cover that is not positively a
  blind/shade/curtain/awning/shutter, or a switch/button/valve named after a garage,
  gate, door or lock (checked live against the registry and the states).
- At most one reload per domain every 10 minutes (persisted; a corrupt state file refuses),
  on top of the capability's daily quota, which is only taken once every check passed.
- Never while one of the integration's vacuums is cleaning, returning or paused.
- One overall deadline below the 120 s RPC timeout; a partial reload is reported.

The domain is the only input. Entry ids are resolved here from Core, never taken from
the caller.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from opsd.access import named_like_access
from opsd.api import LiveAPI
from opsd.capabilities import Args, CapabilityError
from opsd.jsonstore import read_json, write_json_atomic
from opsd.policy import NEVER_RELOAD_DOMAINS

if TYPE_CHECKING:
    from opsd.runctx import ExecContext

_LOGGER = logging.getLogger(__name__)

DOMAIN_RE = re.compile(r"[a-z0-9_]{1,64}")
ENTRY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
RATE_LIMIT = timedelta(minutes=10)
RECOVER_TIMEOUT_S = 60.0
# The whole reload (checks, every entry, the wait) ends before the RPC's 120 s timeout.
DEADLINE_S = 100.0
POST_TIMEOUT_S = 30.0
POLL_S = 3.0
STATE_FILE = "reloads.json"
NEVER_PREFIX = "integration.reload."

# Never reloaded, even when allowlisted: the generic list in code (`policy.NEVER_RELOAD_DOMAINS`) plus the house
# profile's additions (`reload.deny`, e.g. a forked integration that also switches other loads), which can only add.
DENY_DOMAINS: frozenset[str] = NEVER_RELOAD_DOMAINS
PROTECTED_ENTITY_DOMAINS = frozenset({"lock", "alarm_control_panel"})
SAFE_COVER_CLASSES = frozenset({"blind", "shade", "curtain", "awning", "shutter"})
NAMED_ACCESS_DOMAINS = frozenset({"switch", "button", "valve", "cover"})
# Named access hardware (`opsd.access`): a room name alone is not, and a gadget's child lock is removed before
# the check instead of blocking the whole integration.
BUSY_VACUUM_STATES = frozenset({"cleaning", "returning", "paused"})

# One reload decision at a time; domains being reloaded right now.
_GATE = asyncio.Lock()
_INFLIGHT: set[str] = set()


class ReloadAPI(Protocol):
    async def ws(self, msg_type: str, **fields: Any) -> Any: ...

    async def core_rest(self, path: str) -> Any: ...

    async def core_post(self, path: str) -> Any: ...


def static_refusal(domain: str, allowed: Iterable[str], never: Iterable[str] = (),
                   deny: Iterable[str] = ()) -> str | None:  # fmt: skip
    """Why `domain` cannot be reloaded without asking Core, or None. `never` is `capabilities.never`; `deny` is the
    house profile's addition to `DENY_DOMAINS`."""
    if not DOMAIN_RE.fullmatch(domain):
        return "not an integration domain"
    if domain in DENY_DOMAINS or domain in set(deny):
        return f"{domain} is on the agent's never-reload list (locks, alarm, access, radios or core plumbing)"
    if f"{NEVER_PREFIX}{domain}" in set(never):
        return f"{domain} is blocked by capabilities.never"
    if domain not in set(allowed):
        return f"{domain} is not in the reloadable integrations (capabilities.reloadable_domains or the house profile)"
    return None


def _state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILE


def _load_state(data_dir: Path) -> dict[str, datetime]:
    """Last reload per domain. Corrupt, naive or odd content refuses (fail closed)."""
    try:
        raw = read_json(_state_path(data_dir), {})
    except (ValueError, OSError) as err:
        raise CapabilityError(f"reload state unreadable ({type(err).__name__}); not reloading") from err
    if not isinstance(raw, dict):
        raise CapabilityError("reload state is malformed; not reloading")
    state: dict[str, datetime] = {}
    for name, value in raw.items():
        try:
            when = datetime.fromisoformat(str(value))
        except ValueError as err:
            raise CapabilityError("reload state has a bad timestamp; not reloading") from err
        if when.tzinfo is None:
            raise CapabilityError("reload state has a naive timestamp; not reloading")
        state[str(name)] = when
    return state


def rate_refusal(data_dir: Path, domain: str, now: datetime) -> str | None:
    last = _load_state(data_dir).get(domain)
    # A future timestamp (clock jump, hand edit) is ignored rather than blocking forever.
    if last is not None and last <= now and now - last < RATE_LIMIT:
        wait = int((RATE_LIMIT - (now - last)).total_seconds() // 60) + 1
        return f"{domain} was reloaded less than 10 minutes ago; try again in about {wait} min"
    if domain in _INFLIGHT:
        return f"{domain} is being reloaded right now"
    return None


def _mark(data_dir: Path, domain: str, now: datetime) -> None:
    state = _load_state(data_dir)
    cutoff = now - timedelta(days=2)
    kept = {k: v.isoformat() for k, v in state.items() if cutoff < v <= now}
    kept[domain] = now.isoformat()
    write_json_atomic(_state_path(data_dir), kept)


def _require_list(value: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise CapabilityError(f"unexpected {what} reply from Home Assistant; not reloading")
    return [v for v in value if isinstance(v, dict)]


def _domain_entities(registry: list[dict[str, Any]], domain: str, entry_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Entities of the domain's platform or of its config entries (an entry can create other platforms)."""
    ids = set(entry_ids)
    return [
        e
        for e in registry
        if isinstance(e.get("entity_id"), str) and (e.get("platform") == domain or e.get("config_entry_id") in ids)
    ]


def _protected(entities: Iterable[Mapping[str, Any]], states: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Fail closed: anything that might open the house protects the whole domain."""
    for entity in entities:
        entity_id = str(entity["entity_id"])
        kind = entity_id.partition(".")[0]
        if kind in PROTECTED_ENTITY_DOMAINS:
            return f"it provides {entity_id}"
        state = states.get(entity_id, {})
        raw_attributes = state.get("attributes")
        attributes: Mapping[str, Any] = raw_attributes if isinstance(raw_attributes, dict) else {}
        if kind == "cover" and str(attributes.get("device_class")) not in SAFE_COVER_CLASSES:
            return f"it provides {entity_id} (not positively a blind, shade, curtain, awning or shutter)"
        if kind in NAMED_ACCESS_DOMAINS:
            names = " ".join(
                str(x).lower()
                for x in (entity_id, entity.get("name"), entity.get("original_name"), attributes.get("friendly_name"))
                if x
            )
            if named_like_access(names):
                return f"it provides {entity_id} (named like an access control)"
    return None


def _states_map(raw: Any) -> dict[str, dict[str, Any]]:
    return {str(s["entity_id"]): s for s in _require_list(raw, "states") if isinstance(s.get("entity_id"), str)}


async def plan(api: ReloadAPI, domain: str, allowed: Iterable[str], never: Iterable[str] = (),
               deny: Iterable[str] = ()) -> dict[str, Any]:  # fmt: skip
    """The checks that need Core. Raises CapabilityError with a reason safe to show."""
    refusal = static_refusal(domain, allowed, never, deny)
    if refusal is not None:
        raise CapabilityError(refusal)
    entries = [
        e
        for e in _require_list(await api.ws("config_entries/get"), "config entries")
        if e.get("domain") == domain
        and not e.get("disabled_by")
        and isinstance(e.get("entry_id"), str)
        and ENTRY_ID_RE.fullmatch(str(e["entry_id"]))
    ]
    if not entries:
        raise CapabilityError(f"no enabled config entry for {domain}")
    registry = _require_list(await api.ws("config/entity_registry/list"), "entity registry")
    entities = _domain_entities(registry, domain, [str(e["entry_id"]) for e in entries])
    states = _states_map(await api.core_rest("/states"))
    reason = _protected(entities, states)
    if reason is not None:
        raise CapabilityError(f"{domain} is never reloaded: {reason}")
    busy = [
        e["entity_id"]
        for e in entities
        if str(e["entity_id"]).startswith("vacuum.")
        and states.get(e["entity_id"], {}).get("state") in BUSY_VACUUM_STATES
    ]
    if busy:
        raise CapabilityError(f"not while {', '.join(busy)} is cleaning or paused; try again when it is docked")
    return {"entries": entries, "entities": [str(e["entity_id"]) for e in entities], "states": states}


def _unavailable(states: Mapping[str, Mapping[str, Any]], entity_ids: Iterable[str]) -> list[str]:
    return sorted(e for e in entity_ids if states.get(e, {}).get("state") == "unavailable")


async def reload_domain(
    api: ReloadAPI,
    data_dir: Path,
    domain: str,
    *,
    allowed: Iterable[str],
    never: Iterable[str] = (),
    deny: Iterable[str] = (),
    now: datetime | None = None,
    reserve: Callable[[], bool] | None = None,
    wait_s: float = RECOVER_TIMEOUT_S,
    poll_s: float = POLL_S,
    deadline_s: float = DEADLINE_S,
    post_timeout_s: float = POST_TIMEOUT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Reload every enabled entry of `domain`, then wait for its entities. Raises CapabilityError if refused.

    `reserve` takes the daily quota unit; it runs only once every check has passed.
    """
    start = clock()
    when = now or datetime.now(UTC)
    allowed = frozenset(allowed)
    never = tuple(never)
    deny = frozenset(deny)
    async with _GATE:
        refusal = static_refusal(domain, allowed, never, deny) or rate_refusal(data_dir, domain, when)
        if refusal is not None:
            raise CapabilityError(refusal)
        planned = await plan(api, domain, allowed, never, deny)
        if reserve is not None and not reserve():
            raise CapabilityError("integration.reload daily quota reached")
        _INFLIGHT.add(domain)
    try:
        return await _run(api, data_dir, domain, planned, when=when, start=start, wait_s=wait_s, poll_s=poll_s,
                          deadline_s=deadline_s, post_timeout_s=post_timeout_s, sleep=sleep,
                          clock=clock)  # fmt: skip
    finally:
        _INFLIGHT.discard(domain)


async def _run(
    api: ReloadAPI,
    data_dir: Path,
    domain: str,
    planned: dict[str, Any],
    *,
    when: datetime,
    start: float,
    wait_s: float,
    poll_s: float,
    deadline_s: float,
    post_timeout_s: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> dict[str, Any]:
    def left() -> float:
        return deadline_s - (clock() - start)

    entity_ids: list[str] = planned["entities"]
    before = _unavailable(planned["states"], entity_ids)
    failed: list[str] = []
    skipped: list[str] = []
    skipped_ids: set[str] = set()
    attempted = 0
    for entry in planned["entries"]:
        title = str(entry.get("title") or domain)[:60]
        budget = min(post_timeout_s, left())
        if budget <= 1:
            skipped.append(title)
            skipped_ids.add(str(entry["entry_id"]))
            continue
        if attempted == 0:
            # The 10-minute lockout starts with the first entry actually reloaded.
            _mark(data_dir, domain, when)
        attempted += 1
        try:
            await asyncio.wait_for(api.core_post(f"/config/config_entries/entry/{entry['entry_id']}/reload"), budget)
        except TimeoutError:
            failed.append(f"{title}: no answer in {budget:.0f} s")
        except Exception as err:  # noqa: BLE001 - report every entry, do not stop at the first
            failed.append(f"{title}: {type(err).__name__}: {str(err)[:120]}")
    still = before
    # Only entities that worked before the reload count against it; some (an unsupported mop mode, a map
    # image) are unavailable all the time and must not keep the wait going or fail the result.
    already = set(before)
    lost: list[str] = []
    waited = 0.0
    if attempted:
        while True:
            try:
                states = _states_map(await asyncio.wait_for(api.core_rest("/states"), max(1.0, min(15.0, left()))))
            except (TimeoutError, CapabilityError) as err:
                failed.append(f"states unreadable after the reload ({type(err).__name__})")
                break
            except Exception as err:  # noqa: BLE001 - the reload happened; report what is unknown
                failed.append(f"states unreadable after the reload ({type(err).__name__})")
                break
            still = _unavailable(states, entity_ids)
            lost = [e for e in still if e not in already]
            if not lost or waited >= wait_s or left() <= poll_s + 1:
                break
            await sleep(poll_s)
            waited += poll_s
    not_loaded: list[str] = []
    if attempted and left() > 2:
        # The websocket can drop while an integration reloads; one retry on a fresh connection.
        for attempt in range(2):
            try:
                raw = await asyncio.wait_for(api.ws("config_entries/get"), max(1.0, min(10.0, left())))
                entry_states = {
                    str(e.get("entry_id")): str(e.get("state")) for e in _require_list(raw, "config entries")
                }
                not_loaded = [
                    f"{str(e.get('title') or domain)[:60]} is {entry_states.get(str(e['entry_id']), 'missing')}"
                    for e in planned["entries"]
                    if str(e["entry_id"]) not in skipped_ids and entry_states.get(str(e["entry_id"])) != "loaded"
                ]
                break
            except Exception as err:  # noqa: BLE001 - report, do not fail the reload
                if attempt == 1 or left() <= 3:
                    failed.append(f"entry states unreadable ({type(err).__name__})")
                    break
                await sleep(1.0)
    recovered = sorted(set(before) - set(still))
    ok = attempted > 0 and not failed and not not_loaded and not skipped and not lost
    count = len(planned["entries"])
    message = (
        f"Reloaded {domain} ({attempted} of {count} entr{'y' if count == 1 else 'ies'})."
        + (f" {len(recovered)} entit{'y' if len(recovered) == 1 else 'ies'} came back." if recovered else "")
        + (f" Newly unavailable: {', '.join(lost[:8])}." if lost and attempted else "")
        + (
            f" {len(already & set(still))} were already unavailable before the reload."
            if already & set(still) and attempted
            else ""
        )
        + (f" Not reached before the deadline: {', '.join(skipped)}." if skipped else "")
        + (f" Problems: {'; '.join([*failed, *not_loaded])[:300]}." if failed or not_loaded else "")
    )
    _LOGGER.info("integration reloaded", extra={"domain": domain, "ok": ok, "attempted": attempted})
    return {
        "domain": domain,
        "ok": ok,
        "entries": count,
        "attempted": attempted,
        "skipped": skipped,
        "recovered": recovered,
        "still_unavailable": still if attempted else [],
        "newly_unavailable": lost if attempted else [],
        "problems": [*failed, *not_loaded],
        "message": message,
    }


def validate_reload(ctx: ExecContext, args: Args) -> Args:
    """Allowlist, denylist and the 10-minute limit before policy and quota (no Core call yet)."""
    domain = args.text("domain")
    options = ctx.options
    refusal = static_refusal(
        domain, options.reloadable, options.capabilities.never, options.profile.reload.deny
    ) or rate_refusal(ctx.paths.data, domain, datetime.now(UTC))
    if refusal is not None:
        raise CapabilityError(refusal)
    return args


async def exec_reload(ctx: ExecContext, args: Args) -> dict[str, Any]:
    """`opsctl integration-reload <domain>` (the RPC already took the quota; a refusal refunds it)."""
    domain = args.text("domain")
    options = ctx.options
    async with LiveAPI(ctx.client) as api:
        result = await reload_domain(api, ctx.paths.data, domain, allowed=options.reloadable,
                                     never=options.capabilities.never, deny=options.profile.reload.deny)  # fmt: skip
    if ctx.run is not None:
        ctx.run.effects.notes.append(result["message"][:300])
    return result
