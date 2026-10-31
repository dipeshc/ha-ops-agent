"""The agent never modifies itself, by an allowlist.

Stdlib only: `changepaths` and the guard hook import it.

A change to the ops agent that goes wrong can leave the house without its maintainer and with no
way back, so the agent never makes one; the admin does, by updating the add-on. Every Home
Assistant call passes through:

- `check_http` in `SupervisorClient.request`. GET (and HEAD) are reads. Any other call must be one
  of the shapes in `_POST` below, by exact path (no query, no other spelling) and body: the update
  pipeline (Core, add-on and HACS installs, Core's config check and restart), config entry and
  automation/template reloads, the agent's own status sensors, its AI switch and the push to the
  admin. Nothing else: not the agent's add-on, the Supervisor, other add-ons, backups or restores,
  the `hassio` services, Core's stop or restart services, or any other state.
- The approved HAOS-update flow: `POST /os/update {"version"}` and `POST /host/reboot {}`
  (never `force`), each only inside `approved_os_flow(<that path>)`, which `updexec.Executors`
  enters after it verified the approval of that OS job; single use, closed when the block ends.
- `check_ws` in `HAWebSocket`: only the commands in `WS_COMMANDS`, with the two subscriptions and
  the AI switch helper pinned to their exact arguments. The dashboard reads (`lovelace/config`, the
  dashboard list and the frontend resources) are reads.
- One dashboard write: `lovelace/config/save` with exactly `{url_path, config}`, only inside
  `approved_dashboard_write(<url_path>, generated=...)`, which `dashboards.DashboardWriter` enters after
  it consumed the admin's approval of that dashboard's plan (or, to restore it, after a failed save of
  that approved plan). Single use, for that one url_path, closed when the block ends; never the default
  dashboard, a malformed url_path or one the house profile marks as generated.

Proposals (the only path that writes the config repository) are checked by `self_path`, and update
jobs by `check_update`.
"""

import contextlib
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from typing import Any

