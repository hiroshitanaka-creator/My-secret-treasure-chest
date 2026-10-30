"""Simulation Runner: drives turns, wires agents to the World Engine, persists results."""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civlab import __version__
from civlab.adapters.base import ModelAdapter
from civlab.adapters.mock import MockAdapter
from civlab.adapters.openai_adapter import OpenAIAdapter
from civlab.agents import CallRecord, Decision, ResidentAgent
from civlab.budget import BudgetExceededError, BudgetGuard, KillSwitch, KillSwitchEngagedError
from civlab.config import ExperimentConfig
from civlab.prompts import prompt_fingerprint
from civlab.redaction import redact
from civlab.store import ExperimentStore
from civlab.world import (
    WorldEngine,
    canonical_json,
    make_initial_state,
    state_hash,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    turns_completed: int
    final_state_hash: str | None
    budget: dict[str, Any]
    error: str | None


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_run_id() -> str:
    return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            ["git", *args],  # noqa: S607 (git resolved from PATH on purpose)
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def collect_provenance(cwd: Path | None = None) -> dict[str, Any]:
    """Software provenance. Values that could not be obtained are ``None``."""
    where = cwd or Path.cwd()
    commit = _git(["rev-parse", "HEAD"], where)
    status = _git(["status", "--porcelain"], where)
    return {
        "civlab_version": __version__,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": commit,
        "git_dirty": None if status is None else bool(status),
    }


def build_adapter(
    config: ExperimentConfig, environ: Mapping[str, str] | None = None
) -> ModelAdapter:
    if config.adapter == "mock":
        return MockAdapter(seed=config.seed, model=config.model)
    if config.adapter == "openai":
        return OpenAIAdapter(config.openai, environ=environ)
    raise ValueError(f"unknown adapter {config.adapter}")  # pragma: no cover


def _call_row(rid: str, record: CallRecord, adapter: str) -> dict[str, Any]:
    return {
        "resident_id": rid,
        "attempt": record.attempt,
        "adapter": adapter,
        "model": record.model,
        "paid": record.paid,
        "outcome": record.outcome,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "usage_reported": record.usage_reported,
        "estimated_cost_usd": record.estimated_cost_usd,
        "error": record.error,
    }


class SimulationRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        store: ExperimentStore,
        *,
        adapter: ModelAdapter | None = None,
        kill_switch: KillSwitch | None = None,
        provenance: dict[str, Any] | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.config = config
        self.store = store
        self.adapter = adapter if adapter is not None else build_adapter(config)
        self.kill_switch = kill_switch or KillSwitch()
        self.provenance = provenance if provenance is not None else collect_provenance()
        self.clock = clock

    def run(self) -> RunResult:
        cfg = self.config
        engine = WorldEngine(cfg.rules)
        state = make_initial_state(cfg.residents, cfg.seed, cfg.rules)
        guard = BudgetGuard(cfg.budget, cfg.pricing, self.kill_switch)
        pending: list[tuple[str, CallRecord]] = []
        agent = ResidentAgent(self.adapter, guard, cfg.rules, cfg.prompt_version, call_sink=pending)
        run_id = new_run_id()
        now = self.clock()

        self.store.ensure_experiment(cfg.experiment_id, cfg.description, now)
        initial = state.to_dict()
        initial_hash = state_hash(state)
        self.store.start_run(
            {
                "run_id": run_id,
                "experiment_id": cfg.experiment_id,
                "status": "running",
                "started_at": now,
                "split": cfg.split,
                "adapter": self.adapter.name,
                "model": self.adapter.model,
                "prompt_version": cfg.prompt_version,
                "prompt_fingerprint": prompt_fingerprint(cfg.prompt_version),
                "seed": cfg.seed,
                "reproducibility_class": self.adapter.reproducibility_class.value,
                "config_json": canonical_json(cfg.to_dict()),
                "config_hash": cfg.config_hash(),
                "scenario_hash": cfg.scenario_hash(),
                "provenance_json": canonical_json(self.provenance),
                "initial_state_json": canonical_json(initial),
                "initial_state_hash": initial_hash,
            },
            initial,
        )
        log.info("run %s started (%s, %s)", run_id, self.adapter.name, self.adapter.model)

        status, error, error_kind = "completed", None, None
        current_turn = state.turn
        try:
            for _ in range(cfg.turns):
                current_turn = state.turn
                self.kill_switch.check()
                pending.clear()
                decisions: dict[str, Decision] = {
                    rid: agent.decide(run_id, state, rid) for rid in state.living_ids()
                }
                result = engine.step(state, {rid: d.action for rid, d in decisions.items()})
                rows = []
                for rid, outcome in result.outcomes.items():
                    d = decisions.get(rid)
                    rows.append(
                        {
                            "resident_id": rid,
                            "decision_status": d.status if d else "not_asked",
                            "raw_output": d.raw_output if d else None,
                            "action": d.action.to_dict() if d and d.action else None,
                            "decision_reason": d.reason if d else "",
                            "decision_detail": d.detail if d else "",
                            "outcome_status": outcome.status,
                            "outcome_reason": outcome.reason,
                            "effects": outcome.effects,
                        }
                    )
                self.store.record_turn(
                    run_id,
                    result.state.turn,
                    result.state.to_dict(),
                    state_hash(result.state),
                    result.deaths,
                    rows,
                    [_call_row(rid, rec, self.adapter.name) for rid, rec in pending],
                )
                pending.clear()
                state = result.state
                if not state.living_ids():
                    status = "extinct"
                    break
        except BudgetExceededError as exc:
            status, error, error_kind = "stopped_budget", str(exc), "budget"
        except KillSwitchEngagedError as exc:
            status, error, error_kind = "stopped_kill_switch", str(exc), "kill_switch"
        except KeyboardInterrupt:
            self._finish(
                run_id,
                "interrupted",
                guard,
                "KeyboardInterrupt",
                "interrupted",
                current_turn,
                pending,
            )
            raise
        except Exception as exc:
            status, error, error_kind = "failed", f"{type(exc).__name__}: {exc}", "exception"
            log.exception("run %s failed", run_id)

        last_turn, last_hash = self._finish(
            run_id, status, guard, error, error_kind, current_turn, pending
        )
        return RunResult(
            run_id=run_id,
            status=status,
            turns_completed=last_turn,
            final_state_hash=last_hash,
            budget=guard.snapshot(),
            error=None if error is None else redact(error),
        )

    def _finish(
        self,
        run_id: str,
        status: str,
        guard: BudgetGuard,
        error: str | None,
        error_kind: str | None,
        turn: int,
        pending: list[tuple[str, CallRecord]],
    ) -> tuple[int, str]:
        """Mark the run finished. The final hash is taken from the last *stored* turn,
        so an interrupt between a commit and the in-memory update cannot desync them."""
        last_turn, last_hash = self.store.last_turn(run_id)
        budget = guard.snapshot()
        if pending:
            # Calls made during an unfinished turn are still accounted for.
            budget["unfinished_turn_calls"] = [
                _call_row(rid, rec, self.adapter.name) | {"turn": turn + 1} for rid, rec in pending
            ]
        self.store.finish_run(
            run_id,
            status=status,
            finished_at=self.clock(),
            final_state_hash=last_hash,
            budget=budget,
            error=error,
            error_kind=error_kind,
            error_turn=turn + 1 if error is not None else None,
        )
        log.info("run %s finished: %s", run_id, status)
        return last_turn, last_hash
