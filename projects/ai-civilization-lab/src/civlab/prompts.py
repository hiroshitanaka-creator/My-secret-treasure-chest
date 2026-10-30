"""Versioned prompt templates for resident decisions.

Changing the wording of a template requires a new version string so that runs
remain attributable to the exact prompt they used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

PROMPT_VERSION = "resident-v0.1"

_INSTRUCTIONS = {
    "resident-v0.1": (
        "You are a resident of a small simulated village. Each turn you choose exactly "
        "one action. Food and water are scarce; hunger and thirst grow every turn and "
        "damage health once they reach the threshold. Reply with a single JSON object "
        "matching the provided schema. Use null for arguments the action does not need. "
        "Only the actions listed in allowed_actions are accepted."
    ),
}


def available_versions() -> tuple[str, ...]:
    return tuple(_INSTRUCTIONS)


def build_prompt(
    version: str, observation: dict[str, Any], allowed_actions: tuple[str, ...]
) -> tuple[str, str]:
    """Return ``(instructions, input_text)`` for the given prompt version."""
    if version not in _INSTRUCTIONS:
        raise ValueError(f"unknown prompt version: {version}")
    payload = {"allowed_actions": list(allowed_actions), "observation": observation}
    return _INSTRUCTIONS[version], json.dumps(payload, sort_keys=True, ensure_ascii=True)


def prompt_fingerprint(version: str) -> str:
    """Hash of the template text, recorded with each run."""
    return hashlib.sha256(_INSTRUCTIONS[version].encode("utf-8")).hexdigest()[:16]
