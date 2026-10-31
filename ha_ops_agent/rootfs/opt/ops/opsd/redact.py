"""Secret redaction for everything the agent logs, stores or shows to the AI."""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MASK = "***"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Credentials embedded in URLs, e.g. rtsp://user:pass@camera.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), rf"\1{MASK}:{MASK}@"),
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{12,}"), rf"\1{MASK}"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    (
        re.compile(
            r"(?i)((?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|push[_-]?token|client[_-]?secret|webhook[_-]?id)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]{4,}"
        ),
        rf"\1{MASK}",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<email>"),
)

_MIN_LITERAL_LEN = 6


class Redactor:
    """Masks known secret values and common credential shapes."""

    def __init__(self, literals: Iterable[str] = ()) -> None:
        unique = {s for s in literals if len(s) >= _MIN_LITERAL_LEN}
        # Longest first so a secret that contains another is fully masked.
        self._literals = sorted(unique, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for literal in self._literals:
            if literal in text:
                text = text.replace(literal, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_obj(v) for v in value]
        return value


def secrets_yaml_values(path: Path) -> list[str]:
    """Scalar values from `secrets.yaml` (flat `key: value` lines only)."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError, PermissionError:
        return []
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        _, _, value = stripped.partition(":")
        value = value.strip().strip("'\"")
        if value:
            values.append(value)
    return values


def build_redactor(secrets_yaml: Path, extra: Iterable[str] = ()) -> Redactor:
    return Redactor([*secrets_yaml_values(secrets_yaml), *extra])
