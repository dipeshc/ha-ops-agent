"""Daemon status that survives restarts (`state.json`); spend and counters live in the store."""

from dataclasses import asdict, dataclass
from pathlib import Path

from opsd.jsonstore import read_json, write_json_atomic


@dataclass(slots=True)
class AgentState:
    version: str = ""
    last_health_at: str | None = None
    last_result: str | None = None
    last_summary: str = ""
    last_run_status: str | None = None
    core_down_since: str | None = None
    core_down_notified: bool = False
    pending_items: int = 0

    @classmethod
    def load(cls, path: Path) -> AgentState:
        raw = read_json(path, {})
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known}) if isinstance(raw, dict) else cls()

    def save(self, path: Path) -> None:
        write_json_atomic(path, asdict(self))
