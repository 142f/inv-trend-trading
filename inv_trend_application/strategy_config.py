"""Resolved strategy configuration independent of data-source identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class ResolvedRunConfig:
    """Immutable execution contract after profile/default resolution.

    Instrument identity remains owned by ``historical_data``; this model only
    carries strategy, risk, and contract overrides keyed by instrument ID.
    """

    profile: str = "corrected-v2"
    rules: Mapping[str, Any] = field(default_factory=dict)
    risk: Mapping[str, Any] = field(default_factory=dict)
    contracts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("rules", "risk", "contracts"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{field_name} must be a mapping")
            object.__setattr__(self, field_name, MappingProxyType(dict(value)))


def load_resolved_run_config(path: str | Path) -> ResolvedRunConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"strategy config must contain a mapping: {config_path}")
    allowed = set(ResolvedRunConfig.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported strategy configuration keys: {sorted(unknown)}")
    return ResolvedRunConfig(**raw)
