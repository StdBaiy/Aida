"""Secret redaction for locally persisted trace payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"^(?:api[_-]?key|authorization|cookie|credentials?|password|secret|"
    r"(?:access|refresh|auth)[_-]?token|token)$",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*"
        r"([\"']?)[^\s,\"']+\2"
    ),
)
_REDACTED = "[REDACTED]"


def redact(value: Any, *, secrets: Sequence[str] = ()) -> Any:
    """Return a JSON-friendly copy with credentials replaced."""
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED
            if _SENSITIVE_KEYS.search(str(key))
            else redact(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, secrets=secrets) for item in value]
    if isinstance(value, bytes):
        return _redact_text(value.decode(errors="replace"), secrets)
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return redact(value.model_dump(mode="json"), secrets=secrets)
        except (TypeError, ValueError):
            pass
    return _redact_text(str(value), secrets)


def _redact_text(value: str, secrets: Sequence[str]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            (lambda match: f"{match.group(1)}={_REDACTED}") if pattern.groups else _REDACTED,
            result,
        )
    return result
