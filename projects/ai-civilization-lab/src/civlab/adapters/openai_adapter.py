"""OpenAI Responses API adapter (paid; disabled by default).

Construction fails unless ALL of the following hold:

1. the experiment config sets ``[openai].allow_paid_api = true``;
2. the environment sets ``CIVLAB_ALLOW_PAID_API=yes``;
3. ``OPENAI_API_KEY`` is set (only read from the environment, never from files
   in the repository);
4. the ``openai`` SDK is installed (``civlab[openai]`` extra).

The Budget Guard additionally refuses paid calls unless pricing and a paid-call
allowance are configured. The SDK's own retries are disabled (``max_retries=0``)
so that every attempt is counted by the Budget Guard.

Verified against openai-python 3.41.0: ``OpenAI(api_key, timeout, max_retries,
http_client)``, ``client.responses.create(model, instructions, input,
max_output_tokens, text, store, temperature)``, ``Response.output_text``,
``Response.usage.input_tokens/output_tokens``. Model names and prices are not
hard-coded; they come from the experiment config.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from civlab.adapters.base import (
    AdapterError,
    AdapterResponse,
    DecisionRequest,
    PaidApiDisabledError,
    PermanentAdapterError,
    ReproClass,
    TransientAdapterError,
    Usage,
)
from civlab.redaction import redact

ENABLE_ENV = "CIVLAB_ALLOW_PAID_API"
ENABLE_VALUE = "yes"
API_KEY_ENV = "OPENAI_API_KEY"

_TRANSIENT_ERRORS = frozenset(
    {"APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError"}
)


@dataclass(frozen=True)
class OpenAISettings:
    model: str = ""
    allow_paid_api: bool = False
    timeout_seconds: float = 30.0
    max_output_tokens: int = 300
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allow_paid_api, bool):
            raise ValueError("allow_paid_api must be a boolean (true/false)")
        if not isinstance(self.model, str):
            raise ValueError("model must be a string")
        for name in ("timeout_seconds", "temperature"):
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int | float)):
                raise ValueError(f"{name} must be a number")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        mot = self.max_output_tokens
        if isinstance(mot, bool) or not isinstance(mot, int) or mot < 1:
            raise ValueError("max_output_tokens must be a positive integer")


def paid_api_blockers(
    settings: OpenAISettings, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Reasons why a paid call is not possible right now (empty list = enabled)."""
    env = os.environ if environ is None else environ
    reasons = []
    if not settings.allow_paid_api:
        reasons.append("config [openai].allow_paid_api is false")
    if env.get(ENABLE_ENV) != ENABLE_VALUE:
        reasons.append(f"environment variable {ENABLE_ENV} is not '{ENABLE_VALUE}'")
    if not env.get(API_KEY_ENV):
        reasons.append(f"environment variable {API_KEY_ENV} is not set")
    if not settings.model:
        reasons.append("config [agent].model is empty")
    return reasons


class OpenAIAdapter:
    def __init__(
        self,
        settings: OpenAISettings,
        *,
        client: Any | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        blockers = paid_api_blockers(settings, env)
        if blockers:
            raise PaidApiDisabledError("paid API disabled: " + "; ".join(blockers))
        self.settings = settings
        if client is None:
            try:
                import openai
            except ImportError as exc:
                raise PermanentAdapterError(
                    "openai SDK not installed; install the 'openai' extra"
                ) from exc
            client = openai.OpenAI(
                api_key=env[API_KEY_ENV],
                max_retries=0,
                timeout=settings.timeout_seconds,
            )
        self._client = client

    def __repr__(self) -> str:
        return f"OpenAIAdapter(model={self.settings.model!r})"

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def is_paid(self) -> bool:
        return True

    @property
    def reproducibility_class(self) -> ReproClass:
        return ReproClass.B

    @property
    def max_output_tokens(self) -> int:
        return self.settings.max_output_tokens

    def complete(self, request: DecisionRequest) -> AdapterResponse:
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "instructions": request.instructions,
            "input": request.input_text,
            "max_output_tokens": self.settings.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "resident_action",
                    "schema": request.response_schema,
                    "strict": True,
                }
            },
        }
        if self.settings.temperature is not None:
            kwargs["temperature"] = self.settings.temperature
        try:
            response = self._client.responses.create(**kwargs)
        except Exception as exc:
            raise classify_error(exc) from exc

        try:
            usage = response.usage
            return AdapterResponse(
                raw_output=str(getattr(response, "output_text", "") or ""),
                usage=Usage(int(usage.input_tokens), int(usage.output_tokens)),
                model=str(getattr(response, "model", self.settings.model)),
                status=str(getattr(response, "status", "unknown")),
            )
        except Exception as exc:
            # The call was made; the guard then counts it conservatively.
            raise PermanentAdapterError(
                redact(f"unusable response ({type(exc).__name__}): {exc}")[:500]
            ) from exc


def classify_error(exc: BaseException) -> AdapterError:
    names = {cls.__name__ for cls in type(exc).__mro__}
    message = redact(f"{type(exc).__name__}: {exc}")[:500]
    if names & _TRANSIENT_ERRORS:
        return TransientAdapterError(message)
    return PermanentAdapterError(message)
