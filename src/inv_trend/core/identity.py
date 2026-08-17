"""Stable identifiers for reproducible strategy runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from uuid import uuid4
from typing import Any, Mapping


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RunId:
    value: str

    @classmethod
    def create(cls) -> "RunId":
        return cls(uuid4().hex)


@dataclass(frozen=True)
class ConfigFingerprint:
    value: str

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "ConfigFingerprint":
        return cls(_fingerprint(config))


@dataclass(frozen=True)
class DataFingerprint:
    value: str

    @classmethod
    def from_mapping(cls, manifest: Mapping[str, Any]) -> "DataFingerprint":
        return cls(_fingerprint(manifest))
