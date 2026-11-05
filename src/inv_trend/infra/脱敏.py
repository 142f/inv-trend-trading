"""Credential redaction shared by every persisting and emitting layer.

Two implementations used to exist: storage.基础.redact knew only a few key names
while observability.脱敏.redact also stripped opaque URLs, bearer tokens and
token/signature fields.  Storage writes manifests, run blobs and experiment
registrations, so the weaker copy let credentials reach audit artifacts.  The
single implementation lives in infra (L0.5) because storage and observability
both need it and neither may import the other.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlsplit

DEFAULT_MAX_LENGTH = 2048

SECRET_NAME = r"api[_ -]?key|access[_ -]?token|token|secret|password|authorization|signature"

_SENSITIVE_KEYS = re.compile(r"(?i)^(?:%s)$" % SECRET_NAME)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_JSON_SECRET = re.compile(r"(?i)([\"'](?:%s)[\"']\s*:\s*)(\"[^\"]*\"|'[^']*')" % SECRET_NAME)
_KEY_VALUE_SECRET = re.compile(r"(?i)\b(%s)(\s*[:=]\s*)[^\s,;]+" % SECRET_NAME)


def _safe_url(match: re.Match) -> str:
    try:
        parsed = urlsplit(match.group(0))
    except ValueError:
        return "[REDACTED_URL]"
    return "%s://%s/[REDACTED]" % (parsed.scheme, parsed.hostname or "unknown")


def redact(value: Any, *, max_length: int | None = DEFAULT_MAX_LENGTH) -> Any:
    """Return a copy with credentials and opaque URLs replaced by [REDACTED].

    Mappings are redacted by key name, sequences element-wise, and strings by
    pattern.  max_length keeps the historical 2048-character cap for emitted
    messages; pass None to preserve long values.
    """

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEYS.match(str(key))
                else redact(item, max_length=max_length)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, max_length=max_length) for item in value]
    if not isinstance(value, str):
        return value
    text = _URL.sub(_safe_url, value)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _JSON_SECRET.sub(r'\1"[REDACTED]"', text)
    text = _KEY_VALUE_SECRET.sub(r"\1\2[REDACTED]", text)
    return text if max_length is None else text[:max_length]


__all__ = ["DEFAULT_MAX_LENGTH", "SECRET_NAME", "redact"]
