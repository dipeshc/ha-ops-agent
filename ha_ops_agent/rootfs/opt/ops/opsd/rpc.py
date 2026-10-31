"""Unix-socket RPC between Claude's `opsctl` (unprivileged) and the daemon.

The socket is owned root:ops, mode 0660, in a root:ops 0750 directory. Each
connection carries one newline-terminated JSON request `{"verb": ..., "args":
{...}}` of at most 8 KiB and gets one answer: `{"ok": true, "output": "..."}`
or `{"ok": false, "error": "..."}`.

Every request goes through the same pipeline, and every request is audited
in the store whatever its outcome (rejections are rate-limited so a flood
cannot fill the disk):

1. Peer: the uid must be `ops` (SO_PEERCRED); root is refused.
2. Binding: an `ops` caller must be a descendant of the active Claude run's
   process; it is then bound to that run's `RunContext` (principal,
   profile, hand-off). With no active run an `ops` caller is refused.
3. Schema: the capability registry validates verb and arguments strictly.
4. Profile: the capability must be in the run's allowlist.
5. Run budget (200 requests per run) and per-capability quotas.
6. Validator, then policy (`policy.decide`): only `auto` and `auto_notify`
   execute; `approve` and `never` are refused here (an approval
   is only ever granted in the panel, never over the socket).
7. Execution with a timeout; output redacted and size-capped.

If the audit row cannot be written, the request is refused (fail closed).
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import socket
import sqlite3
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsd.capabilities import (
    CAPABILITIES,
    MAX_REQUEST_BYTES,
    Args,
    Capability,
    CapabilityError,
    RequestError,
    parse_request,
    request_verb,
    resolve_ref,
    schema,
    unavailable,
)
from opsd.githistory import HistoryError
from opsd.policy import Origin, PolicyTier, Resolution, Verdict, decide
from opsd.runctx import ExecContext, ExecEnv, RunContext
from opsd.store import StoreError
from opsd.supervisor import ApiError

_LOGGER = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 40_000
REQUEST_TIMEOUT_S = 120.0
MAX_CONCURRENT = 2
MAX_CONNECTIONS = 8
# Rejections written to the audit log per minute; the rest are counted.
AUDIT_REJECTIONS_PER_MIN = 60

# Kept for callers and tests that want the request shapes.
SCHEMA = schema()
__all__ = ["MAX_REQUEST_BYTES", "SCHEMA", "RequestError", "RpcServer", "call", "parse_request"]

Executor = Callable[[ExecContext, Args], Any]

# Appended to a profile refusal so the model can explain it (refusals reach the admin as its answer).
_CHAT_HINT = (
    ": a chat can read, schedule reminders and follow-ups, investigate, ask for an update approval and propose"
    " config changes; applying anything to Home Assistant, git, updates or the baseline needs the admin"
    " (Needs you on Home)"
)
_PROFILE_HINTS = {"chat": _CHAT_HINT, "followup": _CHAT_HINT, "investigate": _CHAT_HINT}


@dataclass(frozen=True, slots=True)
class Peer:
    pid: int | None
    uid: int | None


# An in-process caller (tests, the daemon itself).
LOCAL = Peer(pid=None, uid=None)


def peer_creds(sock: socket.socket | None) -> Peer:
    """The connecting process (Linux SO_PEERCRED); unknown where unsupported."""
    option = getattr(socket, "SO_PEERCRED", None)
    if sock is None or option is None:
        return LOCAL
    creds = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", creds)
    return Peer(pid=int(pid), uid=int(uid))


def peer_uid(sock: socket.socket | None) -> int | None:
    return peer_creds(sock).uid


def descends_from(pid: int, ancestor: int, proc: Path = Path("/proc")) -> bool:
    """Is `pid` the `ancestor` process or one of its descendants (via /proc ppid links)?"""
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        try:
            stat = (proc / str(current) / "stat").read_text()
        except OSError:
            return False
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) < 2 or not fields[1].isdigit():
            return False
        current = int(fields[1])
    return current == ancestor


class RejectionLimiter:
    """Caps how many rejected requests are written to the audit log per minute."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._window = clock()
        self._count = 0
        self.suppressed = 0

    def allow(self) -> bool:
        now = self._clock()
        if now - self._window >= 60:
            self._window, self._count = now, 0
        self._count += 1
        if self._count > self._per_minute:
            self.suppressed += 1
            return False
        return True

    def take_suppressed(self) -> int:
        count, self.suppressed = self.suppressed, 0
        return count


