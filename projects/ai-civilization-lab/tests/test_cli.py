from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from civlab.cli import EXIT_OK, EXIT_RUN_NOT_COMPLETED, EXIT_USAGE, EXIT_VERIFY_FAILED, main
from civlab.store import ExperimentStore

PROJECT = Path(__file__).resolve().parents[1]
MOCK_CONFIG = str(PROJECT / "configs" / "mock-6x10.toml")


def cli(capsys, *argv: str) -> tuple[int, Any]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_run_verify_summary_list_compare(capsys, tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    code, run1 = cli(capsys, "run", "--config", MOCK_CONFIG, "--db", db)
    assert code == EXIT_OK and run1["status"] == "completed" and run1["turns_completed"] == 10
    assert run1["budget"]["paid_calls"] == 0
    code, run2 = cli(capsys, "run", "--config", MOCK_CONFIG, "--db", db)
    assert run2["final_state_hash"] == run1["final_state_hash"]

    code, report = cli(capsys, "verify", "--db", db)
    assert code == EXIT_OK and report["ok"] and report["sqlite_integrity"] == "ok"
    assert report["run_id"] == run2["run_id"]

    code, summary = cli(capsys, "summary", "--db", db, "--run-id", run1["run_id"])
    assert summary["metrics"]["residents"] == 6

    code, runs = cli(capsys, "list-runs", "--db", db)
    assert [r["run_id"] for r in runs] == [run1["run_id"], run2["run_id"]]

    code, cmp = cli(capsys, "compare", "--db", db, run1["run_id"], run2["run_id"])
    assert cmp["comparable"] and cmp["identical_final_state"]


def test_verify_failure_exit_code(capsys, tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    cli(capsys, "run", "--config", MOCK_CONFIG, "--db", str(db))
    with ExperimentStore(db) as s:
        s.conn.execute("UPDATE turns SET state_hash='0' WHERE turn=2")
    code, report = cli(capsys, "verify", "--db", str(db))
    assert code == EXIT_VERIFY_FAILED and not report["ok"]


def test_check_config_reports_paid_api_blocked(capsys, tmp_path: Path) -> None:
    cfg = tmp_path / "openai.toml"
    cfg.write_text(
        '[experiment]\nid = "x"\n[agent]\nadapter = "openai"\nmodel = "some-model"\n',
        encoding="utf-8",
    )
    code, out = cli(capsys, "check-config", "--config", str(cfg))
    assert code == EXIT_OK
    assert out["paid_api"]["uses_paid_adapter"] is True
    assert out["paid_api"]["enabled_now"] is False
    assert len(out["paid_api"]["blockers"]) == 3


def test_run_with_openai_config_is_refused_without_enablement(capsys, tmp_path: Path) -> None:
    cfg = tmp_path / "openai.toml"
    cfg.write_text(
        '[experiment]\nid = "x"\n[agent]\nadapter = "openai"\nmodel = "some-model"\n',
        encoding="utf-8",
    )
    db = tmp_path / "o.sqlite3"
    code, out = cli(capsys, "run", "--config", str(cfg), "--db", str(db))
    assert code == EXIT_USAGE
    assert "PaidApiDisabledError" in out["error"]
    with ExperimentStore(db) as s:
        assert s.list_runs() == []


def test_stop_file_gives_non_zero_exit(capsys, tmp_path: Path) -> None:
    stop = tmp_path / "STOP"
    stop.touch()
    code, out = cli(
        capsys,
        "run",
        "--config",
        MOCK_CONFIG,
        "--db",
        str(tmp_path / "s.sqlite3"),
        "--stop-file",
        str(stop),
    )
    assert code == EXIT_RUN_NOT_COMPLETED
    assert out["status"] == "stopped_kill_switch"


def test_bad_config_is_usage_error(capsys, tmp_path: Path) -> None:
    code, out = cli(capsys, "check-config", "--config", str(tmp_path / "missing.toml"))
    assert code == EXIT_USAGE and "ConfigError" in out["error"]


def test_unknown_run_id_is_usage_error(capsys, tmp_path: Path) -> None:
    code, _ = cli(capsys, "verify", "--db", str(tmp_path / "e.sqlite3"), "--run-id", "nope")
    assert code == EXIT_USAGE


def test_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "civlab" in capsys.readouterr().out
