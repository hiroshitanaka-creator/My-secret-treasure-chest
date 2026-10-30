from __future__ import annotations

import logging
from pathlib import Path

import pytest

from civlab.config import (
    ConfigError,
    ExperimentConfig,
    config_from_dict,
    load_config,
    load_env_file,
)
from civlab.redaction import REDACTED, RedactingFilter, redact

PROJECT = Path(__file__).resolve().parents[1]


def test_shipped_mock_config_is_valid_and_free() -> None:
    config = load_config(PROJECT / "configs" / "mock-6x10.toml")
    assert (config.residents, config.turns, config.adapter) == (6, 10, "mock")
    assert config.budget.max_paid_calls == 0
    assert config.openai.allow_paid_api is False


def test_config_roundtrip_keeps_hash() -> None:
    config = load_config(PROJECT / "configs" / "mock-6x10.toml")
    again = config_from_dict(config.to_dict())
    assert again == config
    assert again.config_hash() == config.config_hash()


def test_scenario_hash_ignores_agent_settings() -> None:
    a = ExperimentConfig(experiment_id="x", seed=1)
    b = ExperimentConfig(experiment_id="y", seed=1, model="mock-other")
    assert a.scenario_hash() == b.scenario_hash()
    assert a.config_hash() != b.config_hash()


@pytest.mark.parametrize(
    "raw",
    [
        {"experiment": {"id": "x"}, "secrets": {}},
        {"experiment": {"id": "x", "typo": 1}},
        {"experiment": {}},
        {"experiment": {"id": "bad id!"}},
        {"experiment": {"id": "x"}, "world": {"residents": "6"}},
        {"experiment": {"id": "x"}, "world": {"residents": 0}},
        {"experiment": {"id": "x"}, "world": {"rules": {"gather_yield": -1}}},
        {"experiment": {"id": "x"}, "world": {"rules": {"unknown_rule": 1}}},
        {"experiment": {"id": "x"}, "agent": {"adapter": "anthropic"}},
        {"experiment": {"id": "x"}, "agent": {"prompt_version": "v999"}},
        {"experiment": {"id": "x"}, "agent": {"adapter": "openai"}},  # model required
        {"experiment": {"id": "x"}, "openai": {"allow_paid_api": "true"}},  # must be bool
        {"experiment": {"id": "x"}, "openai": {"api_key": "sk-nope"}},  # keys never in config
        {"experiment": {"id": "x"}, "budget": {"max_calls": -5}},
        {"experiment": {"id": "x"}, "pricing": {"input_per_million_usd": "cheap"}},
    ],
)
def test_invalid_configs_are_rejected(raw) -> None:
    with pytest.raises(ConfigError):
        config_from_dict(raw)


def test_missing_config_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_env_file_does_not_override_and_reports_names_only(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# comment\nA=1\nexport B='two'\nC=\nEXISTING=new\n", encoding="utf-8")
    env = {"EXISTING": "old"}
    assert load_env_file(f, env) == ["A", "B"]
    assert env == {"EXISTING": "old", "A": "1", "B": "two"}


@pytest.mark.parametrize(
    "text",
    [
        "key sk-proj-abcdefghijklmnopqrstuvwxyz123456 used",
        "Authorization: Bearer abcdefghijklmnop.qrs",
        "OPENAI_API_KEY=dummy0000000000",
        'api_key: "zzzzzzzzzz"',
        "password=hunter2hunter2",
    ],
)
def test_redact_masks_secret_shapes(text: str) -> None:
    out = redact(text)
    assert REDACTED in out
    for secret in ("sk-proj-abc", "abcdefghijklmnop", "dummy0000000000", "zzzzzzzzzz", "hunter2"):
        assert secret not in out


def test_redact_masks_current_env_key_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "plainvalue-without-prefix-123")
    assert "plainvalue" not in redact("got plainvalue-without-prefix-123 back")


def test_redact_leaves_normal_text() -> None:
    text = "turn 3: r01 gathered food (tokens=120)"
    assert redact(text) == text


def test_logging_filter_redacts_messages_and_args(caplog) -> None:
    logger = logging.getLogger("civlab.test")
    handler = caplog.handler
    handler.addFilter(RedactingFilter())
    try:
        with caplog.at_level(logging.INFO, logger="civlab.test"):
            logger.info("calling with %s", "sk-abcdefghijklmnopqrstuvwxyz")
    finally:
        handler.removeFilter(handler.filters[-1])
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in caplog.text
    assert REDACTED in caplog.text
