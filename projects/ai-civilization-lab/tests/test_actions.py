from __future__ import annotations

import json

import pytest

from civlab.actions import (
    ALL_ACTIONS,
    MAX_RAW_OUTPUT_CHARS,
    ActionType,
    ActionValidationError,
    action_json_schema,
    parse_action,
)

IDS = ("r01", "r02", "r03")


def parse(raw: str, allowed: tuple[str, ...] = ALL_ACTIONS):
    return parse_action(raw, actor_id="r01", allowed_actions=allowed, resident_ids=IDS, max_share=5)


def js(**kw: object) -> str:
    return json.dumps(kw)


def test_valid_simple_action() -> None:
    d = parse(js(action="eat", target=None, resource=None, amount=None, reason="hungry"))
    assert d.action.type is ActionType.EAT
    assert d.reason == "hungry"


def test_valid_share() -> None:
    d = parse(js(action="share", target="r02", resource="water", amount=2, reason=""))
    assert (d.action.target, d.action.resource, d.action.amount) == ("r02", "water", 2)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not json", "invalid_json"),
        ("[1, 2]", "not_an_object"),
        (js(action="eat", extra=1), "unknown_keys:extra"),
        (js(target="r02"), "missing_action"),
        (js(action="fly"), "unknown_action:fly"),
        (js(action="eat", amount=1), "unexpected_argument:amount"),
        (js(action="share", target="r09", resource="food", amount=1), "share_unknown_target"),
        (js(action="share", target="r01", resource="food", amount=1), "share_self_target"),
        (js(action="share", target="r02", resource="gold", amount=1), "share_invalid_resource"),
        (
            js(action="share", target="r02", resource="food", amount=True),
            "share_amount_not_integer",
        ),
        (js(action="share", target="r02", resource="food", amount=1.5), "share_amount_not_integer"),
        (js(action="share", target="r02", resource="food", amount=0), "share_amount_out_of_range"),
        (js(action="share", target="r02", resource="food", amount=6), "share_amount_out_of_range"),
        (js(action="eat", reason=5), "reason_not_string"),
        (js(action="eat", reason="x" * 301), "reason_too_long"),
    ],
)
def test_invalid_outputs_are_rejected(raw: str, reason: str) -> None:
    with pytest.raises(ActionValidationError) as exc:
        parse(raw)
    assert str(exc.value) == reason


def test_action_outside_allowed_set_is_rejected() -> None:
    with pytest.raises(ActionValidationError, match="action_not_allowed:share"):
        parse(js(action="share", target="r02", resource="food", amount=1), allowed=("eat", "rest"))


def test_oversized_output_is_rejected_before_parsing() -> None:
    with pytest.raises(ActionValidationError, match="output_too_long"):
        parse(" " * (MAX_RAW_OUTPUT_CHARS + 1))


def test_schema_lists_every_action_and_requires_all_fields() -> None:
    schema = action_json_schema()
    assert schema["properties"]["action"]["enum"] == list(ALL_ACTIONS)
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_deeply_nested_output_is_rejected_not_crashing() -> None:
    # On Python 3.11 json.loads raises RecursionError here (newer versions parse it and
    # reject it as not an object). Either way it must be a format error, not a crash.
    with pytest.raises(ActionValidationError, match="invalid_json|not_an_object"):
        parse("[" * 999 + "]" * 999)
