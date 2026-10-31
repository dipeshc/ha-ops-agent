"""`ha.history` (`opsctl history`): one entity's past states, read-only.

- `raw`: state changes over the window (`history/history_during_period`,
  no attributes), at most `MAX_POINTS`; longer series are thinned evenly,
  always keeping the first and last change.
- `hourly` / `daily`: mean, min and max per period. Long-term statistics
  (`recorder/statistics_during_period`) when the entity has them, else the
  same aggregates computed from the state history (sample mean). Binary
  states count as 1/0, so the mean is the fraction of samples that were on.

Privacy rules (refused domains, id patterns and phone sensors; state, unit
and friendly name only) are in `privacy.py`. Raw mode covers at most 7
days. The websocket reply is capped at 8 MiB and the output is bounded well
under the RPC output limit (`bounded` thins further if a series of long
states still does not fit). Only available inside an agent run.
"""

import asyncio
import json
import math
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from opsd.api import LiveAPI
from opsd.capabilities import Args, CapabilityError
from opsd.privacy import MAX_STATE_CHARS, describe, refusal
from opsd.runctx import ExecContext
from opsd.supervisor import ApiError
from opsd.ws import CommandError

ENTITY_RE = re.compile(r"[a-z_][a-z0-9_]{0,63}\.[a-z0-9_]{1,128}")
WINDOW_RE = re.compile(r"([1-9][0-9]{0,2})([hd])")
MAX_WINDOW = timedelta(days=31)
MIN_WINDOW = timedelta(hours=1)
MAX_POINTS = 500
MAX_JSON_CHARS = 30_000
# Raw state changes beyond a week are a lot of data; hourly/daily summarise longer windows.
MAX_RAW_WINDOW = timedelta(days=7)
# History replies are small after filtering; refuse anything huge at the websocket instead of buffering it.
MAX_WS_BYTES = 8 * 1024 * 1024
_BINARY = {"on": 1.0, "off": 0.0, "true": 1.0, "false": 0.0, "open": 1.0, "closed": 0.0}
_PERIODS = {"hourly": ("hour", timedelta(hours=1)), "daily": ("day", timedelta(days=1))}


def parse_window(value: str) -> timedelta:
    match = WINDOW_RE.fullmatch(value)
    if match is None:
        raise CapabilityError("window must look like 6h or 7d")
    amount = int(match.group(1))
    window = timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount)
    if not MIN_WINDOW <= window <= MAX_WINDOW:
        raise CapabilityError("window must be between 1h and 31d")
    return window


def validate_history(ctx: ExecContext, args: Args) -> Args:
    if ctx.run is None:
        raise CapabilityError("history is only available inside an agent run")
    entity = args.opt_text("entity")
    if not entity or not ENTITY_RE.fullmatch(entity):
        raise CapabilityError("entity is required, e.g. sensor.outdoor_temperature")
    window = parse_window(args.text("window"))
    mode = args.text("mode")
    if mode == "raw" and window > MAX_RAW_WINDOW:
        raise CapabilityError("raw is limited to 7d; use --mode hourly or daily for longer windows")
    if mode in _PERIODS and window // _PERIODS[mode][1] > MAX_POINTS:
        raise CapabilityError(f"{mode} over {args.text('window')} is more than {MAX_POINTS} points; use daily")
    return args


def _number(state: object) -> float | None:
    if not isinstance(state, str):
        return None
    lowered = state.strip().lower()
    if lowered in _BINARY:
        return _BINARY[lowered]
    try:
        value = float(lowered)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _round(value: float) -> float:
    return float(f"{value:.4g}")