class RpcServer:
    def __init__(
        self,
        env: ExecEnv,
        *,
        allowed_uids: frozenset[int] | None = None,
        registry: Mapping[str, Capability] = CAPABILITIES,
        executors: Mapping[str, Executor] | None = None,
        in_run_tree: Callable[[int, int], bool] = descends_from,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        day: Callable[[datetime], str] = lambda now: now.date().isoformat(),
        on_request: Callable[[RunContext, str], None] | None = None,
    ) -> None:
        # Told the capability name of each allowed in-run request (the panel's step labels).
        self.on_request = on_request
        self._env = env
        self._allowed_uids = allowed_uids
        self._registry = registry
        self._executors = dict(executors) if executors is not None else {}
        self._in_run_tree = in_run_tree
        self._clock = clock
        self._day = day
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._connections = 0
        self._server: asyncio.Server | None = None
        self._active: RunContext | None = None
        self._rejections = RejectionLimiter(AUDIT_REJECTIONS_PER_MIN)

    # ------------------------------------------------------------ run binding

    @property
    def active_run(self) -> RunContext | None:
        return self._active

    def begin_run(self, run: RunContext) -> None:
        if self._active is not None:
            raise RuntimeError("another agent run is bound to the socket")
        self._active = run

    def attach_process(self, pid: int) -> None:
        if self._active is not None:
            self._active.root_pid = pid

    def end_run(self) -> None:
        self._active = None

    def _peer_allowed(self, peer: Peer) -> bool:
        """Only the configured uids, never root: nothing but a Claude run uses the socket."""
        if self._allowed_uids is None:
            # Developer machine or in-process call: no uid separation to enforce.
            return True
        return peer.uid is not None and peer.uid != 0 and peer.uid in self._allowed_uids

    def _bind(self, peer: Peer) -> tuple[RunContext | None, str | None]:
        """The run this caller is bound to, or a refusal reason."""
        run = self._active
        if self._allowed_uids is None:
            return run, None
        if not self._peer_allowed(peer):
            return None, "peer not allowed"
        if run is None:
            return None, "no agent run is active"
        if run.root_pid is None or peer.pid is None or not self._in_run_tree(peer.pid, run.root_pid):
            return None, "caller is not part of the active agent run"
        return run, None

    # ------------------------------------------------------------- socket

    async def start(self, *, group_gid: int | None) -> None:
        path = self._env.paths.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, str(path), limit=MAX_REQUEST_BYTES + 2)
        if group_gid is not None:
            os.chown(path.parent, 0, group_gid)
            # Group ops may traverse to the socket; nobody else.
            os.chmod(path.parent, 0o750)  # noqa: S103
            os.chown(path, 0, group_gid)
        os.chmod(path, 0o660)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections += 1
        try:
            peer = peer_creds(writer.get_extra_info("socket"))
            if self._connections > MAX_CONNECTIONS:
                response: dict[str, Any] = {"ok": False, "error": "too many concurrent connections"}
            elif not self._peer_allowed(peer):
                response = {"ok": False, "error": "peer not allowed"}
            else:
                try:
                    raw = await asyncio.wait_for(reader.readline(), 10)
                except ValueError, asyncio.LimitOverrunError:
                    raw = b"x" * (MAX_REQUEST_BYTES + 1)
                response = await self.dispatch(raw, peer)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except TimeoutError, ConnectionError:
            pass
        finally:
            self._connections -= 1
            writer.close()

    # ------------------------------------------------------------ pipeline

    def _executor(self, ref: str) -> Executor:
        found = self._executors.get(ref)
        if found is None:
            found = resolve_ref(ref)
            self._executors[ref] = found
        return found

    def _audit_rejection(
        self, *, run: RunContext | None, peer: Peer, capability: str, args: dict[str, Any], reason: str
    ) -> None:
        store = self._env.store
        if store is None or not self._rejections.allow():
            return
        suppressed = self._rejections.take_suppressed()
        detail = reason + (f" (+{suppressed} rejections not logged)" if suppressed else "")
        try:
            store.audit(
                at=self._clock(),
                run_id=run.run_id if run else None,
                principal=run.principal.label() if run else f"uid:{peer.uid}",
                channel="opsctl",
                capability=capability,
                args=args,
                tier=None,
                decision="rejected",
                outcome="rejected",
                detail=detail,
            )
        except (StoreError, sqlite3.Error, OSError) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    def _resolve(self, cap: Capability, ctx: ExecContext, args: Args) -> Resolution | None:
        if cap.tier_resolver is None:
            return None
        result = resolve_ref(cap.tier_resolver)(ctx, args)
        if not isinstance(result, Resolution):
            raise CapabilityError("tier resolver returned no decision")
        return result

    def _decide(self, cap: Capability, ctx: ExecContext, args: Args, origin: Origin) -> Verdict:
        options = self._env.options
        tighten = {name: PolicyTier.APPROVE for name in options.capabilities.require_approval}
        tighten.update({name: PolicyTier.NEVER for name in options.capabilities.never})
        return decide(
            capability=cap.name,
            default_tier=cap.tier,
            effects=cap.effects,
            origin=origin,
            allowed_origins=cap.origins,
            acting=options.acting,
            resolution=self._resolve(cap, ctx, args),
            tighten=tighten,
        )

    def _reserve_quota(self, cap: Capability, run: RunContext | None, day: str) -> str | None:
        """Take one unit of the per-run and per-day quotas before executing, or refuse.

        Reserving (not counting afterwards) means concurrent requests cannot
        both pass the check. The per-run unit is refunded if execution fails;
        the daily unit is not (conservative).
        """
        if run is not None:
            used = run.counts.get(cap.name, 0)
            if cap.quota.per_run and used >= cap.quota.per_run:
                return f"{cap.name} allows {cap.quota.per_run} call(s) per run"
            run.counts[cap.name] = used + 1
        store = self._env.store
        if cap.quota.per_day and store is not None and not store.bump(day, f"cap:{cap.name}", limit=cap.quota.per_day):
            self._refund(cap, run)
            return f"{cap.name} daily quota reached"
        return None

    @staticmethod
    def _refund(cap: Capability, run: RunContext | None) -> None:
        if run is not None and run.counts.get(cap.name, 0) > 0:
            run.counts[cap.name] -= 1

    async def dispatch(self, raw: bytes, peer: Peer = LOCAL) -> dict[str, Any]:
        redactor = self._env.redactor()
        run, refusal = self._bind(peer)
        verb = request_verb(raw.strip())
        if refusal is not None:
            self._audit_rejection(run=None, peer=peer, capability=verb, args={}, reason=refusal)
            return {"ok": False, "error": refusal}
        if run is not None:
            if run.requests_left <= 0:
                return {"ok": False, "error": "request budget for this run exhausted; finish with what you have"}
            run.requests_left -= 1
        try:
            cap, args = parse_request(raw.strip(), self._registry)
        except RequestError as err:
            self._audit_rejection(run=run, peer=peer, capability=verb, args={}, reason=f"bad request: {err}")
            return {"ok": False, "error": f"bad request: {err}"}
        logged_args = redactor.redact_obj(args.to_json())

        def reject(reason: str) -> dict[str, Any]:
            self._audit_rejection(run=run, peer=peer, capability=cap.name, args=logged_args, reason=reason)
            return {"ok": False, "error": reason}

        if run is not None and cap.name not in run.allowed:
            hint = _PROFILE_HINTS.get(run.profile, "")
            return reject(f"{cap.verb} is not available in the {run.profile} profile{hint}")
        missing = unavailable(cap, self._env.options.available)
        if missing is not None:
            # Git proposals or a house overlay this install does not have (see `Capability.requires`).
            return reject(missing)
        if run is not None and self.on_request is not None:
            try:
                self.on_request(run, cap.name)
            except Exception:
                _LOGGER.exception("on_request hook failed")
        if run is None and not cap.read_only:
            return reject(f"{cap.verb} is only available inside an agent run")
        now = self._clock()
        day = self._day(now)
        ctx = ExecContext(env=self._env, capability=cap, run=run)
        try:
            if cap.validator is not None:
                args = resolve_ref(cap.validator)(ctx, args)
            origin = run.principal.origin if run else Origin.ADMIN
            verdict = self._decide(cap, ctx, args, origin)
        except CapabilityError as err:
            return reject(str(err)[:300])
        ctx.verdict = verdict
        runnable = verdict.execute or (verdict.report_only and cap.report_only_executor)
        if not runnable:
            reason = {
                "needs_approval": "this needs the admin's approval (Needs you on Home); tell the admin what to approve",
                "report_only": "observe mode: report it instead",
            }.get(verdict.outcome, "not allowed by policy")
            return reject(f"{cap.verb}: {reason} ({'; '.join(verdict.reasons)})"[:400])
        store = self._env.store
        if store is None and not cap.read_only:
            return {"ok": False, "error": "audit log unavailable; request refused"}
        try:
            quota = self._reserve_quota(cap, run, day)
        except (StoreError, sqlite3.Error, OSError) as err:
            self._refund(cap, run)
            _LOGGER.error("quota check failed; refusing request", extra={"error": str(err)[:200]})
            return {"ok": False, "error": "audit log unavailable; request refused"}
        if quota is not None:
            return reject(quota)
        audit_id: int | None = None
        if store is not None:
            try:
                audit_id = store.audit_begin(
                    at=now,
                    run_id=run.run_id if run else None,
                    principal=run.principal.label() if run else f"uid:{peer.uid}",
                    channel="opsctl",
                    capability=cap.name,
                    args=redactor.redact_obj(args.to_json()),
                    tier=str(verdict.tier),
                    decision=verdict.outcome,
                    notify_admin=verdict.notify_admin,
                )
            except (StoreError, sqlite3.Error, OSError) as err:
                self._refund(cap, run)
                _LOGGER.error("audit write failed; refusing request", extra={"error": str(err)[:200]})
                return {"ok": False, "error": "audit log unavailable; request refused"}
        response, outcome = await self._execute(cap, ctx, args)
        if not response.get("ok"):
            self._refund(cap, run)
            if outcome == "refused" and cap.refund_refused and cap.quota.per_day and store is not None:
                try:
                    store.unbump(day, f"cap:{cap.name}")
                except (StoreError, sqlite3.Error, OSError) as err:
                    _LOGGER.error("quota refund failed", extra={"error": str(err)[:200]})
        if store is not None and audit_id is not None:
            try:
                store.audit_finish(audit_id, outcome, redactor.redact(str(response.get("error", "")))[:300])
            except (StoreError, sqlite3.Error, OSError) as err:
                # The pending audit row already records the request; only its outcome is missing.
                _LOGGER.error("audit update failed", extra={"error": str(err)[:200], "audit_id": audit_id})
        _LOGGER.info("opsctl request", extra={"verb": cap.verb, "outcome": outcome})
        return response

    async def _execute(self, cap: Capability, ctx: ExecContext, args: Args) -> tuple[dict[str, Any], str]:
        redactor = self._env.redactor()
        executor = self._executor(cap.executor)
        try:
            if inspect.iscoroutinefunction(executor):
                result = await asyncio.wait_for(executor(ctx, args), REQUEST_TIMEOUT_S)
            else:
                await self._sem.acquire()
                # The slot is released only when the worker thread really
                # finishes, so timed-out queries cannot pile up threads.
                worker = asyncio.ensure_future(asyncio.to_thread(executor, ctx, args))
                worker.add_done_callback(lambda _f: self._sem.release())
                result = await asyncio.wait_for(asyncio.shield(worker), REQUEST_TIMEOUT_S)
        except TimeoutError:
            return {"ok": False, "error": "query timed out"}, "timeout"
        except CapabilityError as err:
            return {"ok": False, "error": str(err)[:300]}, "refused"
        except (ApiError, HistoryError, RuntimeError, OSError, ValueError) as err:
            return {"ok": False, "error": redactor.redact(str(err))[:500]}, "error"
        text = result if isinstance(result, str) else json.dumps(result, indent=1, default=str)
        if ctx.notes:
            text += "\nnote: " + "; ".join(ctx.notes)
        text = redactor.redact(text)
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + f"\n... truncated ({len(text)} chars); narrow the query"
        return {"ok": True, "output": text}, "ok"


def call(
    socket_path: Path, verb: str, args: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT_S + 10
) -> dict[str, Any]:
    """Client side: one request, one response."""
    payload = json.dumps({"verb": verb, "args": args}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(payload)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = json.loads(b"".join(chunks) or b"{}")
    return response if isinstance(response, dict) else {"ok": False, "error": "bad response"}
