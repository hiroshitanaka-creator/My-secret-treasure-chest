"""Budget Guard, retries and kill switch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from civlab.adapters.base import Usage
from civlab.budget import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
    KillSwitch,
    KillSwitchEngagedError,
    Pricing,
)
from civlab.config import ExperimentConfig
from civlab.runner import RunResult, SimulationRunner

from .conftest import PROVENANCE
from .helpers import REST, FakeAdapter, always_permanent, always_transient

PRICING = Pricing(1.0, 4.0, source="test fixture (not a real price)", checked_at="2026-10-29")


def cfg(**kw: object) -> ExperimentConfig:
    base: dict[str, object] = dict(experiment_id="budget-test", residents=6, turns=10, seed=7)
    base.update(kw)
    return ExperimentConfig(**base)  # type: ignore[arg-type]


def run(config, store, adapter, **kw) -> RunResult:
    return SimulationRunner(config, store, adapter=adapter, provenance=PROVENANCE, **kw).run()


def test_max_calls_stops_the_run_cleanly(store) -> None:
    result = run(cfg(budget=BudgetLimits(max_calls=12)), store, FakeAdapter())
    assert result.status == "stopped_budget"
    assert result.turns_completed == 2  # 6 residents x 2 turns = 12 calls
    assert result.budget["calls"] == 12
    assert store.get_run(result.run_id)["status"] == "stopped_budget"
    assert store.get_errors(result.run_id)[0]["kind"] == "budget"


def test_paid_calls_are_refused_by_default(store) -> None:
    adapter = FakeAdapter(paid=True)
    result = run(cfg(), store, adapter)
    assert result.status == "stopped_budget"
    assert adapter.calls == 0  # refused before any call is made
    assert result.budget["paid_calls"] == 0


def test_paid_calls_need_pricing(store) -> None:
    limits = BudgetLimits(
        max_paid_calls=10,
        max_input_tokens=10**6,
        max_output_tokens=10**6,
        max_estimated_cost_usd=1.0,
    )
    adapter = FakeAdapter(paid=True)
    result = run(cfg(budget=limits), store, adapter)
    assert result.status == "stopped_budget"
    assert "pricing" in (result.error or "")
    assert adapter.calls == 0


def test_paid_calls_are_counted_and_estimated_not_actual(store) -> None:
    limits = BudgetLimits(
        max_paid_calls=100,
        max_input_tokens=10**6,
        max_output_tokens=10**6,
        max_estimated_cost_usd=1.0,
    )
    adapter = FakeAdapter(paid=True, usage=Usage(1000, 100))
    result = run(cfg(turns=2, budget=limits, pricing=PRICING), store, adapter)
    assert result.status == "completed"
    assert result.budget["paid_calls"] == 12
    # 12 calls x (1000 * 1.0 + 100 * 4.0) / 1e6
    assert result.budget["estimated_cost_usd"] == pytest.approx(12 * 0.0014)
    assert result.budget["actual_cost_usd"] is None
    calls = store.get_api_calls(result.run_id)
    assert all(c["actual_cost_usd"] is None for c in calls)
    assert sum(c["estimated_cost_usd"] for c in calls) == pytest.approx(12 * 0.0014)


def test_cost_limit_stops_before_exceeding(store) -> None:
    # worst case per call is checked before the call: est input + max_output_tokens
    limits = BudgetLimits(
        max_paid_calls=100,
        max_input_tokens=10**6,
        max_output_tokens=10**6,
        max_estimated_cost_usd=0.01,
    )
    adapter = FakeAdapter(paid=True, usage=Usage(1000, 100), max_output_tokens=100)
    result = run(cfg(budget=limits, pricing=PRICING), store, adapter)
    assert result.status == "stopped_budget"
    assert result.budget["estimated_cost_usd"] <= 0.01


def test_token_limit_is_enforced(store) -> None:
    limits = BudgetLimits(
        max_paid_calls=100,
        max_input_tokens=100,
        max_output_tokens=10**6,
        max_estimated_cost_usd=1.0,
    )
    adapter = FakeAdapter(paid=True)
    result = run(cfg(budget=limits, pricing=PRICING), store, adapter)
    assert result.status == "stopped_budget"
    assert "max_input_tokens" in (result.error or "")
    assert adapter.calls == 0


def test_transient_errors_are_retried_a_bounded_number_of_times(store) -> None:
    adapter = FakeAdapter(always_transient)
    result = run(cfg(turns=1, budget=BudgetLimits(max_retries=2)), store, adapter)
    assert result.status == "completed"
    assert adapter.calls == 6 * 3  # 1 try + 2 retries per resident
    actions = store.get_actions(result.run_id)
    assert {a["decision_status"] for a in actions} == {"adapter_error"}
    calls = store.get_api_calls(result.run_id)
    assert {c["outcome"] for c in calls} == {"transient_error"}
    assert max(c["attempt"] for c in calls) == 3


def test_transient_error_then_success(store) -> None:
    def flaky(req, n):
        if n % 2 == 1:
            from civlab.adapters.base import TransientAdapterError

            raise TransientAdapterError("blip")
        return REST

    adapter = FakeAdapter(flaky)
    result = run(cfg(turns=1), store, adapter)
    assert {a["decision_status"] for a in store.get_actions(result.run_id)} == {"valid"}
    assert result.budget["calls"] == 12


def test_permanent_errors_are_not_retried(store) -> None:
    adapter = FakeAdapter(always_permanent)
    run(cfg(turns=1), store, adapter)
    assert adapter.calls == 6


def test_kill_switch_file_prevents_any_call(store, tmp_path: Path) -> None:
    stop = tmp_path / "STOP"
    stop.write_text("stop")
    adapter = FakeAdapter()
    result = run(cfg(), store, adapter, kill_switch=KillSwitch(stop))
    assert result.status == "stopped_kill_switch"
    assert adapter.calls == 0
    assert result.turns_completed == 0


def test_kill_switch_env(store, monkeypatch) -> None:
    monkeypatch.setenv("CIVLAB_KILL_SWITCH", "1")
    adapter = FakeAdapter()
    assert run(cfg(), store, adapter).status == "stopped_kill_switch"
    assert adapter.calls == 0


def test_kill_switch_engaged_mid_run(store, tmp_path: Path) -> None:
    stop = tmp_path / "STOP"

    def behaviour(req, n):
        if req.turn == 2 and req.resident_id == "r03":
            stop.write_text("stop")
        return REST

    adapter = FakeAdapter(behaviour)
    result = run(cfg(), store, adapter, kill_switch=KillSwitch(stop))
    assert result.status == "stopped_kill_switch"
    assert result.turns_completed == 2
    # calls in the unfinished turn are still accounted for
    budget = json.loads(store.get_run(result.run_id)["budget_json"])
    assert len(budget["unfinished_turn_calls"]) == 3
    assert budget["calls"] == 2 * 6 + 3


def test_guard_unit_behaviour() -> None:
    g = BudgetGuard(BudgetLimits(max_calls=1))
    g.before_call(paid=False)
    g.after_call(paid=False, input_tokens=0, output_tokens=0)
    with pytest.raises(BudgetExceededError):
        g.before_call(paid=False)


def test_kill_switch_unit(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "STOP")
    ks.check()
    (tmp_path / "STOP").touch()
    with pytest.raises(KillSwitchEngagedError):
        ks.check()


@pytest.mark.parametrize(
    "kw",
    [
        {"max_calls": -1},
        {"max_retries": 6},
        {"max_estimated_cost_usd": float("inf")},
        {"max_paid_calls": True},
    ],
)
def test_invalid_limits_are_rejected(kw) -> None:
    with pytest.raises(ValueError):
        BudgetLimits(**kw)


def test_pricing_requires_source_and_date() -> None:
    assert not Pricing(1.0, 1.0).is_configured
    assert Pricing(1.0, 1.0).estimate(10, 10) is None
    assert PRICING.is_configured
    with pytest.raises(ValueError):
        Pricing(-1.0, 1.0, "x", "y")


def test_zero_price_does_not_count_as_configured() -> None:
    # A zero price would make every cost estimate 0 and disable the cost cap.
    assert not Pricing(0.0, 0.0, "x", "2026-10-29").is_configured
    assert not Pricing(1.0, 0.0, "x", "2026-10-29").is_configured


def test_unexpected_adapter_failure_is_still_counted(store) -> None:
    def broken(req, n):
        raise RuntimeError("bug in adapter post-processing")

    limits = BudgetLimits(
        max_paid_calls=10,
        max_input_tokens=10**6,
        max_output_tokens=10**6,
        max_estimated_cost_usd=1.0,
    )
    adapter = FakeAdapter(broken, paid=True)
    result = run(cfg(budget=limits, pricing=PRICING), store, adapter)
    assert result.status == "failed"
    assert result.budget["paid_calls"] == 1
    assert result.budget["estimated_cost_usd"] > 0
    budget = json.loads(store.get_run(result.run_id)["budget_json"])
    assert [c["outcome"] for c in budget["unfinished_turn_calls"]] == ["unexpected_error"]
