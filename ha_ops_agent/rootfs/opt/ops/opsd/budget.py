"""The one budget for AI runs.

What bounds use deterministically: a daily run cap per profile, each run's
`--max-turns`, and its timeout (the run time it reserved). On top of that,
the agent's share of each subscription window (5-hour, 7-day) stays under
`usage.max_share_percent` (default 5%):
- measured: Claude Code reports account utilisation per window in
  `rate_limit_event` messages. The change during a run is the agent's share
  of it; runs stop once the rolling share would pass 80% of the limit, and
  when the account itself is above 90% of a window (the admin's own work
  comes first);
- fallback: utilisation is reported in whole percents, so most runs move it
  by nothing measurable. Such a run is charged its run time as a share of
  the plan's weekly allowance (`claude.plan`; auto uses the tier Claude Code
  reports, else pro, the smallest), and run time per window is capped at
  the same share.

A run reserves its share in one store transaction before the AI starts; the
reservation is replaced by the actual figures when it ends. The kill switch
and the usage-limit backoff are in `aicontrol`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from opsd.config import Options

WINDOW = timedelta(hours=5)
RUNS_PER_DAY = {"triage": 6, "chat": 20, "followup": 3, "investigate": 6, "summary": 6, "update_review": 2}
DEFAULT_PROFILE_RUNS = 3

# Runs stop at 80% of the share limit, a margin for measurement lag.
SHARE_LIMIT = 0.05
SHARE_MARGIN = 0.8
SHARE_RANGE = (0.5, 25.0)
ACCOUNT_BUSY = 0.90
# Chat (and its investigations and summaries) stops at this fraction of the share stop
# in either window, keeping the rest for monitoring runs (triage, update reviews).
USER_SHARE_RESERVE = 0.5
DEFAULT_RUN_SHARE = 0.005
# Low end of the published weekly allowances (hours of Sonnet use), per plan.
# Estimates: Anthropic publishes ranges, not exact limits.
PLAN_WEEKLY_HOURS = {"pro": 40.0, "max5": 140.0, "max20": 240.0}
# Assume at most a fifth of the weekly allowance fits in one 5-hour window.
WINDOW_FRACTION_OF_WEEK = 0.2
RESERVE_SECONDS = {"triage": 600.0, "chat": 180.0, "followup": 180.0, "investigate": 420.0, "summary": 90.0}
DEFAULT_RESERVE_SECONDS = 180.0
MIN_RUN_SECONDS = 60.0


def _whole_window_seconds(plan: str, window: str) -> float:
    week = PLAN_WEEKLY_HOURS.get(plan, PLAN_WEEKLY_HOURS["pro"]) * 3600
    return week * WINDOW_FRACTION_OF_WEEK if window == "five_hour" else week


@dataclass(frozen=True, slots=True)
class Period:
    day: str
    week: str  # Monday of the week (local), YYYY-MM-DD
    week_end: str
    month: str

    @classmethod
    def at(cls, now: datetime, tz: tzinfo) -> Period:
        local: date = now.astimezone(tz).date()
        monday = local - timedelta(days=local.weekday())
        return cls(
            local.isoformat(), monday.isoformat(), (monday + timedelta(days=6)).isoformat(), local.isoformat()[:7]
        )


@dataclass(frozen=True, slots=True)
class Caps:
    plan: str = "pro"
    share_limit: float = SHARE_LIMIT
    profile_runs: Mapping[str, int] = field(default_factory=lambda: dict(RUNS_PER_DAY))
    reserve_seconds: Mapping[str, float] = field(default_factory=lambda: dict(RESERVE_SECONDS))

    @property
    def share_stop(self) -> float:
        return self.share_limit * SHARE_MARGIN

    @property
    def week_seconds(self) -> float:
        """Run time the agent may use per 7 days: its share of the plan's weekly allowance (estimate)."""
        return PLAN_WEEKLY_HOURS.get(self.plan, PLAN_WEEKLY_HOURS["pro"]) * 3600 * self.share_limit

    @property
    def window_seconds(self) -> float:
        return self.week_seconds * WINDOW_FRACTION_OF_WEEK

    def estimated_share(self, seconds: float, window: str) -> float:
        """A run's share of a whole subscription window from its run time (the fallback, an estimate)."""
        return max(0.0, seconds) / _whole_window_seconds(self.plan, window)

    def seconds_for(self, profile: str) -> float:
        return self.reserve_seconds.get(profile, DEFAULT_RESERVE_SECONDS)

    def runs_for(self, profile: str) -> int:
        return self.profile_runs.get(profile, DEFAULT_PROFILE_RUNS)

    @classmethod
    def from_options(cls, options: Options, *, detected_plan: str | None = None) -> Caps:
        return cls(
            plan=resolve_plan(options.claude.plan, detected_plan),
            share_limit=share_fraction(options.usage.max_share_percent),
        )


def share_fraction(percent: float) -> float:
    """`max_share_percent` as a fraction, held inside the allowed range."""
    low, high = SHARE_RANGE
    return max(low, min(high, percent)) / 100


def resolve_plan(option: str, detected: str | None) -> str:
    """The option if set; `auto` uses a detected tier, else `pro` (the most conservative)."""
    if option in PLAN_WEEKLY_HOURS:
        return option
    return detected if detected in PLAN_WEEKLY_HOURS else "pro"


@dataclass(frozen=True, slots=True)
class Reservation:
    turns: int
    budget_usd: float
    seconds: float = 1200.0


def _raw_utilisation(info: Mapping[str, object] | None, window: str) -> float | None:
    if not isinstance(info, Mapping):
        return None
    value: object = None
    windows = info.get("unifiedWindows")
    if isinstance(windows, Mapping) and isinstance(windows.get(window), Mapping):
        value = windows[window].get("utilization")
    elif info.get("rateLimitType") == window:
        value = info.get("utilization")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return float(value)


def utilisation(info: Mapping[str, object] | None, window: str) -> float | None:
    """Account utilisation (0..1) of `five_hour` or `seven_day` from a `rate_limit_info`.

    The unit is not documented. A value above 1 can only be a percentage; a
    value of 1 or less is read as a fraction, the reading that records more
    usage (failing toward the agent using less).
    """
    value = _raw_utilisation(info, window)
    if value is None:
        return None
    return value / 100 if value > 1.0 else value


def run_share(first: Mapping[str, object] | None, last: Mapping[str, object] | None, window: str) -> float | None:
    """The window utilisation added while the run was going (None if not reported twice).

    One unit for the whole run, chosen from both readings: percent only if
    either is above 1, otherwise fraction (the larger, conservative reading).
    """
    start, end = _raw_utilisation(first, window), _raw_utilisation(last, window)
    if start is None or end is None:
        return None
    scale = 0.01 if max(start, end) > 1.0 else 1.0
    return max(0.0, end - start) * scale


def share_inconclusive(first: Mapping[str, object] | None, last: Mapping[str, object] | None, window: str) -> bool:
    """Both readings exist but did not move (below the whole-percent resolution): use the fallback."""
    start, end = _raw_utilisation(first, window), _raw_utilisation(last, window)
    return start is not None and end is not None and end <= start
