"""Mock adapters: deterministic, offline, free.

They return raw JSON text exactly like an LLM would, so the same parsing and
validation path is exercised as for a real model.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping
from typing import Any

from civlab.adapters.base import AdapterResponse, DecisionRequest, ReproClass, Usage


def _decision(
    action: str,
    reason: str,
    target: str | None = None,
    resource: str | None = None,
    amount: int | None = None,
) -> str:
    return json.dumps(
        {
            "action": action,
            "target": target,
            "resource": resource,
            "amount": amount,
            "reason": reason,
        },
        sort_keys=True,
    )


class MockAdapter:
    """Rule-based resident policy with a per-decision seeded RNG.

    The RNG is derived from (seed, turn, resident_id), so a decision does not
    depend on the order in which residents are asked.
    """

    def __init__(self, seed: int, model: str = "mock-heuristic-v1") -> None:
        self.seed = seed
        self._model = model

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_paid(self) -> bool:
        return False

    @property
    def reproducibility_class(self) -> ReproClass:
        return ReproClass.A

    @property
    def max_output_tokens(self) -> int:
        return 0

    def _rng(self, request: DecisionRequest) -> random.Random:
        key = f"{self.seed}:{request.turn}:{request.resident_id}".encode()
        return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))

    def complete(self, request: DecisionRequest) -> AdapterResponse:
        raw = self._decide(request.observation, self._rng(request))
        return AdapterResponse(raw_output=raw, usage=Usage(0, 0), model=self._model)

    @staticmethod
    def _decide(obs: Mapping[str, Any], rng: random.Random) -> str:
        me = obs["self"]
        rules = obs["rules"]
        pools = obs["pools"]
        threshold = rules["need_threshold"]

        if me["thirst"] >= threshold - 1 and me["water"] > 0:
            return _decision("drink", "thirsty")
        if me["hunger"] >= threshold - 1 and me["food"] > 0:
            return _decision("eat", "hungry")

        if me["personality"] == "generous" or (
            me["personality"] == "balanced" and rng.random() < 0.3
        ):
            for resource in ("water", "food"):
                spare = me[resource] - 2
                takers = [o for o in obs["others"] if o["alive"] and o[resource] <= 1]
                if spare > 0 and takers:
                    target = rng.choice(sorted(takers, key=lambda o: o["id"]))
                    amount = min(spare, rules["max_share"])
                    return _decision("share", "helping", target["id"], resource, amount)

        if me["water"] <= me["food"] and pools["water"] > 0:
            return _decision("gather_water", "stock_water")
        if pools["food"] > 0:
            return _decision("gather_food", "stock_food")
        return _decision("rest", "nothing_to_do")


class ScriptedAdapter:
    """Returns pre-recorded raw outputs keyed by (turn, resident_id).

    Missing keys fall back to ``default``. Used to inject malformed or
    disallowed outputs in tests and to replay a fixed decision sequence.
    """

    def __init__(
        self,
        script: Mapping[tuple[int, str], str],
        default: str = _decision("rest", "scripted_default"),
        model: str = "mock-scripted-v1",
    ) -> None:
        self.script = dict(script)
        self.default = default
        self._model = model

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_paid(self) -> bool:
        return False

    @property
    def reproducibility_class(self) -> ReproClass:
        return ReproClass.A

    @property
    def max_output_tokens(self) -> int:
        return 0

    def complete(self, request: DecisionRequest) -> AdapterResponse:
        raw = self.script.get((request.turn, request.resident_id), self.default)
        return AdapterResponse(raw_output=raw, usage=Usage(0, 0), model=self._model)
