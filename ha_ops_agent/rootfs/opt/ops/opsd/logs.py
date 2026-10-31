"""Structured JSON logging; contextual fields go in `extra=`."""

import json
import logging
import sys
from datetime import UTC, datetime

from opsd.redact import Redactor

_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with extras as top-level fields, redacted."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        line = json.dumps(payload, default=str, sort_keys=False)
        return self._redactor.redact(line)


def setup_logging(level: str, redactor: Redactor | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(redactor))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
