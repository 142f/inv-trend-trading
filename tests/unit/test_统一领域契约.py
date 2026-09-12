from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.backtest.strategy_contracts import (
    EMAContractStrategy,
    TurtleContractStrategy,
)
from inv_trend.domain import BarEvent, OrderIntent, Strategy, order_bar_events


def _ema_bars(count: int = 180) -> pd.DataFrame:
    timestamp = pd.date_range("2024-01-01", periods=count, freq="D", tz="UTC")
    close = 100 + np.sin(np.arange(count) / 9) * 4 + np.arange(count) * 0.03
    return pd.DataFrame({
        "timestamp": timestamp,
        "available_at": timestamp + pd.Timedelta(days=1),
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.full(count, 1000.0),
    })


def test_ema_adapter_implements_strategy_protocol_and_is_causal() -> None:
    strategy = EMAContractStrategy()
    assert isinstance(strategy, Strategy)
    prefix = _ema_bars(175)
    before = strategy.compute_signals(prefix, None, {"symbol": "TEST"})

    future = _ema_bars(180)
    future.loc[175:, "close"] = 1_000_000
    after = strategy.compute_signals(future.iloc[:175], None, {"symbol": "TEST"})

    assert after == before


def test_order_intent_rejects_same_bar_fill() -> None:
    decided = pd.Timestamp("2025-01-01T16:00:00Z")
    with pytest.raises(ValueError, match="cannot fill"):
        OrderIntent("TEST", "OPEN", 1, 1.0, decided, decided)


def test_signal_evidence_is_recursively_immutable() -> None:
    result = EMAContractStrategy().compute_signals(
        _ema_bars(), None, {"symbol": "TEST"}
    )
    with pytest.raises(TypeError):
        result.evidence["states"]["1"] = ()


def test_explicit_clock_orders_close_before_simultaneous_open() -> None:
    when = pd.Timestamp("2025-01-02T00:00:00Z")
    events = (
        BarEvent(when, "open", "B", 2),
        BarEvent(when, "close", "A", 1),
    )
    ordered = order_bar_events(events, explicit_clock=True)
    assert [(item.phase, item.symbol) for item in ordered] == [("close", "A"), ("open", "B")]


def test_turtle_adapter_preserves_latest_observation_time() -> None:
    bars = _ema_bars().set_index("timestamp")
    result = TurtleContractStrategy().compute_signals(
        bars, None, {"symbol": "TEST"}
    )
    assert result.observed_at == bars.index[-1].isoformat()
