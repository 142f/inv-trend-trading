"""Deterministic JSON container helpers shared across application stages."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .events import DecisionEvent


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    return str(value)


def serialize_event(event: DecisionEvent) -> dict[str, Any]:
    return {
        "event_schema_version": event.event_schema_version,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "event_time": event.event_time,
        "bar_time": event.bar_time,
        "symbol": event.symbol,
        "event_type": _serialize(event.event_type),
        "stage": event.stage,
        "action": event.action,
        "reason_code": event.reason_code,
        "message": event.message,
        "metrics": _serialize(event.metrics),
        "rule_snapshot_hash": event.rule_snapshot_hash,
        "data_fingerprint": event.data_fingerprint,
        "context": _serialize(event.context),
    }


def freeze_json(value: Any) -> Any:
    """Detach a JSON-like value from mutable caller-owned containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Convert a frozen JSON-like value back to ordinary containers."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize a mapping to stable UTF-8 bytes suitable for hashing."""

    return json.dumps(
        thaw_json(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


__all__ = ["canonical_json_bytes", "freeze_json", "serialize_event", "thaw_json"]
