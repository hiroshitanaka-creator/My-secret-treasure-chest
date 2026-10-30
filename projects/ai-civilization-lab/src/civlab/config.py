"""Experiment configuration (TOML) with strict validation.

Secrets are never read from config files; API keys come from the environment.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from civlab.adapters.openai_adapter import OpenAISettings
from civlab.budget import BudgetLimits, Pricing
from civlab.prompts import PROMPT_VERSION, available_versions
from civlab.world import WorldRules, canonical_json

ADAPTERS = ("mock", "openai")
SPLITS = ("dev", "heldout")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_SECTIONS: dict[str, set[str]] = {
    "experiment": {"id", "description", "split"},
    "world": {"residents", "turns", "seed", "rules"},
    "agent": {"adapter", "model", "prompt_version"},
    "budget": {
        "max_calls",
        "max_paid_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_estimated_cost_usd",
        "max_retries",
    },
    "pricing": {"input_per_million_usd", "output_per_million_usd", "source", "checked_at"},
    "openai": {"allow_paid_api", "timeout_seconds", "max_output_tokens", "temperature"},
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    description: str = ""
    split: str = "dev"
    residents: int = 6
    turns: int = 10
    seed: int = 0
    rules: WorldRules = field(default_factory=WorldRules)
    adapter: str = "mock"
    model: str = "mock-heuristic-v1"
    prompt_version: str = PROMPT_VERSION
    budget: BudgetLimits = field(default_factory=BudgetLimits)
    pricing: Pricing = field(default_factory=Pricing)
    openai: OpenAISettings = field(default_factory=OpenAISettings)

    def __post_init__(self) -> None:
        for name in ("residents", "turns", "seed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"world.{name} must be an integer")
        for name in ("experiment_id", "description", "split", "adapter", "model"):
            if not isinstance(getattr(self, name), str):
                raise ConfigError(f"{name} must be a string")
        if not _ID_RE.match(self.experiment_id):
            raise ConfigError("experiment.id must match [A-Za-z0-9._-]{1,64}")
        if self.split not in SPLITS:
            raise ConfigError(f"experiment.split must be one of {SPLITS}")
        if not 1 <= self.residents <= 99:
            raise ConfigError("world.residents must be between 1 and 99")
        if not 1 <= self.turns <= 1000:
            raise ConfigError("world.turns must be between 1 and 1000")
        if self.adapter not in ADAPTERS:
            raise ConfigError(f"agent.adapter must be one of {ADAPTERS}")
        if not self.model:
            raise ConfigError("agent.model must not be empty")
        if self.prompt_version not in available_versions():
            raise ConfigError(f"agent.prompt_version must be one of {available_versions()}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment": {
                "id": self.experiment_id,
                "description": self.description,
                "split": self.split,
            },
            "world": {
                "residents": self.residents,
                "turns": self.turns,
                "seed": self.seed,
                "rules": self.rules.to_dict(),
            },
            "agent": {
                "adapter": self.adapter,
                "model": self.model,
                "prompt_version": self.prompt_version,
            },
            "budget": asdict(self.budget),
            "pricing": asdict(self.pricing),
            # openai.model mirrors agent.model and is not a separate config key.
            "openai": {k: v for k, v in asdict(self.openai).items() if k != "model"},
        }

    def config_hash(self) -> str:
        return _sha(self.to_dict())

    def scenario_hash(self) -> str:
        """Hash of the world conditions only (comparable across agents/prompts)."""
        return _sha(self.to_dict()["world"])


def _sha(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("ascii")).hexdigest()


def _section(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = set(value) - _SECTIONS[name]
    if unknown:
        raise ConfigError(f"unknown keys in [{name}]: {sorted(unknown)}")
    return value


def config_from_dict(raw: Mapping[str, Any]) -> ExperimentConfig:
    unknown = set(raw) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown sections: {sorted(unknown)}")
    exp = _section(raw, "experiment")
    world = _section(raw, "world")
    agent = _section(raw, "agent")
    try:
        rules = WorldRules.from_dict(world.get("rules", {}))
        budget = BudgetLimits(**_section(raw, "budget"))
        pricing = Pricing(**_section(raw, "pricing"))
        openai = OpenAISettings(**_section(raw, "openai"))
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
    if "id" not in exp:
        raise ConfigError("[experiment] id is required")
    adapter = agent.get("adapter", "mock")
    default_model = "mock-heuristic-v1" if adapter == "mock" else ""
    model = agent.get("model", default_model)
    return ExperimentConfig(
        experiment_id=exp["id"],
        description=exp.get("description", ""),
        split=exp.get("split", "dev"),
        residents=world.get("residents", 6),
        turns=world.get("turns", 10),
        seed=world.get("seed", 0),
        rules=rules,
        adapter=adapter,
        model=model,
        prompt_version=agent.get("prompt_version", PROMPT_VERSION),
        budget=budget,
        pricing=pricing,
        openai=OpenAISettings(
            model=model if adapter == "openai" else "",
            allow_paid_api=openai.allow_paid_api,
            timeout_seconds=openai.timeout_seconds,
            max_output_tokens=openai.max_output_tokens,
            temperature=openai.temperature,
        ),
    )


def load_config(path: Path) -> ExperimentConfig:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return config_from_dict(raw)


def load_env_file(path: Path, environ: dict[str, str] | None = None) -> list[str]:
    """Load KEY=VALUE lines into the environment without overriding existing values.

    Returns the names (never the values) of variables that were set. Only used
    when the operator passes ``--env-file`` explicitly.
    """
    env = os.environ if environ is None else environ
    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in env and value:
            env[key] = value
            loaded.append(key)
    return loaded
