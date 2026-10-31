"""Stable fingerprints for log messages, so 'new error pattern' is countable."""

import hashlib
import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I), "<mac>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def normalize(message: str) -> str:
    """Replace volatile tokens (ids, addresses, numbers, quoted values)."""
    text = message.strip().splitlines()[0] if message.strip() else ""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text.strip()[:300]


def fingerprint(logger: str, message: str) -> str:
    digest = hashlib.sha1(f"{logger}|{normalize(message)}".encode(), usedforsecurity=False).hexdigest()
    return digest[:12]
