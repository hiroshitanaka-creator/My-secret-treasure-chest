"""Experiment Store: SQLite persistence for experiments, runs, turns, actions and usage.

Each turn (decisions, outcomes, resulting state, usage) is written in a single
transaction, so an interrupted or failing run leaves only complete turns.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from civlab.redaction import redact

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    split TEXT NOT NULL,
    adapter TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_fingerprint TEXT NOT NULL,
    seed INTEGER NOT NULL,
    reproducibility_class TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    scenario_hash TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    initial_state_json TEXT NOT NULL,
    initial_state_hash TEXT NOT NULL,
    turns_completed INTEGER NOT NULL DEFAULT 0,
    final_state_hash TEXT,
    budget_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    turn INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    deaths_json TEXT NOT NULL,
    PRIMARY KEY (run_id, turn)
);
CREATE TABLE IF NOT EXISTS actions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    turn INTEGER NOT NULL,
    resident_id TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    raw_output TEXT,
    action_json TEXT,
    decision_reason TEXT NOT NULL,
    decision_detail TEXT NOT NULL,
    outcome_status TEXT,
    outcome_reason TEXT,
    effects_json TEXT,
    PRIMARY KEY (run_id, turn, resident_id)
);
CREATE TABLE IF NOT EXISTS api_calls (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    turn INTEGER NOT NULL,
    resident_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    adapter TEXT NOT NULL,
    model TEXT NOT NULL,
    paid INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    usage_reported INTEGER NOT NULL,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    error TEXT,
    PRIMARY KEY (run_id, turn, resident_id, attempt)
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    turn INTEGER,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ExperimentStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we issue BEGIN/COMMIT explicitly.
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        # executescript commits implicitly, so it runs outside our transaction helper.
        self.conn.executescript(_SCHEMA)
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(f"store schema version {row['value']} != supported {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ExperimentStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ----- writes -----

    def ensure_experiment(self, experiment_id: str, description: str, now: str) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT OR IGNORE INTO experiments(experiment_id, description, created_at) "
                "VALUES (?, ?, ?)",
                (experiment_id, description, now),
            )

    def start_run(self, row: dict[str, Any], initial_state: dict[str, Any]) -> None:
        """Insert the run row and its turn-0 state in one transaction."""
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.transaction() as c:
            c.execute(f"INSERT INTO runs({cols}) VALUES ({marks})", tuple(row.values()))  # noqa: S608
            c.execute(
                "INSERT INTO turns(run_id, turn, state_json, state_hash, deaths_json) "
                "VALUES (?, 0, ?, ?, '[]')",
                (row["run_id"], _dumps(initial_state), row["initial_state_hash"]),
            )

    def last_turn(self, run_id: str) -> tuple[int, str]:
        row = self.conn.execute(
            "SELECT turn, state_hash FROM turns WHERE run_id = ? ORDER BY turn DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"run {run_id} has no stored turns")
        return int(row["turn"]), str(row["state_hash"])

    def record_turn(
        self,
        run_id: str,
        turn: int,
        state: dict[str, Any],
        state_hash: str,
        deaths: list[str],
        actions: list[dict[str, Any]],
        calls: list[dict[str, Any]],
    ) -> None:
        """Atomically store the state *after* ``turn`` plus the decisions that produced it.

        ``turn`` is the index of the resulting state (1..N). Turn 0 is the
        initial state and is stored by ``start_run``.
        """
        with self.transaction() as c:
            c.execute(
                "INSERT INTO turns(run_id, turn, state_json, state_hash, deaths_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, turn, _dumps(state), state_hash, _dumps(deaths)),
            )
            c.executemany(
                "INSERT INTO actions(run_id, turn, resident_id, decision_status, raw_output, "
                "action_json, decision_reason, decision_detail, outcome_status, outcome_reason, "
                "effects_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        turn,
                        a["resident_id"],
                        a["decision_status"],
                        None if a["raw_output"] is None else redact(a["raw_output"]),
                        None if a["action"] is None else _dumps(a["action"]),
                        redact(a["decision_reason"]),
                        redact(a["decision_detail"]),
                        a["outcome_status"],
                        a["outcome_reason"],
                        None if a["effects"] is None else _dumps(a["effects"]),
                    )
                    for a in actions
                ],
            )
            c.executemany(
                "INSERT INTO api_calls(run_id, turn, resident_id, attempt, adapter, model, paid, "
                "outcome, input_tokens, output_tokens, usage_reported, estimated_cost_usd, "
                "actual_cost_usd, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        turn,
                        k["resident_id"],
                        k["attempt"],
                        k["adapter"],
                        k["model"],
                        int(k["paid"]),
                        k["outcome"],
                        k["input_tokens"],
                        k["output_tokens"],
                        int(k["usage_reported"]),
                        k["estimated_cost_usd"],
                        None,  # actual billed cost is not available from the API response
                        None if k["error"] is None else redact(k["error"]),
                    )
                    for k in calls
                ],
            )
            c.execute("UPDATE runs SET turns_completed = ? WHERE run_id = ?", (turn, run_id))

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: str,
        final_state_hash: str | None,
        budget: dict[str, Any],
        error: str | None,
        error_kind: str | None = None,
        error_turn: int | None = None,
    ) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE runs SET status = ?, finished_at = ?, final_state_hash = ?, "
                "budget_json = ?, error = ? WHERE run_id = ?",
                (
                    status,
                    finished_at,
                    final_state_hash,
                    _dumps(budget),
                    None if error is None else redact(error),
                    run_id,
                ),
            )
            if error is not None:
                c.execute(
                    "INSERT INTO errors(run_id, turn, kind, message, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, error_turn, error_kind or "error", redact(error), finished_at),
                )

    # ----- reads -----

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return dict(row)

    def latest_run_id(self) -> str:
        row = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise KeyError("store has no runs")
        return str(row["run_id"])

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT run_id, experiment_id, status, started_at, adapter, model, seed, "
            "turns_completed, final_state_hash FROM runs ORDER BY started_at, rowid"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_turns(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT turn, state_json, state_hash, deaths_json FROM turns "
            "WHERE run_id = ? ORDER BY turn",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_actions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE run_id = ? ORDER BY turn, resident_id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_api_calls(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM api_calls WHERE run_id = ? ORDER BY turn, resident_id, attempt",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_errors(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT turn, kind, message, created_at FROM errors WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def integrity_check(self) -> str:
        return str(self.conn.execute("PRAGMA integrity_check").fetchone()[0])
