"""The one budget for AI runs.

What bounds use deterministically: a daily run cap per profile, each run's `--max-turns`, its cost cap
(`--max-budget-usd`), its timeout (the run time it reserved), and one rolling run-time budget:

- `B = plan_hours x 3600 x usage.max_share_percent / 100` seconds of AI run time per 7 days, and `B / 5`
  per 5 hours (`claude.plan`; `auto` is pro, the smallest). It is an honest wall-clock budget of the
  agent's own runs, not a measured share of the subscription: the provider reports account utilisation
  in whole percents, too coarse to see one run.
- Runs stop at 80 % of each window's budget; user-class runs (chat, investigations, summaries) at half
  of it, so monitoring keeps a reserve. A run's timeout is trimmed to the budget left.
- The account-busy yield: a measured account utilisation of at least 90 % in either window, less than an
  hour old, stops every run (the admin's own work comes first).
- `limits.daily_max_usd` (optional): the reported cost of today's runs (`RunResult.cost_usd`) stays under it;
  each run's cost cap is trimmed to what is left.

With a provider API key there are no subscription windows (`Caps.windows` false): neither the run-time
budget nor the account-busy yield applies, while the daily run caps, `--max-turns`, the run timeout, the
per-run cost cap and the daily cost cap still do.

A run reserves its run time in one store transaction before the AI starts; the reservation is replaced by
the actual figures when it ends. The kill switch and the usage-limit backoff are in `aicontrol`.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from math import inf

from opsd.config import Options

WINDOW = timedelta(hours=5)
RUNS_PER_DAY = {"triage": 6, "chat": 20, "followup": 3, "investigate": 6, "summary": 6, "update_review": 2}
DEFAULT_PROFILE_RUNS = 3

SHARE_LIMIT = 0.05
SHARE_RANGE = (0.5, 25.0)
# Every run stops at this fraction of the run-time budget.
STOP_FRACTION = 0.8
# User-class runs (chat, investigations, summaries) stop at this fraction of the budget, keeping the rest
# for monitoring runs (triage, follow-ups, update reviews).
USER_FRACTION = 0.5
ACCOUNT_BUSY = 0.90
# Low end of the published weekly allowances (hours of Sonnet use), per plan.
# Estimates: Anthropic publishes ranges, not exact limits.
PLAN_WEEKLY_HOURS = {"pro": 40.0, "max5": 140.0, "max20": 240.0}
# The 5-hour budget is a fifth of the 7-day one.
WINDOW_FRACTION_OF_WEEK = 0.2
WINDOWS = ("five_hour", "seven_day")
# A daily cost cap leaves a run less than this: it is refused (`cost_cap:day`).
MIN_RUN_USD = 0.05
RESERVE_SECONDS = {"triage": 600.0, "chat": 180.0, "followup": 180.0, "investigate": 420.0, "summary": 90.0}
DEFAULT_RESERVE_SECONDS = 180.0
MIN_RUN_SECONDS = 60.0


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
    # False with an API key: no subscription windows, so the run-time budget and the account yield are unbounded.
    windows: bool = True
    # `limits.daily_max_usd`; None is no daily cost cap.
    daily_usd: float | None = None

    @property
    def account_busy(self) -> float:
        """The account utilisation at which the agent yields to the admin's own use."""
        return ACCOUNT_BUSY if self.windows else inf

    @property
    def week_seconds(self) -> float:
        """B: run time the agent may use per 7 days, `plan hours x 3600 x max_share_percent / 100`."""
        if not self.windows:
            return inf
        return PLAN_WEEKLY_HOURS.get(self.plan, PLAN_WEEKLY_HOURS["pro"]) * 3600 * self.share_limit

    @property
    def window_seconds(self) -> float:
        """B / 5: run time the agent may use per 5 hours."""
        return self.week_seconds * WINDOW_FRACTION_OF_WEEK

    def budget(self, window: str) -> float:
        return self.window_seconds if window == "five_hour" else self.week_seconds

    def stop(self, window: str, *, user: bool = False) -> float:
        """Run seconds in `window` at which runs stop (user-class runs stop earlier)."""
        return self.budget(window) * (USER_FRACTION if user else STOP_FRACTION)

    def seconds_for(self, profile: str) -> float:
        return self.reserve_seconds.get(profile, DEFAULT_RESERVE_SECONDS)

    def runs_for(self, profile: str) -> int:
        return self.profile_runs.get(profile, DEFAULT_PROFILE_RUNS)

    @classmethod
    def from_options(cls, options: Options, *, windows: bool = True) -> Caps:
        """`windows` is false when the credential is an API key (no subscription windows)."""
        return cls(
            plan=resolve_plan(options.claude.plan),
            share_limit=share_fraction(options.usage.max_share_percent),
            windows=windows,
            daily_usd=options.limits.daily_max_usd or None,
        )


def share_fraction(percent: float) -> float:
    """`max_share_percent` as a fraction, held inside the allowed range."""
    low, high = SHARE_RANGE
    return max(low, min(high, percent)) / 100


def resolve_plan(option: str) -> str:
    """The option if it names a tier; `auto` is `pro`, the most conservative (the provider reports no tier)."""
    return option if option in PLAN_WEEKLY_HOURS else "pro"


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
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    # A percent (the provider reports whole percents, 0..100). Over 100 is a full window (capped, never
    # dropped: dropping it would read a saturated account as no reading); negative or not finite is no reading.
    if not math.isfinite(value) or value < 0:
        return None
    return min(float(value), 100.0)


def utilisation(info: Mapping[str, object] | None, window: str) -> float | None:
    """Account utilisation (0..1) of `five_hour` or `seven_day` from a `rate_limit_info`.

    The provider reports the utilisation in percent (whole percents in practice), so 1 is 1 %, never the
    whole window. The payload carries no unit to say otherwise, so every value is read as a percent; a value
    over 100 counts as 100, and a negative or non-finite one is no reading.
    """
    value = _raw_utilisation(info, window)
    return None if value is None else value / 100
