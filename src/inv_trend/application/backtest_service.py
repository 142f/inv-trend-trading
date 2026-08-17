"""Application boundary for reproducible Turtle backtests."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from inv_trend.core.identity import ConfigFingerprint, DataFingerprint, RunId
from inv_trend.application.manifest import StrategyRunManifest
from inv_trend.adapters.multi_asset.backtest.runner import BacktestResult, TurtleBacktester
from inv_trend.adapters.multi_asset.config.settings import BacktestConfig
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, TurtleRules


@dataclass(frozen=True)
class BacktestRun:
    """Backtest result paired with the immutable inputs needed to reproduce it."""

    result: BacktestResult
    manifest: StrategyRunManifest


class BacktestService:
    """Compose data, core execution, and optional run-manifest persistence."""

    def run(
        self,
        data: Mapping[str, pd.DataFrame],
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules,
        *,
        config: BacktestConfig | None = None,
        evaluation_start: str | pd.Timestamp | None = None,
        manifest_path: str | Path | None = None,
        mode: str = "backtest",
    ) -> BacktestRun:
        resolved = config or BacktestConfig()
        result = TurtleBacktester(
            data=data,
            specs=specs,
            rules=rules,
            config=resolved,
            evaluation_start=evaluation_start,
        ).run()
        config_payload = {
            "config": _mapping(resolved),
            "rules": _mapping(rules),
            "specs": {symbol: _mapping(spec) for symbol, spec in sorted(specs.items())},
            "evaluation_start": None if evaluation_start is None else str(evaluation_start),
        }
        data_payload = {
            symbol: {
                "rows": len(frame),
                "first": frame.index[0].isoformat() if len(frame) else None,
                "last": frame.index[-1].isoformat() if len(frame) else None,
                "fingerprint": str(frame.attrs.get("dataset_version", "")),
            }
            for symbol, frame in sorted(data.items())
        }
        manifest = StrategyRunManifest(
            run_id=RunId.create().value,
            code_version=resolved.code_version,
            config_fingerprint=ConfigFingerprint.from_mapping(config_payload).value,
            data_fingerprint=DataFingerprint.from_mapping(data_payload).value,
            mode=mode,
            metadata={"strategy_version": resolved.strategy_version},
        )
        if manifest_path is not None:
            manifest.write(manifest_path)
        return BacktestRun(result=result, manifest=manifest)


def _mapping(value: object) -> dict[str, object]:
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    return dict(_plain(value))  # type: ignore[arg-type]


def _plain(value: object) -> object:
    """Serialize frozen mappings without mutating their domain representation."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if is_dataclass(value):
        return _mapping(value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
