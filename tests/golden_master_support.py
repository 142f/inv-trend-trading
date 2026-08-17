"""Stable serialization and comparison helpers for the D1 Golden Master.

The production paths deliberately stay outside this module.  Its sole job is
to make the test fixture reviewable: it removes generated identifiers, turns
Pandas/NumPy values into JSON values, and compares floats at the documented
relative tolerance without weakening ordering or event identity checks.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from inv_trend.adapters.detector.backtest.engine import DetectorBacktester
from inv_trend.adapters.detector.indicators.daily_signals import analyze_daily_signals
from inv_trend.adapters.detector.models import AssetConfig, Market, StrategyConfig
from inv_trend.adapters.multi_asset import AssetSpec, TurtleBacktester, TurtleRules
from inv_trend.core.features import FeatureRequest, PreparedBars


FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_NAME = "golden_d1.csv"
EXPECTED_NAME = "golden_d1_expected.json"
FLOAT_REL_TOLERANCE = 1e-10
_VOLATILE_KEYS = frozenset({"generated_at", "intent_id"})
_FEATURE_COLUMNS = (
    "atr",
    "channel_high_20",
    "channel_low_20",
    "channel_high_55",
    "channel_low_55",
    "sma_10_lag_0",
    "sma_20_lag_0",
)
_BASELINE_FEATURE_COLUMNS = _FEATURE_COLUMNS[:5]


def fixture_path() -> Path:
    return FIXTURES / FIXTURE_NAME


def expected_path() -> Path:
    return FIXTURES / EXPECTED_NAME


def load_bars(path: Path | None = None) -> pd.DataFrame:
    """Read the static CSV fixture into the strict OHLCV feature boundary."""

    csv_path = path or fixture_path()
    return pd.read_csv(csv_path, parse_dates=["timestamp"]).set_index("timestamp")


def fixture_sha256(path: Path | None = None) -> str:
    return hashlib.sha256((path or fixture_path()).read_bytes()).hexdigest()


def detector_asset() -> AssetConfig:
    """Use the actual published BTC data identity, not a synthetic alias."""

    return AssetConfig(
        symbol="BTC",
        instrument="BTCUSDT.BINANCE.SPOT",
        market=Market.CRYPTO,
        data_source="binance",
        timeframes=("D1",),
        adjustment="none",
        allow_short=False,
    )


def detector_config() -> StrategyConfig:
    return StrategyConfig(
        atr_period=20,
        system1_entry=20,
        system2_entry=55,
        system1_exit=10,
        system2_exit=20,
        volatility_lookback=60,
        trend_ma_period=20,
        long_trend_ma_period=55,
        false_breakout_bars=3,
    )


def turtle_rules() -> TurtleRules:
    return TurtleRules(
        n_period=20,
        fast_entry=20,
        slow_entry=55,
        fast_exit=10,
        slow_exit=20,
        skip_fast_after_win=True,
        max_total_1n_risk_pct=0.12,
    )


def _curve_records(curve: pd.Series) -> list[dict[str, object]]:
    return [
        {"timestamp": pd.Timestamp(timestamp).isoformat(), "equity": float(value)}
        for timestamp, value in curve.items()
    ]


def _stable_order_records(orders: pd.DataFrame) -> list[dict[str, object]]:
    return orders.drop(columns=["intent_id"], errors="ignore").to_dict("records")


def canonical(value: Any) -> Any:
    """Return a JSON-safe, behavior-bearing value.

    ``generated_at`` and ``intent_id`` identify a run rather than a strategy
    decision, so they are intentionally excluded from Golden identity.  Every
    other key, event order, and list item remains in the comparison.
    """

    if isinstance(value, Mapping):
        return {
            str(key): canonical(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_KEYS
        }
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    if isinstance(value, pd.Timestamp):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return timestamp.isoformat()
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if pd.isna(value) if not isinstance(value, (str, bytes)) else False:
        return None
    return value


def stable_digest(value: Any) -> str:
    """Audit-only digest of the canonical payload (not a float-equality gate)."""

    payload = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_current_snapshot(bars: pd.DataFrame | None = None) -> dict[str, Any]:
    """Evaluate every current D1 path covered by the Golden Master once."""

    bars = load_bars() if bars is None else bars
    prepared = PreparedBars.build(
        bars,
        FeatureRequest.turtle(
            atr_period=20,
            channel_periods=(20, 55),
            sma_lags=((10, 0), (20, 0)),
        ),
    ).frame
    detector = DetectorBacktester(
        detector_config(), initial_equity=100_000, cost_bps=5
    ).run(bars, detector_asset(), "D1")
    multi = TurtleBacktester(
        {"BTC": bars},
        {
            "BTC": AssetSpec(
                "BTC", "crypto", "crypto", qty_step=0.001, can_short=False
            )
        },
        turtle_rules(),
        initial_equity=100_000,
    ).run()
    return canonical(
        {
            "feature_tail": {
                column: float(prepared.iloc[-1][column]) for column in _FEATURE_COLUMNS
            },
            "daily": analyze_daily_signals(bars),
            "detector_events": [
                {
                    key: value
                    for key, value in event.items()
                    if key
                    in {
                        "signal_type",
                        "raw_signal_type",
                        "direction",
                        "signal_time",
                        "trigger_price",
                        "tradeable",
                        "filtered_reasons",
                    }
                }
                for event in detector.signals.to_dict("records")
            ],
            "detector_trades": detector.trades.to_dict("records"),
            "detector_equity": _curve_records(detector.equity_curve),
            "detector_metrics": detector.metrics,
            "multi_orders": _stable_order_records(multi.orders),
            "multi_trades": multi.trades.to_dict("records"),
            "multi_equity": _curve_records(multi.equity_curve),
            "multi_metrics": multi.metrics,
        }
    )


def baseline_comparable_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The behavior shared with Git 121f901 before Daily Scanner existed."""

    return {
        "feature_tail": {
            column: snapshot["feature_tail"][column] for column in _BASELINE_FEATURE_COLUMNS
        },
        "detector_events": snapshot["detector_events"],
        "detector_trades": snapshot["detector_trades"],
        "detector_equity": snapshot["detector_equity"],
        "detector_metrics": snapshot["detector_metrics"],
        "multi_orders": snapshot["multi_orders"],
        "multi_trades": snapshot["multi_trades"],
        "multi_equity": snapshot["multi_equity"],
        "multi_metrics": snapshot["multi_metrics"],
    }


