"""What opens the house: the one classification the health check, reloads and dashboard edits share.

Locks, alarm panels and access covers (garage doors, gates) open the house by their domain and device
class (`access_kind`). Anything else counts when its name says it is access hardware (`named_like_access`):
a garage door, an opener, a gate, a door, a lock, unlock, an entry or an entrance. A room name alone
("garage" camera switches) is not access hardware, and a gadget's own child or key lock opens nothing.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

# Domains that are access hardware whatever they are called.
PROTECTED_DOMAINS = frozenset({"lock", "alarm_control_panel"})
# The only covers that open nothing: any other cover (no class, `door`, `garage`, `gate`, an unknown class) is
# treated as one that might (fail closed). Reloads and the health check share this rule.
SAFE_COVER_CLASSES = frozenset({"blind", "shade", "curtain", "awning", "shutter"})

# Covers that open the house (with locks and alarm panels, see `access_kind`). None may be exposed to Assist.
ACCESS_COVER_CLASSES = frozenset({"garage", "gate"})
ACCESS_WORD = re.compile(
    r"(?:^|[_\s.\-])(garage[_\s.\-]?doors?|opener|openers|gate|gates|door|doors|lock|locks|unlock|entry|entrance)(?:[_\s.\-]|$)"
)
# Device-local "locks" that disable a gadget's own buttons (a vacuum's or purifier's child lock) open nothing,
# so they are removed before the access-word check.
BENIGN_LOCK = re.compile(r"(?:child|key|control|panel|keypad)[_\s.\-]?locks?")


def access_kind(entity_id: str, attributes: Mapping[str, Any]) -> str | None:
    """`lock`, `alarm` or `garage` for an entity that opens the house, else None."""
    domain = entity_id.split(".", 1)[0]
    if domain == "lock":
        return "lock"
    if domain == "alarm_control_panel":
        return "alarm"
    if domain == "cover" and (attributes.get("device_class") in ACCESS_COVER_CLASSES or "garage" in entity_id):
        return "garage"
    return None


def named_like_access(text: str) -> bool:
    """Whether names (an entity id, a friendly name, several joined by spaces) say access hardware."""
    return ACCESS_WORD.search(BENIGN_LOCK.sub(" ", text.lower())) is not None


def opens_by_kind(entity_id: str, device_class: Any) -> bool:
    """A lock, an alarm panel, or a cover that is not positively a blind, shade, curtain, awning or shutter."""
    domain = entity_id.split(".", 1)[0]
    return domain in PROTECTED_DOMAINS or (domain == "cover" and str(device_class) not in SAFE_COVER_CLASSES)


def opens_the_house(entity_id: str, *, device_class: Any = None, names: Iterable[Any] = ()) -> bool:
    """`opens_by_kind`, or its id, friendly name or registry names say access hardware (fail closed: without a
    device class a cover counts)."""
    return opens_by_kind(entity_id, device_class) or named_like_access(
        " ".join(str(n) for n in (entity_id, *names) if n)
    )
