"""Budget Guard: limits on model calls, tokens, estimated cost, and a kill switch.

Every adapter call (paid or not) passes through ``BudgetGuard.before_call`` and
``BudgetGuard.after_call``. Paid calls are refused unless a paid-call allowance,
token limits and user-supplied pricing are all configured.

Cost figures here are *estimates* computed from token counts and the pricing
given in the experiment config. Actual billed cost is not available from the
API response and is recorded as unknown (``None``).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KILL_SWITCH_ENV = "CIVLAB_KILL_SWITCH"


class BudgetExceededError(RuntimeError):
    """A configured limit would be (or has been) exceeded."""


class KillSwitchEngagedError(RuntimeError):
    """The operator requested an immediate stop."""


@dataclass(frozen=True)
class BudgetLimits:
    max_calls: int = 200
    max_paid_calls: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_estimated_cost_usd: float = 0.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        for name in ("max_calls", "max_paid_calls", "max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        if not math.isfinite(self.max_estimated_cost_usd) or self.max_estimated_cost_usd < 0:
            raise ValueError("max_estimated_cost_usd must be a finite non-negative number")


@dataclass(frozen=True)
class Pricing:
    """USD per one million tokens. Must be copied from the provider's official
    pricing page by a human at experiment time; never guessed in code."""

    input_per_million_usd: float | None = None
    output_per_million_usd: float | None = None
    source: str = ""
    checked_at: str = ""

    def __post_init__(self) -> None:
        for name in ("input_per_million_usd", "output_per_million_usd"):
            v = getattr(self, name)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not isinstance(self.source, str) or not isinstance(self.checked_at, str):
            raise ValueError("pricing source and checked_at must be strings")

    @property
    def is_configured(self) -> bool:
        return (
            self.input_per_million_usd is not None
            and self.output_per_million_usd is not None
            # A zero price would disable the cost cap, so it does not count as configured.
            and self.input_per_million_usd > 0
            and self.output_per_million_usd > 0
            and bool(self.source)
            and bool(self.checked_at)
        )

    def estimate(self, input_tokens: int, output_tokens: int) -> float | None:
        inp, out = self.input_per_million_usd, self.output_per_million_usd
        if not self.is_configured or inp is None or out is None:
            return None
        return (input_tokens * inp + output_tokens * out) / 1_000_000


class KillSwitch:
    """Stop requested when the env var is truthy or the stop file exists."""

    def __init__(self, stop_file: Path | None = None) -> None:
        self.stop_file = stop_file

    def engaged(self) -> bool:
        if os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return self.stop_file is not None and self.stop_file.exists()

    def check(self) -> None:
        if self.engaged():
            raise KillSwitchEngagedError("kill switch engaged")


@dataclass
class _Totals:
    calls: int = 0
    paid_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class BudgetGuard:
    def __init__(
        self,
        limits: BudgetLimits,
        pricing: Pricing | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.limits = limits
        self.pricing = pricing or Pricing()
        self.kill_switch = kill_switch or KillSwitch()
        self.totals = _Totals()

    def before_call(
        self, *, paid: bool, est_input_tokens: int = 0, max_output_tokens: int = 0
    ) -> None:
        """Raise if this call must not be made. Counts nothing."""
        self.kill_switch.check()
        t, lim = self.totals, self.limits
        if t.calls + 1 > lim.max_calls:
            raise BudgetExceededError("max_calls reached")
        if not paid:
            return
        if t.paid_calls + 1 > lim.max_paid_calls:
            raise BudgetExceededError("paid calls not allowed or max_paid_calls reached")
        if not self.pricing.is_configured:
            raise BudgetExceededError("pricing not configured; paid calls refused")
        if t.input_tokens + est_input_tokens > lim.max_input_tokens:
            raise BudgetExceededError("max_input_tokens would be exceeded")
        if t.output_tokens + max_output_tokens > lim.max_output_tokens:
            raise BudgetExceededError("max_output_tokens would be exceeded")
        worst = self.pricing.estimate(est_input_tokens, max_output_tokens) or 0.0
        if t.estimated_cost_usd + worst > lim.max_estimated_cost_usd:
            raise BudgetExceededError("max_estimated_cost_usd would be exceeded")

    def after_call(self, *, paid: bool, input_tokens: int, output_tokens: int) -> float | None:
        """Record a completed (or failed-but-sent) call. Returns its estimated cost."""
        t = self.totals
        t.calls += 1
        t.input_tokens += input_tokens
        t.output_tokens += output_tokens
        if not paid:
            return 0.0
        t.paid_calls += 1
        cost = self.pricing.estimate(input_tokens, output_tokens)
        if cost is not None:
            t.estimated_cost_usd += cost
        return cost

    def snapshot(self) -> dict[str, Any]:
        t = self.totals
        return {
            "calls": t.calls,
            "paid_calls": t.paid_calls,
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "estimated_cost_usd": round(t.estimated_cost_usd, 8),
            "actual_cost_usd": None,
            "actual_cost_source": "unavailable",
            "limits": {
                "max_calls": self.limits.max_calls,
                "max_paid_calls": self.limits.max_paid_calls,
                "max_input_tokens": self.limits.max_input_tokens,
                "max_output_tokens": self.limits.max_output_tokens,
                "max_estimated_cost_usd": self.limits.max_estimated_cost_usd,
                "max_retries": self.limits.max_retries,
            },
            "pricing_configured": self.pricing.is_configured,
        }
