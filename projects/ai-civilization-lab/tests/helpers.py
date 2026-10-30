"""Fake adapters for tests (no network, no cost)."""

from __future__ import annotations

import json
from collections.abc import Callable

from civlab.adapters.base import (
    AdapterResponse,
    DecisionRequest,
    PermanentAdapterError,
    ReproClass,
    TransientAdapterError,
    Usage,
)

ZERO_USAGE = Usage(0, 0)

REST = json.dumps(
    {"action": "rest", "target": None, "resource": None, "amount": None, "reason": "fake"}
)


class FakeAdapter:
    """Configurable fake. ``behaviour`` returns a raw output or raises."""

    def __init__(
        self,
        behaviour: Callable[[DecisionRequest, int], str] | None = None,
        *,
        paid: bool = False,
        usage: Usage = ZERO_USAGE,
        max_output_tokens: int = 50,
    ) -> None:
        self.behaviour = behaviour or (lambda req, n: REST)
        self.paid = paid
        self.usage = usage
        self._max_out = max_output_tokens
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def is_paid(self) -> bool:
        return self.paid

    @property
    def reproducibility_class(self) -> ReproClass:
        return ReproClass.B if self.paid else ReproClass.A

    @property
    def max_output_tokens(self) -> int:
        return self._max_out

    def complete(self, request: DecisionRequest) -> AdapterResponse:
        self.calls += 1
        raw = self.behaviour(request, self.calls)
        return AdapterResponse(raw, self.usage, self.model)


def always_transient(req: DecisionRequest, n: int) -> str:
    raise TransientAdapterError("simulated timeout")


def always_permanent(req: DecisionRequest, n: int) -> str:
    raise PermanentAdapterError("simulated 400")
