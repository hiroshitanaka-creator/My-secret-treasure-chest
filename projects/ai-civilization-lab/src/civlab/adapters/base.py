"""Model Adapter interface shared by the mock and real LLM adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ReproClass(StrEnum):
    """How reproducible a run's *decisions* are.

    A: fully deterministic (same inputs -> identical outputs).
    B: conditions are controlled but outputs may vary (LLM sampling).
    C: depends on external service state that we cannot control or record
       completely (provider-side model updates, live data).

    World updates are always class A: given the recorded actions, the World
    Engine replays a run bit-for-bit regardless of the decision class.
    """

    A = "A"
    B = "B"
    C = "C"


class AdapterError(RuntimeError):
    """Base class for adapter failures."""


class TransientAdapterError(AdapterError):
    """Failure that may succeed on retry (timeouts, rate limits, 5xx)."""


class PermanentAdapterError(AdapterError):
    """Failure that will not succeed on retry (bad config, auth, 4xx)."""


class PaidApiDisabledError(AdapterError):
    """A paid adapter was constructed without explicit enablement."""


@dataclass(frozen=True)
class DecisionRequest:
    run_id: str
    turn: int
    resident_id: str
    prompt_version: str
    instructions: str
    input_text: str
    observation: dict[str, Any]
    allowed_actions: tuple[str, ...]
    response_schema: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class AdapterResponse:
    raw_output: str
    usage: Usage
    model: str
    status: str = "completed"


@runtime_checkable
class ModelAdapter(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def is_paid(self) -> bool: ...

    @property
    def reproducibility_class(self) -> ReproClass: ...

    @property
    def max_output_tokens(self) -> int: ...

    def complete(self, request: DecisionRequest) -> AdapterResponse: ...
