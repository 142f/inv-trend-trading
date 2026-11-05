"""Thin re-export of the shared redaction implementation.

The rules used to live here, which made the storage layer (manifests, run blobs,
experiment registrations) apply a weaker rule set.  The implementation now lives
in infra.脱敏; this module keeps the historical import path for monitoring,
notification and CLI callers.
"""
from __future__ import annotations

from inv_trend.infra.脱敏 import DEFAULT_MAX_LENGTH, SECRET_NAME, redact

__all__ = ["DEFAULT_MAX_LENGTH", "SECRET_NAME", "redact"]
