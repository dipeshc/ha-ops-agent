"""Supervisor REST and Core REST (via the Supervisor proxy), stdlib only."""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from opsd import selfguard

_LOGGER = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """A Supervisor or Core API call failed."""

    def __init__(self, method: str, path: str, status: int | None, detail: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {detail}")
        self.method = method
        self.path = path
        self.status = status


def load_supervisor_token() -> str:
    """Only the daemon (root) has the token; it lives in its environment, never in a file."""
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN unavailable")
    return token


@dataclass(slots=True)
class SupervisorClient:
    """Blocking HTTP client; callers in asyncio wrap it with `asyncio.to_thread`."""

    token: str
    base_url: str = "http://supervisor"
    timeout: float = 30.0

    def request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        accept: str = "application/json",
        timeout: float | None = None,
    ) -> bytes:
        try:
            # The agent never changes itself, the Supervisor or the host, whoever asked (opsd.selfguard).
            selfguard.check_http(method, path, body)
        except selfguard.SelfTargetError as err:
            _LOGGER.warning("self-target call refused", extra={"method": method, "path": path.split("?", 1)[0]})
            raise ApiError(method, path, 403, f"refused: {err}") from err
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", accept)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload: bytes = resp.read()
                return payload
        except urllib.error.HTTPError as err:
            detail = err.read()[:300].decode(errors="replace")
            raise ApiError(method, path, err.code, detail) from err
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            raise ApiError(method, path, None, str(err)) from err

    def supervisor(
        self, path: str, method: str = "GET", body: Any | None = None, *, timeout: float | None = None
    ) -> Any:
        """Supervisor endpoints wrap results as `{"result": "ok", "data": ...}`."""
        raw = json.loads(self.request(method, path, body, timeout=timeout) or b"{}")
        if isinstance(raw, dict) and raw.get("result") == "error":
            raise ApiError(method, path, 200, str(raw.get("message")))
        return raw.get("data", raw) if isinstance(raw, dict) else raw

    def core(self, path: str, method: str = "GET", body: Any | None = None, *, timeout: float | None = None) -> Any:
        raw = self.request(method, f"/core/api{path}", body, timeout=timeout)
        return json.loads(raw) if raw else None

    def logs(self, path: str, lines: int = 200) -> str:
        """Journal-backed log endpoints (`/core/logs`, `/host/logs/...`) as text."""
        query = urllib.parse.urlencode({"lines": max(2, lines), "no_colors": ""})
        return self.request("GET", f"{path}?{query}", accept="text/plain", timeout=60).decode(errors="replace")

    def core_alive(self) -> bool:
        try:
            result = self.core("/")
        except ApiError:
            return False
        return isinstance(result, dict) and "message" in result

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return self.core(f"/services/{domain}/{service}", "POST", data)

    def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> Any:
        return self.core(f"/states/{entity_id}", "POST", {"state": state, "attributes": attributes})


# Supervisor job names that mean the system is busy changing itself.
BUSY_JOB_MARKERS = ("update", "restore", "backup", "rebuild", "restart", "install")


def supervisor_jobs_busy(info: Any) -> bool:
    """An update, restore, backup or Core restart/rebuild is in progress (`/jobs/info`)."""

    def busy(jobs: Any) -> bool:
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            name = str(job.get("name", "")).lower()
            if not job.get("done") and any(m in name for m in BUSY_JOB_MARKERS):
                return True
            if busy(job.get("child_jobs")):
                return True
        return False

    return busy(info.get("jobs") if isinstance(info, dict) else None)
