"""Shared fixtures. Every test runs with outbound network blocked and no API keys."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from civlab.config import ExperimentConfig
from civlab.store import ExperimentStore

PROVENANCE = {
    "civlab_version": "test",
    "python_version": "test",
    "platform": "test",
    "git_commit": None,
    "git_dirty": None,
}


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any code path tries to open an internet connection."""

    def guard(self: socket.socket, address: object) -> None:
        raise NetworkBlockedError(f"network access attempted in tests: {address!r}")

    def guard_create(*args: object, **kwargs: object) -> socket.socket:
        raise NetworkBlockedError("network access attempted in tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard_create)


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "CIVLAB_ALLOW_PAID_API", "CIVLAB_KILL_SWITCH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ExperimentStore]:
    with ExperimentStore(tmp_path / "test.sqlite3") as s:
        yield s


@pytest.fixture
def config() -> ExperimentConfig:
    return ExperimentConfig(experiment_id="test-exp", residents=6, turns=10, seed=7)
