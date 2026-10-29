"""Action types and format validation for resident decisions.

Model output is untrusted text. ``parse_action`` turns it into a typed ``Action``
or raises ``ActionValidationError``. It checks format and the allowed action set
only; whether the action is feasible (enough food, target alive, ...) is decided
by the World Engine, which is the only component allowed to change world state.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

RESOURCES: tuple[str, ...] = ("food", "water")

MAX_RAW_OUTPUT_CHARS = 2000
MAX_REASON_CHARS = 300


class ActionType(StrEnum):
    GATHER_FOOD = "gather_food"
    GATHER_WATER = "gather_water"
    EAT = "eat"
    DRINK = "drink"
    SHARE = "share"
    REST = "rest"


ALL_ACTIONS: tuple[str, ...] = tuple(a.value for a in ActionType)

_ALLOWED_KEYS = frozenset({"action", "target", "resource", "amount", "reason"})
_ARG_KEYS = ("target", "resource", "amount")


class ActionValidationError(ValueError):
    """Raised when a proposed action is malformed or not allowed."""


@dataclass(frozen=True)
class Action:
    type: ActionType
    target: str | None = None
    resource: str | None = None
    amount: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "target": self.target,
            "resource": self.resource,
            "amount": self.amount,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Action:
        return cls(
            type=ActionType(data["type"]),
            target=data.get("target"),
            resource=data.get("resource"),
            amount=data.get("amount"),
        )


@dataclass(frozen=True)
class ParsedDecision:
    action: Action
    reason: str


def action_json_schema() -> dict[str, Any]:
    """JSON schema sent to structured-output capable models.

    Every property is required and nullable so the schema also satisfies
    strict structured-output modes. ``parse_action`` re-validates regardless.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": list(ALL_ACTIONS)},
            "target": {"type": ["string", "null"]},
            "resource": {"type": ["string", "null"], "enum": [*RESOURCES, None]},
            "amount": {"type": ["integer", "null"]},
            "reason": {"type": "string"},
        },
        "required": ["action", "target", "resource", "amount", "reason"],
    }


def parse_action(
    raw: str,
    *,
    actor_id: str,
    allowed_actions: Collection[str],
    resident_ids: Collection[str],
    max_share: int,
) -> ParsedDecision:
    """Validate raw model output and return a typed decision.

    Raises ``ActionValidationError`` with a short machine-friendly reason.
    """
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise ActionValidationError("output_too_long")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError: deeply nested input (e.g. "[[[[...]]]]") on some Python versions.
        raise ActionValidationError("invalid_json") from None
    if not isinstance(data, dict):
        raise ActionValidationError("not_an_object")

    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise ActionValidationError(f"unknown_keys:{','.join(sorted(unknown))}")

    name = data.get("action")
    if not isinstance(name, str):
        raise ActionValidationError("missing_action")
    if name not in ALL_ACTIONS:
        raise ActionValidationError(f"unknown_action:{name[:40]}")
    if name not in allowed_actions:
        raise ActionValidationError(f"action_not_allowed:{name}")
    action_type = ActionType(name)

    reason = data.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise ActionValidationError("reason_not_string")
    if len(reason) > MAX_REASON_CHARS:
        raise ActionValidationError("reason_too_long")

    if action_type is not ActionType.SHARE:
        for key in _ARG_KEYS:
            if data.get(key) is not None:
                raise ActionValidationError(f"unexpected_argument:{key}")
        return ParsedDecision(Action(action_type), reason)

    target = data.get("target")
    resource = data.get("resource")
    amount = data.get("amount")
    if not isinstance(target, str) or target not in resident_ids:
        raise ActionValidationError("share_unknown_target")
    if target == actor_id:
        raise ActionValidationError("share_self_target")
    if resource not in RESOURCES:
        raise ActionValidationError("share_invalid_resource")
    # bool is a subclass of int; reject it explicitly.
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ActionValidationError("share_amount_not_integer")
    if not 1 <= amount <= max_share:
        raise ActionValidationError("share_amount_out_of_range")
    return ParsedDecision(
        Action(ActionType.SHARE, target=target, resource=resource, amount=amount),
        reason,
    )
