"""Single-pass strategy evidence projection shared by execution combinations."""

from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import math
from typing import Any, Mapping

import pandas as pd

from inv_trend.adapters.multi_asset.backtest.data_store import BacktestDataStore
from inv_trend.adapters.multi_asset.models.domain import LONG, AssetSpec, TurtleRules
from inv_trend.core.突破规则 import breakout_direction
from inv_trend.adapters.multi_asset.profiles.asset_profiles import build_asset_specs
from inv_trend.adapters.multi_asset.strategy.engine import MultiAssetTurtleStrategy
from inv_trend.config import canonical_to_backtest_mapping

from .grid import signal_parameters
from .models import BacktestSignalBundle, BacktestSourceBundle, canonical_hash


class ProjectedSignalStrategy:
    """Filter new entries against one immutable, precomputed signal stream."""

    def __init__(
        self,
        base: MultiAssetTurtleStrategy,
        permitted_entries: set[tuple[pd.Timestamp, str, str, int]],
    ) -> None:
        self.base = base
        self.permitted_entries = permitted_entries

    def generate_orders_for_date(
        self,
        date: pd.Timestamp,
        rows_by_symbol: Mapping[str, Mapping[str, Any]],
        state: Any,
        equity: float,
        tradable_symbols: set[str] | None = None,
    ) -> list[Any]:
        orders = self.base.generate_orders(
            rows_by_symbol, state, equity, tradable_symbols=tradable_symbols
        )
        timestamp = _timestamp(date)
        return [
            order for order in orders
            if order.action != "open"
            or (timestamp, order.symbol, order.system, int(order.side)) in self.permitted_entries
        ]


class StrategyReplayProjector:
    """Prepare indicators and formal entry evidence once per signal parameter group."""

    def __init__(self, source: BacktestSourceBundle) -> None:
        self.source = source
        self._cache: dict[str, tuple[BacktestSignalBundle, BacktestDataStore]] = {}

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def bundles(self) -> tuple[BacktestSignalBundle, ...]:
        return tuple(value[0] for _, value in sorted(self._cache.items()))

    def project(
        self,
        data: Mapping[str, pd.DataFrame],
        parameters: Mapping[str, Any],
    ) -> tuple[BacktestSignalBundle, BacktestDataStore, TurtleRules]:
        selected = signal_parameters(parameters)
        parameter_hash = canonical_hash(selected)
        rules = resolve_rules(self.source.strategy_config, parameters)
        cached = self._cache.get(parameter_hash)
        if cached is not None:
            return cached[0], cached[1], rules
        store = BacktestDataStore(data, rules)
        events: list[Mapping[str, Any]] = []
        data_hashes: dict[str, str] = {}
        for symbol, prepared in store.by_symbol.items():
            data_hashes[symbol] = _frame_hash(prepared.bars)
            for position, row in enumerate(prepared.records):
                timestamp = _timestamp(prepared.index[position])
                n = _number(row.get("n"))
                if n is None or n <= 0:
                    continue
                for system, period in (("fast", rules.fast_entry), ("slow", rules.slow_entry)):
                    direction = breakout_direction(row, period, n, buffer_n=rules.breakout_buffer_n,
                                                   trigger_mode=rules.trigger_mode)
                    if direction is None:
                        continue
                    events.append({
                        "symbol": symbol,
                        "timeframe": "D1",
                        "signal_time": timestamp.isoformat(),
                        "system": system,
                        "period": period,
                        "direction": "LONG" if direction == LONG else "SHORT",
                        "side": direction,
                        "signal_type": "TURTLE_CLOSE_BREAKOUT" if rules.trigger_mode == "close" else "TURTLE_INTRADAY_BREAKOUT",
                        "trigger_price": float(row["close"]),
                        "channel_value": float(row[f"high_{period}"] if direction == LONG else row[f"low_{period}"]),
                        "n": n,
                        "source_screening_hash": _screening_hash(self.source, symbol),
                        "source_decision_hash": _decision_hash(self.source, symbol),
                    })
        bundle = BacktestSignalBundle(
            signal_parameter_hash=parameter_hash,
            parameters=selected,
            source_hash=self.source.source_hash,
            event_count=len(events),
            events=tuple(events),
            prepared_data_hashes=data_hashes,
        )
        value = (bundle, store)
        self._cache[parameter_hash] = value
        return bundle, store, rules

    def strategy(
        self,
        bundle: BacktestSignalBundle,
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules,
    ) -> ProjectedSignalStrategy:
        permitted = {
            (
                _timestamp(event["signal_time"]), str(event["symbol"]),
                str(event["system"]), int(event["side"]),
            )
            for event in bundle.events
        }
        return ProjectedSignalStrategy(MultiAssetTurtleStrategy(specs, rules), permitted)


def resolve_rules(
    canonical_config: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> TurtleRules:
    base = dict(canonical_to_backtest_mapping(canonical_config).get("rules", {}))
    for name, value in parameters.items():
        if name.startswith("rules."):
            base[name.removeprefix("rules.")] = value
    allowed = {field.name for field in fields(TurtleRules)}
    unknown = set(base) - allowed
    if unknown:
        raise ValueError(f"unsupported Turtle rule overrides: {sorted(unknown)}")
    return TurtleRules(**base)


def resolve_specs(
    source: BacktestSourceBundle,
    parameters: Mapping[str, Any],
) -> dict[str, AssetSpec]:
    contracts = source.strategy_config.get("risk", {}).get("contracts", {})
    overrides: dict[str, dict[str, Any]] = {}
    by_symbol = {item.symbol: item for item in source.instruments}
    for symbol in by_symbol:
        instrument = by_symbol[symbol]
        raw = contracts.get(instrument.instrument_id, contracts.get(symbol, {}))
        overrides[symbol] = dict(raw) if isinstance(raw, Mapping) else {}
    specs = build_asset_specs(list(by_symbol), overrides)
    for symbol, spec in list(specs.items()):
        # The generic AssetSpec default is one whole contract.  Spot crypto
        # data in the daily pipeline is fractional; using the generic step
        # would silently suppress every BTC trade at the default capital.
        if spec.asset_class == "crypto" and "qty_step" not in overrides[symbol]:
            specs[symbol] = replace(spec, qty_step=0.001, min_qty=0.001)
    for name, value in parameters.items():
        parts = name.split(".")
        if len(parts) == 3 and parts[0] == "assets":
            symbol, field_name = parts[1], parts[2]
            specs[symbol] = replace(specs[symbol], **{field_name: value})
    return specs


def execution_settings(parameters: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    for name, value in parameters.items():
        if name.startswith("execution."):
            result[name.removeprefix("execution.")] = value
    return result


def _screening_hash(source: BacktestSourceBundle, symbol: str) -> str:
    return next(item.screening_result_hash for item in source.instruments if item.symbol == symbol)


def _decision_hash(source: BacktestSourceBundle, symbol: str) -> str:
    return next(item.decision_result_hash for item in source.instruments if item.symbol == symbol)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _frame_hash(frame: pd.DataFrame) -> str:
    values = pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
    return hashlib.sha256(values).hexdigest()


__all__ = [
    "ProjectedSignalStrategy", "StrategyReplayProjector", "execution_settings",
    "resolve_rules", "resolve_specs",
]
