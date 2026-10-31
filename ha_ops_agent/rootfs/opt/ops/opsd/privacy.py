"""What the agent may see of one entity (`opsctl history`, `opsctl entities --entity`).

Refused outright, with "not available to the agent (privacy)":
- domains that hold free text or personal data: input_text, text, calendar,
  todo, conversation;
- entity ids that carry location, network or usage details
  (`*_geocoded_location`, `*_wifi_ssid`, `*_public_ip`, ...);
- every entity of the `mobile_app` integration (the phone's own sensors)
  except an allowlist of battery and storage sensors, judged from the
  entity registry.

What is returned for the rest: the state (capped), plus the unit and the
friendly name, never any other attribute. Cameras, media players, people,
device trackers and images return their state alone (a zone, never
coordinates; never `access_token` or `entity_picture`).
"""

from collections.abc import Mapping
from typing import Any

PRIVATE = "not available to the agent (privacy)"
DENY_DOMAINS = frozenset({"input_text", "text", "calendar", "todo", "conversation"})
DENY_PATTERNS = (
    "geocoded_location",
    "last_notification",
    "wifi_ssid",
    "wifi_bssid",
    "ip_address",
    "public_ip",
    "last_used_app",
)
MOBILE_APP_ALLOW = (
    "battery_level",
    "battery_state",
    "battery_health",
    "battery_temperature",
    "charger_type",
    "is_charging",
    "storage",
)
STATE_ONLY_DOMAINS = frozenset({"camera", "media_player", "person", "device_tracker", "image"})
MAX_STATE_CHARS = 64


def refusal(entity_id: str, registry_entry: Mapping[str, Any] | None) -> str | None:
    """Why this entity is withheld from the agent, or None."""
    domain, _, object_id = entity_id.partition(".")
    if domain in DENY_DOMAINS or any(p in object_id for p in DENY_PATTERNS):
        return f"{entity_id} is {PRIVATE}"
    platform = registry_entry.get("platform") if isinstance(registry_entry, Mapping) else None
    if platform == "mobile_app" and not object_id.endswith(MOBILE_APP_ALLOW):
        return f"{entity_id} is {PRIVATE}: phone sensors other than battery and storage"
    return None


def find(registry: Any, entity_id: str) -> Mapping[str, Any] | None:
    for entry in registry if isinstance(registry, list) else []:
        if isinstance(entry, Mapping) and entry.get("entity_id") == entity_id:
            return entry
    return None


def describe(state: Any, entity_id: str) -> dict[str, Any]:
    """The current state with the only attributes ever returned: unit and friendly name."""
    info: dict[str, Any] = {"entity_id": entity_id}
    if not isinstance(state, Mapping):
        return info
    current = state.get("state")
    info["state"] = current[:MAX_STATE_CHARS] if isinstance(current, str) else None
    changed = state.get("last_changed")
    if isinstance(changed, str):
        info["last_changed"] = changed[:40]
    if entity_id.split(".", 1)[0] in STATE_ONLY_DOMAINS:
        return info
    raw = state.get("attributes")
    attrs: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    for key, name in (("unit_of_measurement", "unit"), ("friendly_name", "name")):
        value = attrs.get(key)
        if isinstance(value, str):
            info[name] = value[:80]
    return info
