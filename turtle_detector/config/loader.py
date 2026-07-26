"""Strict YAML configuration loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..models import AssetConfig, Market, StrategyConfig


def _read_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"missing detector config: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"detector config must contain a mapping: {config_path}")
    return payload


def load_asset_configs(path: str | Path | None = None) -> dict[str, AssetConfig]:
    path = path or Path(__file__).with_name("assets.yaml")
    payload = _read_mapping(path)
    rows = payload.get("assets")
    if not isinstance(rows, list):
        raise ValueError("assets config must contain an assets list")
    configs: dict[str, AssetConfig] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("each asset config must be a mapping")
        values = dict(raw)
        values["market"] = Market(values["market"])
        values["timeframes"] = tuple(str(x).upper() for x in values["timeframes"])
        values["source_symbols"] = tuple(
            str(x) for x in values.get("source_symbols", ())
        )
        config = AssetConfig(**values)
        if config.symbol in configs:
            raise ValueError(f"duplicate asset config: {config.symbol}")
        configs[config.symbol] = config
    return configs


def load_strategy_config(path: str | Path | None = None) -> StrategyConfig:
    path = path or Path(__file__).with_name("strategy.yaml")
    payload = _read_mapping(path)
    raw = payload.get("strategy", payload)
    if not isinstance(raw, dict):
        raise ValueError("strategy config must contain a mapping")
    unknown = set(raw) - set(StrategyConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unsupported detector strategy keys: {sorted(unknown)}")
    return StrategyConfig(**raw)