def first_difference(
    actual: Any,
    expected: Any,
    path: str = "$",
    *,
    rel_tolerance: float = FLOAT_REL_TOLERANCE,
) -> str | None:
    """Return the first exact structural or tolerance-aware numeric difference."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return f"{path}: expected mapping, got {type(actual).__name__}"
        expected_keys = list(expected)
        actual_keys = list(actual)
        if set(actual_keys) != set(expected_keys):
            missing = sorted(set(expected_keys) - set(actual_keys))
            unexpected = sorted(set(actual_keys) - set(expected_keys))
            return f"{path}: keys differ; missing={missing}, unexpected={unexpected}"
        for key in expected_keys:
            difference = first_difference(
                actual[key], expected[key], f"{path}.{key}", rel_tolerance=rel_tolerance
            )
            if difference:
                return difference
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return f"{path}: expected list, got {type(actual).__name__}"
        if len(actual) != len(expected):
            return f"{path}: length differs; expected={len(expected)}, got={len(actual)}"
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            difference = first_difference(
                actual_item,
                expected_item,
                f"{path}[{index}]",
                rel_tolerance=rel_tolerance,
            )
            if difference:
                return difference
        return None
    if isinstance(expected, bool) or isinstance(actual, bool):
        if actual != expected:
            return f"{path}: expected={expected!r}, got={actual!r}"
        return None
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if not math.isclose(float(actual), float(expected), rel_tol=rel_tolerance, abs_tol=0.0):
            return f"{path}: expected={expected!r}, got={actual!r} (rel_tol={rel_tolerance})"
        return None
    if actual != expected:
        return f"{path}: expected={expected!r}, got={actual!r}"
    return None


def expected_payload(
    snapshot: Mapping[str, Any],
    *,
    input_sha256: str,
    certification: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a human-reviewable, re-certified expected payload."""

    hashes = {key: stable_digest(value) for key, value in snapshot.items()}
    return {
        "schema_version": 2,
        "fixture": {
            "instrument_id": "BTCUSDT.BINANCE.SPOT",
            "symbol": "BTC",
            "market": "binance_spot",
            "data_source": "binance",
            "timeframe": "D1",
            "dataset_version": "028585a633484f05948afa5f",
            "start": "2022-01-01T00:00:00+00:00",
            "end": "2023-02-24T00:00:00+00:00",
            "bars": 420,
            "line_endings": "LF",
        },
        "input_sha256": input_sha256,
        "certification": dict(certification),
        "hashes": hashes,
        "outputs": canonical(dict(snapshot)),
    }
