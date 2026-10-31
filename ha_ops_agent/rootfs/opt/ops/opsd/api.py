"""Live implementation of `HealthAPI` over the Supervisor proxy."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from opsd import evidence
from opsd.supervisor import SupervisorClient
from opsd.ws import HAWebSocket

_LOGGER = logging.getLogger(__name__)

CORE_WS_URL = "ws://supervisor/core/websocket"
GITHUB_RELEASE_URL = "https://api.github.com/repos/{repo}/releases/tags/{tag}"
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_TAG_RE = re.compile(r"[A-Za-z0-9_.+-]{1,64}")


class LiveAPI:
    """One websocket connection per instance; use as an async context manager."""

    def __init__(self, client: SupervisorClient, *, ws_url: str = CORE_WS_URL, max_bytes: int | None = None) -> None:
        self._client = client
        self._ws_url = ws_url
        self._max_bytes = max_bytes
        self._ws: HAWebSocket | None = None

    async def __aenter__(self) -> LiveAPI:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def ws(self, msg_type: str, **fields: Any) -> Any:
        if self._ws is None:
            ws = (
                HAWebSocket(self._ws_url, self._client.token)
                if self._max_bytes is None
                else HAWebSocket(self._ws_url, self._client.token, max_bytes=self._max_bytes)
            )
            await ws.connect()
            self._ws = ws
        return await self._ws.command(msg_type, **fields)

    async def core_rest(self, path: str) -> Any:
        return await asyncio.to_thread(self._client.core, path)

    async def core_post(self, path: str) -> Any:
        return await asyncio.to_thread(self._client.core, path, "POST", {})

    async def supervisor(self, path: str) -> Any:
        return await asyncio.to_thread(self._client.supervisor, path)

    async def supervisor_post(self, path: str, *, http_timeout: float | None = None) -> Any:
        return await asyncio.to_thread(self._client.supervisor, path, "POST", {}, timeout=http_timeout)

    async def logs(self, path: str, lines: int) -> str:
        return await asyncio.to_thread(self._client.logs, path, lines)


def core_release_age_days(version: str, *, timeout: float = 15.0) -> float | None:
    """Age of a Core release from GitHub (unauthenticated; 60 requests/hour is plenty)."""
    return github_release_age_days("home-assistant/core", version, timeout=timeout)


def github_release_age_days(repo: str, tag: str, *, timeout: float = 15.0) -> float | None:
    """Days since a GitHub release was published; None when unknown (the caller then waits).

    Through the hardened fetch: HTTPS to api.github.com only, redirects
    checked against the same allowlist, at most `evidence.MAX_BYTES`.
    """
    if not _REPO_RE.fullmatch(repo) or not _TAG_RE.fullmatch(tag) or ".." in repo:
        return None
    try:
        raw = json.loads(evidence.fetch_text(GITHUB_RELEASE_URL.format(repo=repo, tag=tag), timeout=timeout))
        published = raw.get("published_at") if isinstance(raw, dict) else None
        when = datetime.fromisoformat(str(published).replace("Z", "+00:00")) if published else None
    except (evidence.FetchError, ValueError) as err:
        _LOGGER.warning("release age lookup failed", extra={"repo": repo, "tag": tag, "error": str(err)[:200]})
        return None
    if when is None:
        return None
    return (datetime.now(UTC) - when).total_seconds() / 86400
