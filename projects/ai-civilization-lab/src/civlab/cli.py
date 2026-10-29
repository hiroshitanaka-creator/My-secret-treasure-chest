"""Command-line interface. All output is JSON on stdout; logs go to stderr."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from civlab import __version__
from civlab.adapters.base import AdapterError
from civlab.adapters.openai_adapter import paid_api_blockers
from civlab.budget import KillSwitch
from civlab.config import ConfigError, load_config, load_env_file
from civlab.evaluation import compare_runs, summarize_run, verify_run
from civlab.redaction import RedactingFilter
from civlab.runner import SimulationRunner
from civlab.store import ExperimentStore

DEFAULT_DB = Path("runs") / "civlab.sqlite3"

EXIT_OK = 0
EXIT_RUN_NOT_COMPLETED = 1
EXIT_USAGE = 2
EXIT_VERIFY_FAILED = 3


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True))


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("civlab")
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    root.propagate = False


def _run_id(store: ExperimentStore, value: str) -> str:
    return store.latest_run_id() if value == "latest" else value


def cmd_run(args: argparse.Namespace) -> int:
    if args.env_file:
        names = load_env_file(Path(args.env_file))
        logging.getLogger("civlab").info("loaded env names: %s", ", ".join(names) or "(none)")
    config = load_config(Path(args.config))
    kill = KillSwitch(Path(args.stop_file) if args.stop_file else None)
    with ExperimentStore(Path(args.db)) as store:
        result = SimulationRunner(config, store, kill_switch=kill).run()
    _print(
        {
            "run_id": result.run_id,
            "status": result.status,
            "turns_completed": result.turns_completed,
            "final_state_hash": result.final_state_hash,
            "budget": result.budget,
            "error": result.error,
            "db": str(args.db),
        }
    )
    return EXIT_OK if result.status in ("completed", "extinct") else EXIT_RUN_NOT_COMPLETED


def cmd_check_config(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    blockers = paid_api_blockers(config.openai) if config.adapter == "openai" else []
    _print(
        {
            "experiment_id": config.experiment_id,
            "adapter": config.adapter,
            "model": config.model,
            "config_hash": config.config_hash(),
            "scenario_hash": config.scenario_hash(),
            "paid_api": {
                "uses_paid_adapter": config.adapter == "openai",
                "enabled_now": config.adapter == "openai" and not blockers,
                "blockers": blockers,
                "pricing_configured": config.pricing.is_configured,
                "max_paid_calls": config.budget.max_paid_calls,
                "max_estimated_cost_usd": config.budget.max_estimated_cost_usd,
            },
        }
    )
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    with ExperimentStore(Path(args.db)) as store:
        report = verify_run(store, _run_id(store, args.run_id))
        report["sqlite_integrity"] = store.integrity_check()
    _print(report)
    ok = report["ok"] and report["sqlite_integrity"] == "ok"
    return EXIT_OK if ok else EXIT_VERIFY_FAILED


def cmd_summary(args: argparse.Namespace) -> int:
    with ExperimentStore(Path(args.db)) as store:
        _print(summarize_run(store, _run_id(store, args.run_id)))
    return EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    with ExperimentStore(Path(args.db)) as store:
        _print(compare_runs(store, args.run_a, args.run_b))
    return EXIT_OK


def cmd_list_runs(args: argparse.Namespace) -> int:
    with ExperimentStore(Path(args.db)) as store:
        _print(store.list_runs())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="civlab", description="AI Civilization Lab (phase 0)")
    p.add_argument("--version", action="version", version=f"civlab {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    def db_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite file (default {DEFAULT_DB})")

    r = sub.add_parser("run", help="run one simulation and store it")
    r.add_argument("--config", required=True)
    db_arg(r)
    r.add_argument("--stop-file", help="kill switch: the run stops if this file exists")
    r.add_argument("--env-file", help="load KEY=VALUE pairs (never printed) before running")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("check-config", help="validate a config; report paid-API gating")
    c.add_argument("--config", required=True)
    c.set_defaults(func=cmd_check_config)

    v = sub.add_parser("verify", help="replay a stored run and check every state hash")
    db_arg(v)
    v.add_argument("--run-id", default="latest")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("summary", help="metrics and usage of a stored run")
    db_arg(s)
    s.add_argument("--run-id", default="latest")
    s.set_defaults(func=cmd_summary)

    m = sub.add_parser("compare", help="compare two stored runs")
    db_arg(m)
    m.add_argument("run_a")
    m.add_argument("run_b")
    m.set_defaults(func=cmd_compare)

    ls = sub.add_parser("list-runs", help="list stored runs")
    db_arg(ls)
    ls.set_defaults(func=cmd_list_runs)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        code: int = args.func(args)
    except (ConfigError, AdapterError, KeyError, FileNotFoundError) as exc:
        _print({"error": f"{type(exc).__name__}: {exc}"})
        return EXIT_USAGE
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
