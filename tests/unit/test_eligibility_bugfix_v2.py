from __future__ import annotations

import pandas as pd

from inv_trend.adapters.detector.eligibility.checker import TradeEligibilityChecker
from inv_trend.adapters.detector.eligibility.models import (
    BlockedReason,
    EligibilityThresholds,
    SignalReadiness,
)
from inv_trend.adapters.detector.models import AssetConfig, Market


def _asset() -> AssetConfig:
    return AssetConfig(
        symbol="BTC",
        instrument="BTCUSDT.BINANCE.SPOT",
        market=Market.CRYPTO,
        data_source="binance_public_data",
        timeframes=("D1",),
        source_symbols=("BTCUSDT",),
    )


def test_explicit_threshold_injection_has_priority() -> None:
    thresholds = EligibilityThresholds(data={"min_history_bars": 17})
    checker = TradeEligibilityChecker(thresholds)
    assert checker._resolve_thresholds(_asset()) is thresholds


def test_instrument_mismatch_is_hard_block_with_evidence() -> None:
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    bars = pd.DataFrame(
        {
            "symbol": ["ETH"] * 3,
            "instrument": ["ETHUSDT.BINANCE.SPOT"] * 3,
            "open": [1.0] * 3,
            "high": [1.1] * 3,
            "low": [0.9] * 3,
            "close": [1.0] * 3,
            "volume": [100.0] * 3,
        },
        index=idx,
    )
    checker = TradeEligibilityChecker(EligibilityThresholds())
    status, blocks, evidence = checker._check_instrument(bars, _asset(), checker.thresholds)
    assert status is SignalReadiness.NOT_READY
    assert blocks == [BlockedReason.DATA_SOURCE_MISMATCH]
    assert evidence["passed"] is False
    assert evidence["expected_instrument"] == "BTCUSDT.BINANCE.SPOT"
    assert evidence["observed_instruments"] == ["ETHUSDT.BINANCE.SPOT"]
