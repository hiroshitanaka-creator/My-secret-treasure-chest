"""End-to-end mock simulation: storage, reproducibility, replay verification, failure safety."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from civlab.adapters.mock import ScriptedAdapter
from civlab.config import ExperimentConfig
from civlab.evaluation import compare_runs, summarize_run, verify_run
from civlab.runner import SimulationRunner, collect_provenance
from civlab.store import ExperimentStore

from .conftest import PROVENANCE
from .helpers import FakeAdapter

# Pinned result of ExperimentConfig(experiment_id="test-exp", residents=6, turns=10, seed=7)
# with the mock adapter. Identical on every OS / Python version in CI. If a rule change
# is intentional, update this value and record the change in docs/decision-log.md.
GOLDEN_FINAL_HASH = "84e64e794ff8fd9a8390b824a14e7e4ce6366753040d1938515dc577d68974d9"


def run(config: ExperimentConfig, store: ExperimentStore, **kw: object):
    return SimulationRunner(config, store, provenance=PROVENANCE, **kw).run()  # type: ignore[arg-type]


def test_six_residents_ten_turns_are_run_and_stored(config, store) -> None:
    result = run(config, store)
    assert result.status == "completed"
    assert result.turns_completed == 10
    turns = store.get_turns(result.run_id)
    assert [t["turn"] for t in turns] == list(range(11))
    ids = [r["resident_id"] for r in json.loads(turns[0]["state_json"])["residents"]]
    assert ids == ["r01", "r02", "r03", "r04", "r05", "r06"]
    assert len(set(ids)) == 6
    run_row = store.get_run(result.run_id)
    assert run_row["status"] == "completed"
    assert run_row["reproducibility_class"] == "A"
    assert run_row["final_state_hash"] == turns[-1]["state_hash"] == result.final_state_hash
    assert len(store.get_actions(result.run_id)) == 6 * 10  # nobody dies in this scenario
    assert store.integrity_check() == "ok"


def test_no_paid_calls_and_zero_cost(config, store) -> None:
    result = run(config, store)
    assert result.budget["paid_calls"] == 0
    assert result.budget["estimated_cost_usd"] == 0.0
    calls = store.get_api_calls(result.run_id)
    assert calls and all(c["paid"] == 0 and c["adapter"] == "mock" for c in calls)
    usage = summarize_run(store, result.run_id)["usage"]
    assert usage["paid_calls_recorded"] == 0
    assert usage["actual_cost_usd"] is None


def test_same_conditions_reproduce_identical_results(config, tmp_path: Path) -> None:
    hashes = []
    for name in ("a", "b"):
        with ExperimentStore(tmp_path / f"{name}.sqlite3") as s:
            r = run(config, s)
            hashes.append(
                (
                    [t["state_hash"] for t in s.get_turns(r.run_id)],
                    [(a["resident_id"], a["raw_output"]) for a in s.get_actions(r.run_id)],
                )
            )
    assert hashes[0] == hashes[1]


def test_golden_hash_is_stable_across_platforms(config, store) -> None:
    result = run(config, store)
    assert result.final_state_hash == GOLDEN_FINAL_HASH


def test_different_seed_changes_result(config, store) -> None:
    a = run(config, store)
    b = run(ExperimentConfig(experiment_id="test-exp", seed=8), store)
    assert a.final_state_hash != b.final_state_hash
    cmp = compare_runs(store, a.run_id, b.run_id)
    assert cmp["comparable"] is False


def test_compare_same_scenario_is_comparable(config, store) -> None:
    a, b = run(config, store), run(config, store)
    cmp = compare_runs(store, a.run_id, b.run_id)
    assert cmp["comparable"] is True
    assert cmp["identical_final_state"] is True
    assert all(v == 0 for v in cmp["metric_deltas_b_minus_a"].values())


def test_stored_run_verifies_by_replay(config, store) -> None:
    result = run(config, store)
    report = verify_run(store, result.run_id)
    assert report == {
        "run_id": result.run_id,
        "ok": True,
        "turns_checked": 10,
        "final_state_hash": result.final_state_hash,
        "problems": [],
    }


def test_tampered_state_is_detected(config, store) -> None:
    result = run(config, store)
    row = store.conn.execute(
        "SELECT state_json FROM turns WHERE run_id=? AND turn=4", (result.run_id,)
    ).fetchone()
    data = json.loads(row[0])
    data["pools"]["food"] += 1
    store.conn.execute(
        "UPDATE turns SET state_json=? WHERE run_id=? AND turn=4",
        (json.dumps(data), result.run_id),
    )
    report = verify_run(store, result.run_id)
    assert not report["ok"]
    assert any("turn 4" in p for p in report["problems"])


def test_tampered_action_is_detected(config, store) -> None:
    result = run(config, store)
    store.conn.execute(
        "UPDATE actions SET action_json=? WHERE run_id=? AND turn=3 AND resident_id='r01'",
        (
            json.dumps({"type": "rest", "target": None, "resource": None, "amount": None}),
            result.run_id,
        ),
    )
    assert not verify_run(store, result.run_id)["ok"]


def test_invalid_model_output_is_rejected_and_recorded(config, store) -> None:
    script = {
        (0, "r01"): "this is not json",
        (0, "r02"): json.dumps(
            {"action": "steal", "target": None, "resource": None, "amount": None, "reason": ""}
        ),
        (0, "r03"): json.dumps(
            {"action": "share", "target": "r03", "resource": "food", "amount": 1, "reason": ""}
        ),
        (0, "r04"): json.dumps(
            {"action": "eat", "target": None, "resource": None, "amount": None, "reason": ""}
        ),
    }
    result = run(config, store, adapter=ScriptedAdapter(script))
    rows = {a["resident_id"]: a for a in store.get_actions(result.run_id) if a["turn"] == 1}
    assert rows["r01"]["decision_status"] == "rejected_format"
    assert rows["r01"]["decision_detail"] == "invalid_json"
    assert rows["r02"]["decision_detail"] == "unknown_action:steal"
    assert rows["r03"]["decision_detail"] == "share_self_target"
    for rid in ("r01", "r02", "r03"):
        assert rows[rid]["action_json"] is None
        assert rows[rid]["outcome_status"] == "no_action"
    assert rows["r04"]["outcome_status"] == "applied"
    assert verify_run(store, result.run_id)["ok"]


def test_failure_mid_run_keeps_completed_turns_intact(config, store, monkeypatch) -> None:
    original = store.record_turn

    def failing(run_id, turn, *args, **kwargs):
        if turn == 5:
            raise OSError("simulated disk failure")
        return original(run_id, turn, *args, **kwargs)

    monkeypatch.setattr(store, "record_turn", failing)
    result = run(config, store)
    assert result.status == "failed"
    assert "simulated disk failure" in (result.error or "")
    assert [t["turn"] for t in store.get_turns(result.run_id)] == [0, 1, 2, 3, 4]
    run_row = store.get_run(result.run_id)
    assert run_row["status"] == "failed"
    assert run_row["turns_completed"] == 4
    assert store.get_errors(result.run_id)[0]["kind"] == "exception"
    assert verify_run(store, result.run_id)["ok"]
    assert store.integrity_check() == "ok"


def test_failure_inside_turn_transaction_rolls_back(config, store, monkeypatch) -> None:
    original = store.record_turn

    def duplicate_rows(run_id, turn, state, h, deaths, actions, calls):
        if turn == 3:
            actions = [*actions, actions[0]]  # violates PRIMARY KEY after turns row insert
        return original(run_id, turn, state, h, deaths, actions, calls)

    monkeypatch.setattr(store, "record_turn", duplicate_rows)
    result = run(config, store)
    assert result.status == "failed"
    assert [t["turn"] for t in store.get_turns(result.run_id)] == [0, 1, 2]
    assert {a["turn"] for a in store.get_actions(result.run_id)} == {1, 2}
    assert {c["turn"] for c in store.get_api_calls(result.run_id)} == {1, 2}
    assert verify_run(store, result.run_id)["ok"]


def test_keyboard_interrupt_marks_run_and_keeps_data(config, store) -> None:
    def behaviour(req, n):
        if req.turn == 3:
            raise KeyboardInterrupt
        return json.dumps(
            {"action": "rest", "target": None, "resource": None, "amount": None, "reason": ""}
        )

    with pytest.raises(KeyboardInterrupt):
        run(config, store, adapter=FakeAdapter(behaviour))
    run_id = store.latest_run_id()
    assert store.get_run(run_id)["status"] == "interrupted"
    assert [t["turn"] for t in store.get_turns(run_id)] == [0, 1, 2, 3]
    assert verify_run(store, run_id)["ok"]


def test_store_survives_reopen(config, tmp_path: Path) -> None:
    path = tmp_path / "reopen.sqlite3"
    with ExperimentStore(path) as s:
        run_id = run(config, s).run_id
    with ExperimentStore(path) as s:
        assert verify_run(s, run_id)["ok"]
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def test_run_metadata_is_recorded(config, store) -> None:
    result = run(config, store)
    row = store.get_run(result.run_id)
    cfg = json.loads(row["config_json"])
    assert cfg["world"] == {"residents": 6, "turns": 10, "seed": 7, "rules": config.rules.to_dict()}
    assert row["seed"] == 7
    assert row["model"] == "mock-heuristic-v1"
    assert row["prompt_version"] == "resident-v0.1"
    assert len(row["prompt_fingerprint"]) == 16
    assert row["split"] == "dev"
    assert json.loads(row["provenance_json"]) == PROVENANCE
    assert row["started_at"].endswith("Z")
    assert row["finished_at"] is not None


def test_collect_provenance_reports_unavailable_git_as_none(tmp_path: Path) -> None:
    prov = collect_provenance(tmp_path)  # not a git repository
    assert prov["git_commit"] is None
    assert prov["git_dirty"] is None
    assert prov["civlab_version"]


def test_tampered_config_is_detected(config, store) -> None:
    result = run(config, store)
    row = store.get_run(result.run_id)
    cfg = json.loads(row["config_json"])
    cfg["openai"]["allow_paid_api"] = True
    cfg["budget"]["max_paid_calls"] = 999
    store.conn.execute(
        "UPDATE runs SET config_json=? WHERE run_id=?", (json.dumps(cfg), result.run_id)
    )
    report = verify_run(store, result.run_id)
    assert not report["ok"]
    assert "stored config does not match config_hash" in report["problems"]


def test_consistently_rewritten_initial_state_is_detected(config, store) -> None:
    """Rewriting turn 0 together with its hash is caught by regenerating it from the seed."""
    from civlab.world import WorldState, state_hash

    result = run(config, store)
    row = store.conn.execute(
        "SELECT state_json FROM turns WHERE run_id=? AND turn=0", (result.run_id,)
    ).fetchone()
    st = WorldState.from_dict(json.loads(row[0]))
    st.residents["r01"].food += 5
    new_hash = state_hash(st)
    store.conn.execute(
        "UPDATE turns SET state_json=?, state_hash=? WHERE run_id=? AND turn=0",
        (json.dumps(st.to_dict()), new_hash, result.run_id),
    )
    store.conn.execute(
        "UPDATE runs SET initial_state_hash=? WHERE run_id=?", (new_hash, result.run_id)
    )
    report = verify_run(store, result.run_id)
    assert not report["ok"]
    assert any("generated by the stored seed" in p for p in report["problems"])


def test_malformed_stored_state_is_reported_not_raised(config, store) -> None:
    result = run(config, store)
    row = store.conn.execute(
        "SELECT state_json FROM turns WHERE run_id=? AND turn=2", (result.run_id,)
    ).fetchone()
    data = json.loads(row[0])
    data["residents"][0]["gold"] = 1
    store.conn.execute(
        "UPDATE turns SET state_json=? WHERE run_id=? AND turn=2",
        (json.dumps(data), result.run_id),
    )
    report = verify_run(store, result.run_id)
    assert not report["ok"]
    assert any("malformed" in p for p in report["problems"])


def test_run_start_is_atomic(config, store, monkeypatch) -> None:
    """If writing turn 0 fails, no orphan 'running' row is left behind."""
    original = store.conn

    class FailingTurnZero:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO turns") and "VALUES (?, 0," in sql:
                raise sqlite3.OperationalError("simulated failure writing turn 0")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "conn", FailingTurnZero(original))
    with pytest.raises(sqlite3.OperationalError):
        run(config, store)
    monkeypatch.setattr(store, "conn", original)
    assert store.list_runs() == []