SELF_SLUG = "local_ha_ops_agent"
# The agent's add-on slug under any repository: `local_` for this local add-on, the repository hash otherwise.
# Every one of them is the agent itself, whatever the house profile's `self_slug` says.
_SELF_SLUG_RE = re.compile(r"[a-z0-9]{1,32}_ha_ops_agent")
SELF_TITLE = "ha ops agent"
SUPERVISOR_TITLE = "home assistant supervisor"
SELF_UPDATE_ENTITY = "update.ha_ops_agent_update"
SUPERVISOR_UPDATE_ENTITY = "update.home_assistant_supervisor_update"
OS_UPDATE_ENTITY = "update.home_assistant_operating_system_update"
PROTECTED_UPDATE_ENTITIES = frozenset({SELF_UPDATE_ENTITY, SUPERVISOR_UPDATE_ENTITY})
# Renamed or duplicated (`_2`) copies of the agent's, the Supervisor's and the OS update entities.
_PROTECTED_UPDATE_RE = re.compile(
    r"update\.(?:.*ha_ops_agent.*|home_assistant_supervisor_update(?:_\d+)?"
    r"|home_assistant_operating_system_update(?:_\d+)?)"
)
UPDATE_ENTITY_RE = re.compile(r"update\.[a-z0-9_]{1,128}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
# Repository paths a proposal may never write (with `self_path`).
# `ops/` holds the house profile (`opsd.profile`): the agent's own configuration.
_PATH_DENY_PREFIXES = ("addons/", ".git/", ".git", "/data", "/ssl", "/backup", "/addons", "data/", "ssl/", "backup/",
                       "ops/")  # fmt: skip
# The AI kill switch (`opsd.aicontrol`), the one helper the agent creates and turns on or off.
AI_SWITCH_ENTITY = "input_boolean.ops_agent_ai_enabled"
AI_SWITCH_NAME = "Ops Agent AI enabled"
# The only event the agent subscribes to: the admin's taps and replies on its pushes.
INBOUND_EVENT = "mobile_app_notification_action"

OS_UPDATE_PATH = "/os/update"
HOST_REBOOT_PATH = "/host/reboot"
OS_FLOW_PATHS = frozenset({OS_UPDATE_PATH, HOST_REBOOT_PATH})


class SelfTargetError(Exception):
    """A call would change the agent itself (or the platform it runs on), or is not one the agent makes."""


class _OsFlowScope:
    """One approved OS-flow call: single use, and closed when its `with` block ends.

    The context variable holds this object by reference. `asyncio.to_thread` (the call itself) and
    any task created inside the block inherit the reference, so closing it or using it up ends the
    scope for all of them at once; code running anywhere else sees the default, which allows nothing.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._open = True

    def take(self, path: str) -> bool:
        """True once, for this scope's own path, while the block is open; then it is used up."""
        with self._lock:
            if not self._open or path != self.path:
                return False
            self._open = False
            return True

    def close(self) -> None:
        with self._lock:
            self._open = False


_OS_FLOW: ContextVar[_OsFlowScope | None] = ContextVar("opsd_selfguard_os_flow", default=None)


@contextlib.contextmanager
def approved_os_flow(path: str) -> Iterator[None]:
    """Let exactly one OS-flow POST (`/os/update` or `/host/reboot`) through, for the calls made inside.

    Only `updexec.Executors` enters it, after it verified the admin's approval of the OS job.
    """
    if path not in OS_FLOW_PATHS:
        raise ValueError(f"not an approved OS-flow path: {path!r}")
    scope = _OsFlowScope(path)
    token = _OS_FLOW.set(scope)
    try:
        yield
    finally:
        scope.close()
        _OS_FLOW.reset(token)


# Home Assistant's rule for a storage dashboard's url_path: lower case, with a hyphen. The default dashboard
# has none and is never written.
DASHBOARD_PATH_RE = re.compile(r"[a-z0-9][a-z0-9_]*(?:-[a-z0-9_]+)+")
LOVELACE_SAVE = "lovelace/config/save"


class _DashboardScope:
    """One approved dashboard save: single use, for one url_path, closed when its `with` block ends."""

    def __init__(self, url_path: str) -> None:
        self.url_path = url_path
        self._lock = threading.Lock()
        self._open = True

    def take(self, url_path: Any) -> bool:
        with self._lock:
            if not self._open or url_path != self.url_path:
                return False
            self._open = False
            return True

    def close(self) -> None:
        with self._lock:
            self._open = False


_DASHBOARD: ContextVar[_DashboardScope | None] = ContextVar("opsd_selfguard_dashboard", default=None)


def dashboard_unwritable(url_path: Any, generated: frozenset[str]) -> str | None:
    """Why the agent may never write this dashboard (the default one, a malformed path, a generated one), or None."""
    if not isinstance(url_path, str) or not url_path:
        return "the default dashboard is never written by the agent"
    if len(url_path) > 100 or not DASHBOARD_PATH_RE.fullmatch(url_path):
        return f"{url_path[:60]!r} is not a dashboard url_path"
    if url_path in generated:
        return f"{url_path} is generated (dashboards.generated in the house profile): change what writes it instead"
    return None


@contextlib.contextmanager
def approved_dashboard_write(url_path: str, *, generated: frozenset[str]) -> Iterator[None]:
    """Let exactly one `lovelace/config/save` for `url_path` through, for the calls made inside.

    Only `dashboards.DashboardWriter` enters it, after it consumed the admin's approval of that dashboard's plan.
    """
    problem = dashboard_unwritable(url_path, generated)
    if problem is not None:
        raise SelfTargetError(problem)
    scope = _DashboardScope(url_path)
    token = _DASHBOARD.set(scope)
    try:
        yield
    finally:
        scope.close()
        _DASHBOARD.reset(token)


def _dashboard_save_allows(data: Mapping[str, Any]) -> bool:
    """True once for the save the current approved scope names, with exactly a url_path and a config mapping."""
    scope = _DASHBOARD.get()
    if scope is None or set(data) != {"url_path", "config"} or not isinstance(data.get("config"), Mapping):
        return False
    return scope.take(data.get("url_path"))


def _os_flow_allows(path: str, body: Any) -> bool:
    """True only for the one POST the current approved OS-flow scope names, with a plain body (uses it up)."""
    scope = _OS_FLOW.get()
    if scope is None or path not in OS_FLOW_PATHS:
        return False
    if path == HOST_REBOOT_PATH:
        # Never `force`: it skips the Supervisor's check for a Core offline database migration.
        plain = body is None or (isinstance(body, Mapping) and not body)
    else:
        plain = isinstance(body, Mapping) and set(body) == {"version"} and isinstance(body["version"], str)
        plain = plain and bool(body["version"])
    return plain and scope.take(path)


def is_self_slug(slug: str) -> bool:
    """The agent's own add-on: `self`, the local slug, or the agent installed from any repository."""
    value = slug.strip().lower()
    return value in (SELF_SLUG, "self") or bool(_SELF_SLUG_RE.fullmatch(value))


def protected_update_entity(entity_id: str) -> bool:
    """The agent's, the Supervisor's or the OS update entity, including renamed and `_2` copies."""
    return bool(_PROTECTED_UPDATE_RE.fullmatch(entity_id.strip().lower()))


def _one_update(_match: re.Match[str], body: Any) -> bool:
    """`update.install` exactly as the executor sends it: one update entity (never the agent's, the
    Supervisor's or the OS's), and optionally the version."""
    if not isinstance(body, Mapping) or "entity_id" not in body or not set(body) <= {"entity_id", "version"}:
        return False
    entity, version = body["entity_id"], body.get("version", "x")
    return (
        isinstance(entity, str)
        and UPDATE_ENTITY_RE.fullmatch(entity) is not None
        and not protected_update_entity(entity)
        and isinstance(version, str)
        and _VERSION_RE.fullmatch(version) is not None
    )


def _empty(_match: re.Match[str], body: Any) -> bool:
    return body is None or (isinstance(body, Mapping) and not body)


def _core_update(_match: re.Match[str], body: Any) -> bool:
    return (
        isinstance(body, Mapping)
        and set(body) == {"version", "backup"}
        and isinstance(body["version"], str)
        and bool(body["version"])
        and body["backup"] is False
    )


def _addon_update(match: re.Match[str], body: Any) -> bool:
    return not is_self_slug(match.group(1)) and bool(body == {"backup": False})


def _ai_switch(_match: re.Match[str], body: Any) -> bool:
    return bool(body == {"entity_id": AI_SWITCH_ENTITY})


def _any_body(_match: re.Match[str], _body: Any) -> bool:
    return True


_POST: tuple[tuple[re.Pattern[str], Callable[[re.Match[str], Any], bool]], ...] = tuple(
    (re.compile(pattern), check)
    for pattern, check in (
        (r"/core/update", _core_update),
        (r"/store/addons/([a-z0-9_]{1,64})/update", _addon_update),
        (r"/core/check", _empty),
        (r"/core/restart", _empty),
        (r"/core/api/config/core/check_config", _empty),
        (r"/core/api/config/config_entries/entry/[A-Za-z0-9_-]{1,64}/reload", _empty),
        (r"/core/api/services/update/install", _one_update),
        (r"/core/api/services/(?:automation|template)/reload", _empty),
        (r"/core/api/services/homeassistant/reload_custom_templates", _empty),
        (r"/core/api/services/input_boolean/turn_(?:on|off)", _ai_switch),
        (r"/core/api/services/notify/mobile_app_[a-z0-9_]{1,64}", _any_body),
        (r"/core/api/states/sensor\.ops_agent(?:_[a-z0-9_]{1,40})?", _any_body),
    )
)


def check_http(method: str, path: str, body: Any = None) -> None:
    """Raise SelfTargetError unless the call is a read or one of the calls the agent makes (`_POST`)."""
    verb = method.upper()
    if verb in ("GET", "HEAD"):
        return
    if verb == "POST":
        if _os_flow_allows(path, body):
            return
        for pattern, ok in _POST:
            match = pattern.fullmatch(path)
            if match is not None:
                if ok(match, body):
                    return
                raise SelfTargetError(f"POST {path[:80]}: refused (not the body the agent sends)")
    raise SelfTargetError(f"{method} {path[:80]!r}: refused (not a call the agent makes)")


# Every websocket command the daemon sends: its reads, the pre-update backup, the AI switch helper and
# its two subscriptions (`auth` is the handshake, outside `command`).
WS_COMMANDS = frozenset({
    "ping", "subscribe_events", "subscribe_trigger",
    "backup/info", "backup/generate_with_automatic_settings",
    "config_entries/get", "config/entity_registry/list", "config/entity_registry/get", "config/area_registry/list",
    "config/auth/list", "repairs/list_issues", "system_log/list", "homeassistant/expose_entity/list",
    "hacs/repository/info", "update/release_notes", "history/history_during_period",
    "recorder/statistics_during_period", "vacuum/get_segments", "input_boolean/list", "input_boolean/create",
    "lovelace/config", "lovelace/dashboards/list", "lovelace/resources",
})  # fmt: skip


def check_ws(msg_type: str, fields: Mapping[str, Any] | None = None) -> None:
    """Raise SelfTargetError unless this is a command the agent sends, with its pinned arguments."""
    kind = str(msg_type)
    # `id` numbers the message on the connection; it is not an argument.
    data = {k: v for k, v in (fields or {}).items() if k != "id"}
    if kind == LOVELACE_SAVE:
        if not _dashboard_save_allows(data):
            raise SelfTargetError(f"websocket {kind}: refused (only the approved dashboard plan's one save)")
        return
    if kind not in WS_COMMANDS:
        raise SelfTargetError(f"websocket {kind[:60]!r}: refused (not a command the agent sends)")
    pinned = {
        "subscribe_events": {"event_type": INBOUND_EVENT},
        "subscribe_trigger": {"trigger": {"platform": "state", "entity_id": AI_SWITCH_ENTITY}},
        "lovelace/dashboards/list": {},
        "lovelace/resources": {},
    }.get(kind)
    if pinned is not None and data != pinned:
        raise SelfTargetError(f"websocket {kind}: refused (only the agent's own subscription)")
    if kind == "input_boolean/create" and data.get("name") != AI_SWITCH_NAME:
        raise SelfTargetError("websocket input_boolean/create: only the agent's AI switch")
    if kind == "lovelace/config" and not set(data) <= {"url_path"}:
        # A read; `force` and anything else are not arguments the agent sends.
        raise SelfTargetError("websocket lovelace/config: only a url_path")


def check_update(entity_id: str, kind: str, slug: str | None = None, title: str | None = None) -> None:
    """Update jobs: never the agent itself or the Supervisor (the OS only through its approval flow)."""
    entity = entity_id.strip().lower()
    name = (title or "").strip().lower()
    if kind.strip().lower() == "supervisor" or name == SUPERVISOR_TITLE:
        raise SelfTargetError(f"{entity_id}: the agent never updates itself or the Supervisor")
    if is_self_slug(slug or "") or name == SELF_TITLE:
        raise SelfTargetError("the agent never updates its own add-on")
    if entity in PROTECTED_UPDATE_ENTITIES or (kind != "os" and protected_update_entity(entity)):
        raise SelfTargetError(f"{entity_id}: the agent never updates itself or the Supervisor")


def self_path(path: str) -> bool:
    """A repository or file path the agent may never write: its add-on, git internals, /data, keys."""
    p = path.strip().replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    lowered = p.lower()
    return (
        lowered.startswith(_PATH_DENY_PREFIXES)
        or "/.git/" in lowered
        or lowered.endswith("/.git")
        or "ha-ops-agent" in lowered
        or "ha_ops_agent" in lowered
        or "github_deploy" in lowered
    )
