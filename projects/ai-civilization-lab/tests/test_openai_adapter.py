"""OpenAI adapter: gating, and request/response handling against the real SDK
through an in-process fake HTTP transport (no network, no real key, no cost)."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import openai
import pytest

from civlab.actions import action_json_schema
from civlab.adapters.base import (
    DecisionRequest,
    PaidApiDisabledError,
    PermanentAdapterError,
    TransientAdapterError,
)
from civlab.adapters.openai_adapter import OpenAIAdapter, OpenAISettings, paid_api_blockers
from civlab.budget import BudgetLimits, Pricing
from civlab.config import ExperimentConfig
from civlab.evaluation import summarize_run, verify_run
from civlab.runner import SimulationRunner, build_adapter

from .conftest import PROVENANCE

FAKE_KEY = "sk-test-0000000000000000000000000000"
ENABLED_ENV = {"CIVLAB_ALLOW_PAID_API": "yes", "OPENAI_API_KEY": FAKE_KEY}
SETTINGS = OpenAISettings(model="test-model", allow_paid_api=True, max_output_tokens=100)
DECISION = {
    "action": "gather_food",
    "target": None,
    "resource": None,
    "amount": None,
    "reason": "stock",
}


def response_body(text: str, input_tokens: int = 120, output_tokens: int = 30) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


class Recorder:
    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body or response_body(json.dumps(DECISION))
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body)


def sdk_client(recorder: Recorder) -> openai.OpenAI:
    return openai.OpenAI(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(recorder)),
    )


def request() -> DecisionRequest:
    return DecisionRequest(
        run_id="run-x",
        turn=0,
        resident_id="r01",
        prompt_version="resident-v0.1",
        instructions="instr",
        input_text="{}",
        observation={},
        allowed_actions=("eat",),
        response_schema=action_json_schema(),
    )


def test_all_gates_closed_by_default() -> None:
    blockers = paid_api_blockers(OpenAISettings(), environ={})
    assert len(blockers) == 4


@pytest.mark.parametrize(
    ("settings", "env", "missing"),
    [
        (OpenAISettings(model="m", allow_paid_api=False), ENABLED_ENV, "allow_paid_api"),
        (SETTINGS, {"OPENAI_API_KEY": FAKE_KEY}, "CIVLAB_ALLOW_PAID_API"),
        (
            SETTINGS,
            {"CIVLAB_ALLOW_PAID_API": "true", "OPENAI_API_KEY": FAKE_KEY},
            "CIVLAB_ALLOW_PAID_API",
        ),
        (SETTINGS, {"CIVLAB_ALLOW_PAID_API": "yes"}, "OPENAI_API_KEY"),
    ],
)
def test_each_gate_blocks_construction(settings, env, missing) -> None:
    with pytest.raises(PaidApiDisabledError, match=missing):
        OpenAIAdapter(settings, environ=env, client=object())


def test_build_adapter_refuses_openai_without_enablement() -> None:
    config = ExperimentConfig(experiment_id="x", adapter="openai", model="test-model")
    with pytest.raises(PaidApiDisabledError):
        build_adapter(config)


def test_request_shape_with_real_sdk() -> None:
    rec = Recorder()
    adapter = OpenAIAdapter(SETTINGS, environ=ENABLED_ENV, client=sdk_client(rec))
    resp = adapter.complete(request())
    assert json.loads(resp.raw_output) == DECISION
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (120, 30)
    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent.method == "POST"
    assert sent.url.path.endswith("/responses")
    body = json.loads(sent.content)
    assert body["model"] == "test-model"
    assert body["store"] is False
    assert body["max_output_tokens"] == 100
    assert body["instructions"] == "instr"
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert fmt["schema"] == action_json_schema()
    assert "temperature" not in body


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, TransientAdapterError),
        (500, TransientAdapterError),
        (400, PermanentAdapterError),
        (401, PermanentAdapterError),
    ],
)
def test_http_errors_are_classified(status, error) -> None:
    rec = Recorder(status=status, body={"error": {"message": "x", "type": "x"}})
    adapter = OpenAIAdapter(SETTINGS, environ=ENABLED_ENV, client=sdk_client(rec))
    with pytest.raises(error):
        adapter.complete(request())
    assert len(rec.requests) == 1  # SDK retries are disabled; the guard counts attempts


def test_error_messages_are_redacted() -> None:
    rec = Recorder(status=401, body={"error": {"message": f"Incorrect API key {FAKE_KEY}"}})
    adapter = OpenAIAdapter(SETTINGS, environ=ENABLED_ENV, client=sdk_client(rec))
    with pytest.raises(PermanentAdapterError) as exc:
        adapter.complete(request())
    assert FAKE_KEY not in str(exc.value)


def test_repr_has_no_key() -> None:
    adapter = OpenAIAdapter(SETTINGS, environ=ENABLED_ENV, client=object())
    assert FAKE_KEY not in repr(adapter)


def test_full_paid_run_through_fake_transport(store) -> None:
    """Exercises the whole paid path end to end; nothing leaves the process."""
    rec = Recorder()
    adapter = OpenAIAdapter(SETTINGS, environ=ENABLED_ENV, client=sdk_client(rec))
    config = ExperimentConfig(
        experiment_id="openai-fake",
        adapter="openai",
        model="test-model",
        turns=2,
        budget=BudgetLimits(
            max_paid_calls=12,
            max_input_tokens=10**6,
            max_output_tokens=10**6,
            max_estimated_cost_usd=0.5,
        ),
        pricing=Pricing(
            1.0, 4.0, source="test fixture (not a real price)", checked_at="2026-10-29"
        ),
    )
    result = SimulationRunner(config, store, adapter=adapter, provenance=PROVENANCE).run()
    assert result.status == "completed"
    assert len(rec.requests) == 12
    summary = summarize_run(store, result.run_id)
    assert summary["reproducibility_class"] == "B"
    assert summary["usage"]["paid_calls_recorded"] == 12
    assert summary["usage"]["estimated_cost_usd"] == pytest.approx(12 * (120 + 30 * 4) / 1e6)
    assert summary["usage"]["actual_cost_usd"] is None
    assert verify_run(store, result.run_id)["ok"]