def _when(epoch: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(epoch, UTC).astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def _epoch(value: object) -> float | None:
    """HA sends compressed times as epoch seconds (or ms for statistics), or ISO strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def thin(points: list[Any], limit: int = MAX_POINTS) -> list[Any]:
    """Evenly spaced subset of at most `limit` points, keeping the first and last."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def changes(raw: Any, entity: str) -> list[tuple[float, str]]:
    rows = raw.get(entity) if isinstance(raw, dict) else None
    out: list[tuple[float, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        at = _epoch(row.get("lu", row.get("last_updated", row.get("last_changed"))))
        state = row.get("s", row.get("state"))
        if at is not None and isinstance(state, str):
            out.append((at, state[:MAX_STATE_CHARS]))
    return sorted(out)


def aggregate(series: list[tuple[float, str]], start: float, end: float, period: timedelta) -> list[dict[str, Any]]:
    """Mean/min/max per period from state samples (numeric or binary states only)."""
    step = period.total_seconds()
    buckets: dict[int, list[float]] = {}
    for at, state in series:
        value = _number(state)
        if value is None or not start <= at < end:
            continue
        buckets.setdefault(int((at - start) // step), []).append(value)
    return [
        {"start": start + index * step, "mean": sum(v) / len(v), "min": min(v), "max": max(v), "n": len(v)}
        for index, v in sorted(buckets.items())
    ]


def statistics_rows(raw: Any, entity: str) -> list[dict[str, Any]]:
    rows = raw.get(entity) if isinstance(raw, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        start = _epoch(row.get("start"))
        values = {k: row.get(k) for k in ("mean", "min", "max")}
        if start is None or not any(isinstance(v, (int, float)) for v in values.values()):
            continue
        out.append({"start": start, **{k: v for k, v in values.items() if isinstance(v, (int, float))}})
    return out


def bounded(result: dict[str, Any], key: str) -> dict[str, Any]:
    """Thin the series until the JSON fits `MAX_JSON_CHARS`."""
    while len(json.dumps(result)) > MAX_JSON_CHARS and len(result[key]) > 2:
        result[key] = thin(result[key], max(2, len(result[key]) // 2))
        result["thinned"] = True
    return result


def cmd_history(ctx: ExecContext, args: Args) -> Any:
    entity = args.text("entity")
    window = parse_window(args.text("window"))
    mode = args.text("mode")
    tz = ctx.env.tz()
    end = datetime.now(UTC)
    start = end - window
    try:
        current = ctx.client.core(f"/states/{entity}")
    except ApiError as err:
        if err.status != 404:
            raise
        current = None

    async def fetch() -> tuple[Any, Any, str | None]:
        async with LiveAPI(ctx.client, max_bytes=MAX_WS_BYTES) as api:
            try:
                entry = await api.ws("config/entity_registry/get", entity_id=entity)
            except CommandError:
                entry = None
            withheld = refusal(entity, entry if isinstance(entry, dict) else None)
            if withheld is not None:
                return None, None, withheld
            stats = None
            if mode in _PERIODS:
                stats = await api.ws("recorder/statistics_during_period", start_time=start.isoformat(),
                                     end_time=end.isoformat(), statistic_ids=[entity], period=_PERIODS[mode][0],
                                     types=["mean", "min", "max"])  # fmt: skip
                if statistics_rows(stats, entity):
                    return stats, None, None
            history = await api.ws("history/history_during_period", start_time=start.isoformat(),
                                   end_time=end.isoformat(), entity_ids=[entity], minimal_response=True,
                                   no_attributes=True, include_start_time_state=True)  # fmt: skip
            return stats, history, None

    withheld = refusal(entity, None)
    if withheld is not None:
        return {"error": withheld}
    stats, history, withheld = asyncio.run(fetch())
    if withheld is not None:
        return {"error": withheld}
    series = changes(history, entity) if history is not None else []
    if current is None and not series and not statistics_rows(stats, entity):
        return {"error": f"{entity} not found (no current state and no history in the window)"}
    result: dict[str, Any] = {**describe(current, entity), "window": args.text("window"), "mode": mode}
    if mode == "raw":
        result["total_changes"] = len(series)
        points = [[_when(at, tz), state] for at, state in series]
        result["points"] = thin(points)
        result["thinned"] = len(points) > MAX_POINTS
        return bounded(result, "points")
    rows = statistics_rows(stats, entity) if history is None else []
    result["source"] = "statistics" if rows else "history (sample mean)"
    if not rows:
        rows = aggregate(series, start.timestamp(), end.timestamp(), _PERIODS[mode][1])
        if not rows and series:
            return {**result, "error": "the state is not numeric or binary; use mode raw"}
    result["periods"] = [
        {"start": _when(float(r["start"]), tz), **{k: _round(float(r[k])) for k in ("mean", "min", "max") if k in r},
         **({"n": r["n"]} if "n" in r else {})}
        for r in rows[-MAX_POINTS:]
    ]  # fmt: skip
    return bounded(result, "periods")
