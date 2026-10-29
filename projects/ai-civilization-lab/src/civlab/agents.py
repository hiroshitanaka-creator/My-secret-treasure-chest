"""Agent Interface: turns a world view into a validated decision for one resident.

Pipeline per resident and turn:
observation -> prompt -> Budget Guard -> Model Adapter (with bounded retry)
-> ``parse_action`` (format + allowed set) -> ``Decision``.
The World Engine later checks feasibility and applies the action.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from civlab.actions import (
    ALL_ACTIONS,
    Action,
    ActionValidationError,
    action_json_schema,
    parse_action,
)
from civlab.adapters.base import (
    AdapterError,
    DecisionRequest,
    ModelAdapter,
    TransientAdapterError,
)
from civlab.budget import BudgetGuard
from civlab.prompts import build_prompt
from civlab.redaction import redact
from civlab.world import WorldRules, WorldState


@dataclass(frozen=True)
class CallRecord:
    attempt: int
    outcome: str  # "ok" | "transient_error" | "permanent_error" | "unexpected_error"
    paid: bool
    model: str
    input_tokens: int
    output_tokens: int
    usage_reported: bool
    estimated_cost_usd: float | None
    error: str | None = None


@dataclass(frozen=True)
class Decision:
    resident_id: str
    status: str  # "valid" | "rejected_format" | "adapter_error"
    action: Action | None
    raw_output: str | None
    reason: str
    detail: str
    calls: list[CallRecord] = field(default_factory=list)


def build_observation(state: WorldState, resident_id: str, rules: WorldRules) -> dict[str, Any]:
    me = state.residents[resident_id]
    return {
        "turn": state.turn,
        "self": {
            "id": me.resident_id,
            "name": me.name,
            "role": me.role,
            "personality": me.personality,
            "food": me.food,
            "water": me.water,
            "hunger": me.hunger,
            "thirst": me.thirst,
            "health": me.health,
            "memory": list(me.memory),
        },
        "pools": dict(state.pools),
        "others": [
            {
                "id": o.resident_id,
                "name": o.name,
                "alive": o.alive,
                "food": o.food,
                "water": o.water,
                "hunger": o.hunger,
                "thirst": o.thirst,
                "health": o.health,
            }
            for rid, o in sorted(state.residents.items())
            if rid != resident_id
        ],
        "rules": {
            "need_threshold": rules.need_threshold,
            "gather_yield": rules.gather_yield,
            "consume_relief": rules.consume_relief,
            "max_share": rules.max_share,
            "max_health": rules.max_health,
        },
    }


def estimate_input_tokens(*texts: str) -> int:
    """Deliberately conservative (over-)estimate used only for pre-call budget checks."""
    return math.ceil(sum(len(t) for t in texts) / 2)


class ResidentAgent:
    def __init__(
        self,
        adapter: ModelAdapter,
        guard: BudgetGuard,
        rules: WorldRules,
        prompt_version: str,
        allowed_actions: tuple[str, ...] = ALL_ACTIONS,
        call_sink: list[tuple[str, CallRecord]] | None = None,
    ) -> None:
        self.call_sink = call_sink
        self.adapter = adapter
        self.guard = guard
        self.rules = rules
        self.prompt_version = prompt_version
        self.allowed_actions = allowed_actions

    def decide(self, run_id: str, state: WorldState, resident_id: str) -> Decision:
        """Budget/kill-switch exceptions propagate to the runner (run must stop)."""
        observation = build_observation(state, resident_id, self.rules)
        instructions, input_text = build_prompt(
            self.prompt_version, observation, self.allowed_actions
        )
        request = DecisionRequest(
            run_id=run_id,
            turn=state.turn,
            resident_id=resident_id,
            prompt_version=self.prompt_version,
            instructions=instructions,
            input_text=input_text,
            observation=observation,
            allowed_actions=self.allowed_actions,
            response_schema=action_json_schema(),
        )
        paid = self.adapter.is_paid
        schema_text = json.dumps(request.response_schema)
        est_in = estimate_input_tokens(instructions, input_text, schema_text) if paid else 0
        max_out = self.adapter.max_output_tokens if paid else 0

        calls: list[CallRecord] = []

        def log(record: CallRecord) -> None:
            calls.append(record)
            if self.call_sink is not None:
                self.call_sink.append((resident_id, record))

        last_error = ""
        for attempt in range(1, self.guard.limits.max_retries + 2):
            self.guard.before_call(paid=paid, est_input_tokens=est_in, max_output_tokens=max_out)
            try:
                response = self.adapter.complete(request)
            except AdapterError as exc:
                # The request may have been processed; count it conservatively.
                cost = self.guard.after_call(paid=paid, input_tokens=est_in, output_tokens=max_out)
                transient = isinstance(exc, TransientAdapterError)
                last_error = redact(str(exc))[:500]
                log(
                    CallRecord(
                        attempt=attempt,
                        outcome="transient_error" if transient else "permanent_error",
                        paid=paid,
                        model=self.adapter.model,
                        input_tokens=est_in,
                        output_tokens=max_out,
                        usage_reported=False,
                        estimated_cost_usd=cost,
                        error=last_error,
                    )
                )
                if transient:
                    continue
                break
            except BaseException as exc:
                # Unexpected failure (bug, interrupt): count the attempt, then stop the run.
                cost = self.guard.after_call(paid=paid, input_tokens=est_in, output_tokens=max_out)
                log(
                    CallRecord(
                        attempt=attempt,
                        outcome="unexpected_error",
                        paid=paid,
                        model=self.adapter.model,
                        input_tokens=est_in,
                        output_tokens=max_out,
                        usage_reported=False,
                        estimated_cost_usd=cost,
                        error=redact(f"{type(exc).__name__}: {exc}")[:500],
                    )
                )
                raise
            cost = self.guard.after_call(
                paid=paid,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            log(
                CallRecord(
                    attempt=attempt,
                    outcome="ok",
                    paid=paid,
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    usage_reported=True,
                    estimated_cost_usd=cost,
                )
            )
            # Parse the original text; only the stored copy is redacted.
            raw = redact(response.raw_output)
            try:
                parsed = parse_action(
                    response.raw_output,
                    actor_id=resident_id,
                    allowed_actions=self.allowed_actions,
                    resident_ids=state.residents.keys(),
                    max_share=self.rules.max_share,
                )
            except ActionValidationError as exc:
                return Decision(resident_id, "rejected_format", None, raw, "", str(exc), calls)
            return Decision(resident_id, "valid", parsed.action, raw, parsed.reason, "ok", calls)

        return Decision(resident_id, "adapter_error", None, None, "", last_error, calls)
