"""Secret redaction for logs and persisted free text."""

from __future__ import annotations

import logging
import os
import re

REDACTED = "[REDACTED]"

# Environment variables whose values must never appear in logs or the store.
SECRET_ENV_VARS: tuple[str, ...] = ("OPENAI_API_KEY",)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{8,}=*"),
    re.compile(
        r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password))\b"
        r"(\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\3"
    ),
)


def redact(text: str) -> str:
    """Mask known secret shapes and the current values of secret env vars."""
    for var in SECRET_ENV_VARS:
        value = os.environ.get(var, "")
        if len(value) >= 8:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
            del exc
        return True
