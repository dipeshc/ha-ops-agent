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

A config entry's settings (`opsctl entries`, `entry_settings`): only the allowlisted keys
(`ENTRY_SETTING_KEYS`), and only values that look like no secret, address or place (spec capabilities.md K12a).
"""

import re
from collections.abc import Callable, Mapping
from typing import Any

from opsd.access import named_like_access

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
    """The current state with the only attributes ever returned: unit, friendly name, device class and a light's
    effect (what a release note's "affects only X" is decided by)."""
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
    for key, name in (
        ("unit_of_measurement", "unit"),
        ("friendly_name", "name"),
        ("device_class", "device_class"),
        ("effect", "effect"),
    ):
        value = attrs.get(key)
        if isinstance(value, str):
            info[name] = value[:80]
    return info


# ------------------------------------------------------------------ config entry settings (`opsctl entries`)

# A config entry's `data` and `options` as the agent may read them (spec capabilities.md K12a): only the keys below,
# plain choices such as a country or a list of weekdays. Everything else is withheld (fail closed).
# Adding a key widens what the AI reads: it needs a review (no key here may ever carry a credential, an address, a
# place or an identity).
ENTRY_SETTING_KEYS = frozenset({
    "country", "province", "region", "subdiv", "state", "language", "workdays", "excludes", "add_holidays",
    "remove_holidays", "days_offset", "unit_system", "temperature_unit", "time_zone", "mode", "scan_interval",
    "update_interval",
})  # fmt: skip
# A key whose value is shown only as a short code (`state` is also a word for anything).
ENTRY_CODE_KEYS = frozenset({"state"})
ENTRY_DENY_DOMAINS = frozenset({"mobile_app"})
ENTRY_MAX_FIELDS = 20
ENTRY_MAX_CHARS = 800
ENTRIES_MAX_CHARS = 16_000
ENTRY_MAX_LIST = 10
ENTRY_MAX_NUMBER = 100_000
_SAFE_VALUE = re.compile(r"[A-Za-z0-9 _.,:/+-]{1,64}")
_CODE_VALUE = re.compile(r"[A-Za-z0-9_-]{1,8}")
# The shape each text key may take (a string or a list item; a key without one keeps `_SAFE_VALUE`): a free-text
# value on a generic key (`mode`, `region`, a holiday list) must not be able to carry a password, a host or an
# address, so no digits, `:`, `=` or `/` outside the shapes that need them.
_NAME_VALUE = re.compile(r"[A-Za-z_-]{1,16}")
_ZONE_VALUE = re.compile(r"[A-Za-z_]+(?:/[A-Za-z_+-]+){0,2}")
_WORDS_VALUE = re.compile(r"[A-Za-z_ -]{1,32}")
_DAY_VALUE = re.compile(r"mon|tue|wed|thu|fri|sat|sun|holiday|\d{4}-\d{2}-\d{2}|[A-Za-z ]{1,40}")
ENTRY_VALUE_SHAPES: Mapping[str, re.Pattern[str]] = {
    "country": _NAME_VALUE, "province": _NAME_VALUE, "subdiv": _NAME_VALUE, "language": _NAME_VALUE,
    "unit_system": _NAME_VALUE, "temperature_unit": _NAME_VALUE, "time_zone": _ZONE_VALUE,
    "mode": _WORDS_VALUE, "region": _WORDS_VALUE,
    "workdays": _DAY_VALUE, "excludes": _DAY_VALUE, "add_holidays": _DAY_VALUE, "remove_holidays": _DAY_VALUE,
}  # fmt: skip
# The second guard: a value on an allowed key that looks like a secret, an address or a place is withheld too.
_SECRET_VALUES = (
    re.compile(r"(?i)://|^www\."),  # a URL
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),  # an IPv4 address
    re.compile(r"(?i)\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,}\b"),  # an IPv6 or MAC address (colon form)
    re.compile(r"(?i)\b[0-9a-f]{2}(?:[-.][0-9a-f]{2}){5}\b"),  # a MAC address (dash or dot form)
    re.compile(r"@"),  # an e-mail address
    re.compile(r"eyJ"),  # a JWT
    re.compile(r"(?i)[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}"),  # a UUID
    re.compile(r"(?i)[0-9a-f]{16,}"),  # a long hex value (a key, a token, an id)
    re.compile(r"\d(?:[ ()+-]?\d){8,}"),  # a phone number or another long number (a date has 8 digits)
    re.compile(r"-?\d{1,3}\.\d{4,}"),  # a coordinate
)


def _base64ish(text: str) -> bool:
    """20 or more token characters mixing letters and digits: an opaque value (a key, a token, an id)."""
    for run in re.findall(r"[A-Za-z0-9+/=_-]{20,}", text):
        if re.search(r"[A-Za-z]", run) and re.search(r"\d", run):
            return True
    return False


def _entry_scalar(key: str, value: Any, redact: Callable[[str], str] | None) -> tuple[bool, Any]:
    """(kept, value) for one scalar of an allowed setting."""
    if isinstance(value, bool):
        return True, value
    if isinstance(value, int | float):
        # NaN compares false; a float with more than 3 decimals could be a coordinate.
        return abs(value) <= ENTRY_MAX_NUMBER and round(value, 3) == value, value
    if not isinstance(value, str) or not _SAFE_VALUE.fullmatch(value):
        return False, None
    if key in ENTRY_CODE_KEYS and not _CODE_VALUE.fullmatch(value):
        return False, None
    shape = ENTRY_VALUE_SHAPES.get(key)
    if shape is not None and not shape.fullmatch(value):
        return False, None
    if _base64ish(value) or any(p.search(value) for p in _SECRET_VALUES):
        return False, None
    if redact is not None and redact(value) != value:
        return False, None
    return True, value


def entry_settings(entry: Mapping[str, Any], redact: Callable[[str], str] | None = None) -> tuple[dict[str, Any], int]:
    """An entry's allowed `data` and `options` (`{"data": {...}, "options": {...}}`, empty parts left out) and how
    many fields were withheld. Only `ENTRY_SETTING_KEYS`, and only values that pass the second guard; `redact`: the
    daemon's redactor (`str -> str`), a value it would change is withheld. Fails closed: an odd shape gives
    nothing."""
    domain = entry.get("domain")
    if not isinstance(domain, str) or domain in ENTRY_DENY_DOMAINS or named_like_access(domain):
        return {}, 0
    out: dict[str, Any] = {}
    withheld = count = size = 0
    for part in ("data", "options"):
        raw = entry.get(part)
        if not isinstance(raw, Mapping):
            continue
        kept: dict[str, Any] = {}
        for key, value in raw.items():
            ok, clean = False, None
            if isinstance(key, str) and key in ENTRY_SETTING_KEYS:
                if isinstance(value, list) and 0 < len(value) <= ENTRY_MAX_LIST:
                    items = [_entry_scalar(key, v, redact) for v in value]
                    ok, clean = all(i[0] for i in items), [i[1] for i in items]
                elif not isinstance(value, list | dict):
                    ok, clean = _entry_scalar(key, value, redact)
            cost = len(str(key)) + len(repr(clean)) + 4
            if not ok or count >= ENTRY_MAX_FIELDS or size + cost > ENTRY_MAX_CHARS:
                withheld += 1
                continue
            kept[key] = clean
            count += 1
            size += cost
        if kept:
            out[part] = kept
    return out, withheld
