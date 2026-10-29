"""Evaluation: replay verification, run summaries and run comparison."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from civlab.actions import Action
from civlab.config import config_from_dict
from civlab.store import ExperimentStore
from civlab.world import WorldEngine, WorldState, state_hash


def verify_run(store: ExperimentStore, run_id: str) -> dict[str, Any]:
    """Re-derive every stored state from the initial state and stored actions.

    Checks (deterministic, class A, independent of how decisions were made):
    - each stored state's hash matches its content;
    - replaying the stored actions through the World Engine reproduces each
      stored state hash and each stored outcome;
    - run metadata (initial/final hash, turns_completed) is consistent.
    """
    run = store.get_run(run_id)
    config = config_from_dict(json.loads(run["config_json"]))
    engine = WorldEngine(config.rules)
    turns = store.get_turns(run_id)
    actions_by_turn: dict[int, list[dict[str, Any]]] = {}
    for row in store.get_actions(run_id):
        actions_by_turn.setdefault(row["turn"], []).append(row)

    problems: list[str] = []
    if not turns or turns[0]["turn"] != 0:
        return {
            "run_id": run_id,
            "ok": False,
            "turns_checked": 0,
            "problems": ["initial state (turn 0) missing"],
        }

    states: dict[int, WorldState] = {}
    for row in turns:
        st = WorldState.from_dict(json.loads(row["state_json"]))
        if state_hash(st) != row["state_hash"]:
            problems.append(f"turn {row['turn']}: stored state does not match stored hash")
        if st.turn != row["turn"]:
            problems.append(f"turn {row['turn']}: state.turn field is {st.turn}")
        states[row["turn"]] = st

    if turns[0]["state_hash"] != run["initial_state_hash"]:
        problems.append("initial state hash differs from run metadata")

    state = states[0]
    expected_turns = [t["turn"] for t in turns]
    if expected_turns != list(range(len(turns))):
        problems.append(f"turn sequence has gaps: {expected_turns}")

    for t in range(1, len(turns)):
        rows = actions_by_turn.get(t, [])
        actions = {
            r["resident_id"]: (
                None if r["action_json"] is None else Action.from_dict(json.loads(r["action_json"]))
            )
            for r in rows
            if r["decision_status"] != "not_asked"
        }
        result = engine.step(state, actions)
        replay_hash = state_hash(result.state)
        if replay_hash != turns[t]["state_hash"]:
            problems.append(f"turn {t}: replayed state hash differs from stored hash")
        for r in rows:
            outcome = result.outcomes.get(r["resident_id"])
            if outcome is None or (outcome.status, outcome.reason) != (
                r["outcome_status"],
                r["outcome_reason"],
            ):
                problems.append(f"turn {t}: outcome mismatch for {r['resident_id']}")
        state = result.state

    last = turns[-1]
    if run["turns_completed"] != last["turn"]:
        problems.append("turns_completed differs from last stored turn")
    if run["final_state_hash"] is not None and run["final_state_hash"] != last["state_hash"]:
        problems.append("final_state_hash differs from last stored state")

    return {
        "run_id": run_id,
        "ok": not problems,
        "turns_checked": len(turns) - 1,
        "final_state_hash": last["state_hash"],
        "problems": problems,
    }


def summarize_run(store: ExperimentStore, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    turns = store.get_turns(run_id)
    final = WorldState.from_dict(json.loads(turns[-1]["state_json"]))
    actions = store.get_actions(run_id)
    calls = store.get_api_calls(run_id)
    budget = json.loads(run["budget_json"]) if run["budget_json"] else None

    paid_calls = [c for c in calls if c["paid"]]
    unpriced_paid = any(c["estimated_cost_usd"] is None for c in paid_calls)
    deaths = sorted(rid for rid, r in final.residents.items() if not r.alive)
    return {
        "run_id": run_id,
        "experiment_id": run["experiment_id"],
        "status": run["status"],
        "adapter": run["adapter"],
        "model": run["model"],
        "prompt_version": run["prompt_version"],
        "reproducibility_class": run["reproducibility_class"],
        "scenario_hash": run["scenario_hash"],
        "seed": run["seed"],
        "turns_completed": run["turns_completed"],
        "final_state_hash": turns[-1]["state_hash"],
        "metrics": {
            "residents": len(final.residents),
            "survivors": len(final.residents) - len(deaths),
            "food_held": sum(r.food for r in final.residents.values()),
            "water_held": sum(r.water for r in final.residents.values()),
            "mean_health_x100": (
                sum(r.health for r in final.residents.values()) * 100 // len(final.residents)
            ),
            "pool_food": final.pools["food"],
            "pool_water": final.pools["water"],
            "shares_applied": sum(
                1
                for a in actions
                if a["outcome_reason"] == "shared" and a["outcome_status"] == "applied"
            ),
            "decisions_rejected_format": sum(
                1 for a in actions if a["decision_status"] == "rejected_format"
            ),
            "actions_rejected_rule": sum(
                1 for a in actions if a["outcome_status"] == "rejected_rule"
            ),
        },
        "deaths": deaths,
        "decision_status_counts": dict(
            sorted(Counter(a["decision_status"] for a in actions).items())
        ),
        "outcome_counts": dict(sorted(Counter(a["outcome_status"] for a in actions).items())),
        "usage": {
            "calls_recorded": len(calls),
            "paid_calls_recorded": len(paid_calls),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "estimated_cost_usd": (
                None
                if unpriced_paid
                else round(sum(c["estimated_cost_usd"] or 0.0 for c in paid_calls), 8)
            ),
            "actual_cost_usd": None,
            "actual_cost_source": "unavailable",
            "budget_snapshot": budget,
        },
    }


def compare_runs(store: ExperimentStore, run_a: str, run_b: str) -> dict[str, Any]:
    """Compare two runs. ``comparable`` is True only when the world conditions match."""
    a, b = summarize_run(store, run_a), summarize_run(store, run_b)
    comparable = a["scenario_hash"] == b["scenario_hash"]
    deltas = {k: b["metrics"][k] - a["metrics"][k] for k in a["metrics"]}
    return {
        "run_a": run_a,
        "run_b": run_b,
        "comparable": comparable,
        "note": (
            "same world conditions (scenario hash)"
            if comparable
            else "different world conditions: metric deltas are not a fair comparison"
        ),
        "identical_final_state": a["final_state_hash"] == b["final_state_hash"],
        "metric_deltas_b_minus_a": deltas,
        "a": a["metrics"],
        "b": b["metrics"],
    }
