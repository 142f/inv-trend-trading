"""Load and verify immutable upstream inputs and versioned market data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from inv_trend.config import load_strategy_mapping
from inv_trend.data.lineage import load_versioned_lineage
from inv_trend.data.storage import DataLake

from .models import BacktestPlan, BacktestSourceBundle, SourceInstrument


def load_source_bundle(source_run: str | Path, plan: BacktestPlan) -> BacktestSourceBundle:
    root = Path(source_run).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"source run directory is missing: {root}")
    batch_index = _read_optional(root / "批次索引.json") or _read_optional(root / "run_index.json")
    source_run_id = str((batch_index or {}).get("run_id") or root.name)
    instruments: list[SourceInstrument] = []
    v3_path = root / "审计" / "运行清单_v3.json"
    v3 = None
    if v3_path.exists():
        from inv_trend.storage import Storage
        from inv_trend.storage.仓库 import project_root
        store = Storage(project_root(root))
        v3 = store.manifest(root.name)
        store._verify_manifest(v3)
    for symbol in plan.symbols:
        canonical = root / symbol / "01_canonical"
        if v3 is not None:
            name = f"输入/{symbol}/完整分析.json"
            entry = next(f for f in v3["files"] if f["logical_name"] == name)
            complete = _read_json(store.root / entry["path"])
            row = complete["storage_source_row"]
            from inv_trend.adapters.daily.artifact_publisher import _stage
            data, screening, decision = (_stage(row, key) for key in ("data_update", "strategy_screening", "trend_decision"))
        elif not canonical.is_dir():
            raise FileNotFoundError(f"source run has no canonical directory for {symbol}: {canonical}")
        else:
            data = _read_json(canonical / "data_update_result.json")
            screening = _read_json(canonical / "strategy_screening_result.json")
            decision = _read_json(canonical / "trend_decision_result.json")
        _require_identity(symbol, data, screening, decision)
        version = str(data.get("dataset_version") or "")
        if not version:
            raise ValueError(f"source data result has no dataset_version for {symbol}")
        signals = _formal_signals(screening, decision)
        instruments.append(
            SourceInstrument(
                symbol=symbol,
                instrument_id=str(data.get("instrument_id") or symbol),
                timeframe=str(data.get("timeframe") or "D1").upper(),
                dataset_version=version,
                data_result_hash=_result_hash(data, "data update", symbol),
                screening_result_hash=_result_hash(screening, "screening", symbol),
                decision_result_hash=_result_hash(decision, "decision", symbol),
                formal_signals=tuple(signals),
            )
        )
    config_path = plan.strategy_config_path
    strategy_config = load_strategy_mapping(config_path)
    strategy_version = str(
        strategy_config.get("risk", {}).get("runtime", {}).get("strategy_version", "corrected-v2")
    )
    code_version = str(
        strategy_config.get("risk", {}).get("runtime", {}).get("code_version", "unknown")
    )
    return BacktestSourceBundle(
        source_run_id=source_run_id,
        source_run_path=str(root),
        strategy_version=strategy_version,
        instruments=tuple(instruments),
        strategy_config=strategy_config,
        strategy_id=plan.strategy_id,
        code_version=code_version,
    )


def load_versioned_data(
    source: BacktestSourceBundle,
    data_root: str | Path,
) -> tuple[dict[str, pd.DataFrame], dict[str, Mapping[str, Any]]]:
    lake = DataLake(data_root)
    data: dict[str, pd.DataFrame] = {}
    lineage: dict[str, Mapping[str, Any]] = {}
    for instrument in source.instruments:
        verified = load_versioned_lineage(
            lake, instrument.symbol, instrument.timeframe, instrument.dataset_version
        )
        frame = pd.read_parquet(verified.curated_path)
        timestamp_column = next(
            (name for name in ("timestamp", "time", "date", "datetime") if name in frame),
            None,
        )
        if timestamp_column is not None:
            index = pd.to_datetime(frame.pop(timestamp_column), utc=True, errors="raise")
            frame.index = pd.DatetimeIndex(index, name="timestamp")
        elif not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError(f"versioned bars have no time index for {instrument.symbol}")
        elif frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
        if "is_complete" in frame:
            complete = frame["is_complete"]
            if pd.api.types.is_bool_dtype(complete) or pd.api.types.is_numeric_dtype(complete):
                mask = complete.fillna(False).astype(bool)
            else:
                mask = complete.astype("string").str.strip().str.lower().isin(
                    {"1", "true", "yes", "y"}
                )
            frame = frame.loc[mask].copy()
        frame.attrs["dataset_version"] = instrument.dataset_version
        data[instrument.symbol] = frame.sort_index()
        lineage[instrument.symbol] = {
            "dataset_version": instrument.dataset_version,
            "dataset_manifest": str(verified.dataset_manifest_path),
            "curated_path": str(verified.curated_path),
            "curated_sha256": verified.dataset_manifest.get("curated_sha256"),
            "row_count": int(len(frame)),
            "first": frame.index[0].isoformat() if len(frame) else None,
            "last": frame.index[-1].isoformat() if len(frame) else None,
        }
    return data, lineage


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"required source artifact is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"source artifact must contain a JSON object: {path}")
    return value


def _read_optional(path: Path) -> dict[str, Any] | None:
    return _read_json(path) if path.is_file() else None


def _result_hash(payload: Mapping[str, Any], label: str, symbol: str) -> str:
    value = str(payload.get("result_hash") or "")
    if len(value) != 64:
        raise ValueError(f"{label} result hash is invalid for {symbol}")
    return value


def _require_identity(symbol: str, *payloads: Mapping[str, Any]) -> None:
    for payload in payloads:
        if str(payload.get("symbol") or "").upper() != symbol:
            raise ValueError(f"source artifact identity mismatch for {symbol}")
        if str(payload.get("timeframe") or "D1").upper() != "D1":
            raise ValueError(f"strategy backtest currently requires D1 input for {symbol}")


def _formal_signals(
    screening: Mapping[str, Any], decision: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("raw_events", "turtle_breakouts"):
        value = screening.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    projection = decision.get("commit_projection")
    if isinstance(projection, Mapping):
        value = projection.get("formal_signal_events")
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    return rows


__all__ = ["load_source_bundle", "load_versioned_data"]
