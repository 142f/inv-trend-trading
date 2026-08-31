from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from inv_trend.application import load_detector_asset_configs
from inv_trend.adapters.detector.config import load_strategy_config
from inv_trend.adapters.detector.backtest import ChronologicalSignalValidator
from inv_trend.adapters.detector.data.normalizer import apply_split_adjustment, normalize_bars
from inv_trend.adapters.detector.engine.scanner import TurtleScanner
from inv_trend.core.math_utils import donchian_channels, wilder_atr
from inv_trend.adapters.detector.models import (
    AssetConfig,
    DetectorState,
    Direction,
    Market,
    PositionStatus,
    SignalType,
    StrategyConfig,
)
from inv_trend.adapters.detector.storage import InMemorySignalRepository


def _asset(*timeframes: str) -> AssetConfig:
    return AssetConfig(
        symbol="TEST",
        instrument="TEST_SPOT",
        market=Market.CRYPTO,
        data_source="test",
        timeframes=tuple(timeframes or ("D1",)),
        adjustment="none",
    )


def _config(**overrides: object) -> StrategyConfig:
    values = {
        "atr_period": 3,
        "system1_entry": 3,
        "system2_entry": 8,
        "system1_exit": 2,
        "system2_exit": 4,
        "volatility_lookback": 4,
        "trend_ma_period": 3,
        "long_trend_ma_period": 5,
        "false_breakout_bars": 3,
    }
    values.update(overrides)
    return StrategyConfig(**values)


def _bars(closes: list[float], *, tz: str | None = "UTC") -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="D", tz=tz)
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(len(close), 1_000.0),
        },
        index=index,
    )


def test_default_assets_pin_actual_instruments_and_sources() -> None:
    assets = load_detector_asset_configs()
    assert assets["BTC"].instrument == "BTCUSDT.BINANCE.SPOT"
    assert assets["XAU"].instrument == "XAUUSD.DUKAS.BID.CFD"
    assert assets["XAG"].data_source == "dukascopy"
    assert assets["NVDA"].adjustment == "back_adjusted"
    assert load_strategy_config().confirmation_mode == "close"


def test_donchian_channel_excludes_current_bar() -> None:
    bars = _bars([10, 11, 12, 50])
    result = donchian_channels(bars["high"], bars["low"], {3})
    assert result.iloc[-1]["channel_high_3"] == pytest.approx(12.5)
    assert result.iloc[-1]["channel_high_3"] != 50.5


def test_wilder_atr_for_constant_ranges() -> None:
    bars = _bars([10, 10, 10, 10, 10])
    atr = wilder_atr(bars["high"], bars["low"], bars["close"], 3)
    assert atr.iloc[:2].isna().all()
    assert atr.iloc[2:].tolist() == pytest.approx([1.0, 1.0, 1.0])


def test_system1_and_system2_breakouts() -> None:
    scanner = TurtleScanner(_config())
    system1 = scanner.detect(
        _bars([10, 10, 10, 10, 12]),
        _asset(),
        "D1",
    )
    assert system1.signal.signal_type is SignalType.SYSTEM1_BREAKOUT
    assert system1.signal.direction is Direction.LONG

    system2_config = _config(system2_entry=5)
    system2 = TurtleScanner(system2_config).detect(
        _bars([10, 10, 10, 10, 10, 12]),
        _asset(),
        "D1",
    )
    assert system2.signal.signal_type is SignalType.SYSTEM2_BREAKOUT


@pytest.mark.parametrize(
    ("system", "expected_period"),
    [(1, 2), (2, 4)],
)
def test_system_specific_donchian_exits(system: int, expected_period: int) -> None:
    config = _config()
    bars = _bars([10, 10, 10, 10, 8])
    state = DetectorState(
        "TEST",
        "D1",
        status=PositionStatus.ENTERED,
        direction=Direction.LONG,
        system=system,
        entry_time=bars.index[-2].isoformat(),
        entry_price=10,
        breakout_level=9,
        atr_at_entry=1,
        stop_price=1,
        next_add_price=20,
        holding_bars=5,
    )
    result = TurtleScanner(config).detect(bars, _asset(), "D1", state)
    assert result.signal.signal_type is SignalType.EXIT_SIGNAL
    assert result.signal.metadata["system"] == system
    assert expected_period in {config.system1_exit, config.system2_exit}


def test_system1_winner_filter_preserves_raw_signal() -> None:
    state = DetectorState("TEST", "D1", last_system1_won=True)
    result = TurtleScanner(_config()).detect(
        _bars([10, 10, 10, 10, 12]),
        _asset(),
        "D1",
        state,
    )
    assert result.signal.raw_signal_type is SignalType.SYSTEM1_BREAKOUT
    assert result.signal.tradeable is False
    assert "system1_skipped_after_winner" in result.signal.filtered_reasons


def test_overextended_and_quality_filters_do_not_erase_raw_breakout() -> None:
    config = _config(
        overextended_atr=0.5,
        volume_filter=True,
        volume_lookback=3,
        min_volume_ratio=2,
    )
    result = TurtleScanner(config).detect(
        _bars([10, 10, 10, 10, 12]),
        _asset(),
        "D1",
    )
    assert result.signal.signal_type is SignalType.OVEREXTENDED
    assert result.signal.raw_signal_type is SignalType.SYSTEM1_BREAKOUT
    assert result.signal.tradeable is False
    assert "breakout_overextended" in result.signal.filtered_reasons
    assert "volume_below_filter" in result.signal.filtered_reasons


def test_pyramid_add_only_occurs_in_favorable_direction() -> None:
    bars = _bars([10, 10, 10, 11.5])
    state = DetectorState(
        "TEST",
        "D1",
        status=PositionStatus.ENTERED,
        direction=Direction.LONG,
        system=1,
        entry_time=bars.index[-2].isoformat(),
        entry_price=10,
        breakout_level=9,
        atr_at_entry=1,
        stop_price=5,
        next_add_price=11,
        holding_bars=5,
    )
    result = TurtleScanner(_config()).detect(bars, _asset(), "D1", state)
    assert result.signal.signal_type is SignalType.PYRAMID_ADD
    assert result.state.additions == 1
    assert result.state.next_add_price > 11

    adverse = replace(state, next_add_price=12)
    no_add = TurtleScanner(_config()).detect(
        _bars([10, 10, 10, 9.5]),
        _asset(),
        "D1",
        adverse,
    )
    assert no_add.signal.signal_type is not SignalType.PYRAMID_ADD


def test_false_breakout_invalidates_recent_entry() -> None:
    bars = _bars([10, 10, 10, 9.5])
    state = DetectorState(
        "TEST",
        "D1",
        status=PositionStatus.ENTERED,
        direction=Direction.LONG,
        system=1,
        entry_time=bars.index[-2].isoformat(),
        entry_price=11,
        breakout_level=10,
        atr_at_entry=1,
        stop_price=5,
        next_add_price=20,
        holding_bars=0,
    )
    result = TurtleScanner(_config()).detect(bars, _asset(), "D1", state)
    assert result.signal.signal_type is SignalType.FALSE_BREAKOUT
    assert result.state.status is PositionStatus.INVALIDATED


def test_atr_stop_has_priority_over_other_position_nodes() -> None:
    bars = _bars([10, 10, 10, 10])
    bars.iloc[-1, bars.columns.get_loc("low")] = 7
    state = DetectorState(
        "TEST",
        "D1",
        status=PositionStatus.ENTERED,
        direction=Direction.LONG,
        system=1,
        entry_time=bars.index[-2].isoformat(),
        entry_price=10,
        breakout_level=9,
        atr_at_entry=1,
        stop_price=8,
        next_add_price=11,
        holding_bars=2,
    )
    result = TurtleScanner(_config()).detect(bars, _asset(), "D1", state)
    assert result.signal.signal_type is SignalType.ATR_STOP
    assert result.signal.trigger_price == pytest.approx(8)


def test_intraday_breakout_transitions_to_close_confirmation() -> None:
    config = _config(confirmation_mode="intraday")
    scanner = TurtleScanner(config)
    first_bars = _bars([10, 10, 10, 10])
    first_bars.iloc[-1, first_bars.columns.get_loc("high")] = 12
    first = scanner.detect(first_bars, _asset(), "D1")
    assert first.signal.signal_type is SignalType.SYSTEM1_BREAKOUT
    assert first.state.status is PositionStatus.PENDING_CONFIRMATION

    next_bars = pd.concat([first_bars, _bars([12]).set_axis(
        [first_bars.index[-1] + pd.Timedelta(days=1)]
    )])
    confirmed = scanner.detect(next_bars, _asset(), "D1", first.state)
    assert confirmed.signal.signal_type is SignalType.CLOSE_CONFIRMED
    assert confirmed.state.status is PositionStatus.ENTERED


def test_multi_timeframe_frames_and_states_are_isolated() -> None:
    asset = _asset("H4", "D1")
    scanner = TurtleScanner(_config())
    results = scanner.scan_multi_timeframe(
        {
            "H4": _bars([10, 10, 10, 10, 12]),
            "D1": _bars([10, 10, 10, 10, 8]),
        },
        asset,
    )
    assert results["H4"].signal.timeframe == "H4"
    assert results["D1"].signal.timeframe == "D1"
    assert results["H4"].state is not results["D1"].state
    assert (
        results["H4"].signal.metadata["timeframe_alignment"]
        == "timeframe_conflict"
    )


def test_timezone_and_instrument_identity_are_strict() -> None:
    asset = _asset()
    with pytest.raises(ValueError, match="naive timestamps"):
        normalize_bars(_bars([10, 11], tz=None), asset, "D1")

    mixed = _bars([10, 11])
    mixed["instrument"] = ["TEST_SPOT", "OTHER"]
    with pytest.raises(ValueError, match="instrument"):
        normalize_bars(mixed, asset, "D1")

    dated = _bars([10, 11]).reset_index(names="date")
    normalized = normalize_bars(dated, asset, "D1")
    assert isinstance(normalized.index, pd.DatetimeIndex)
    assert str(normalized.index.tz) == "UTC"


def test_split_adjustment_preserves_economic_price_continuity() -> None:
    bars = _bars([400, 100])
    adjusted = apply_split_adjustment(bars, bars.index[1], split_ratio=4)
    assert adjusted.iloc[0]["close"] == pytest.approx(100)
    assert adjusted.iloc[0]["volume"] == pytest.approx(4_000)
    assert adjusted.iloc[1]["close"] == pytest.approx(100)


def test_signal_repository_deduplicates_same_transition() -> None:
    repository = InMemorySignalRepository()
    scanner = TurtleScanner(_config(), repository=repository)
    bars = _bars([10, 10, 10, 10, 12])
    first = scanner.scan_and_store(bars, _asset(), "D1")
    second = scanner.scan_and_store(bars, _asset(), "D1")
    assert first is not None
    assert second is None
    assert len(repository.signals) == 1


def test_missing_duplicate_and_invalid_data_are_rejected() -> None:
    asset = _asset()
    missing = _bars([10, 11]).drop(columns="volume")
    with pytest.raises(ValueError, match="missing bar columns"):
        normalize_bars(missing, asset, "D1")

    duplicate = pd.concat([_bars([10, 11]), _bars([10]).iloc[:1]])
    duplicate = duplicate.sort_index()
    with pytest.raises(ValueError, match="unique"):
        normalize_bars(duplicate, asset, "D1")

    invalid = _bars([10, 11])
    invalid.iloc[-1, invalid.columns.get_loc("high")] = 5
    with pytest.raises(ValueError, match="invalid OHLC"):
        normalize_bars(invalid, asset, "D1")


def test_future_bars_cannot_change_prior_indicators_or_signal() -> None:
    scanner = TurtleScanner(_config())
    history = _bars([10, 10, 10, 10, 12])
    with_future = pd.concat(
        [
            history,
            _bars([100]).set_axis([history.index[-1] + pd.Timedelta(days=1)]),
        ]
    )
    prior = scanner.prepare(history, _asset(), "D1").iloc[-1]
    expanded_prior = scanner.prepare(with_future, _asset(), "D1").iloc[-2]
    for column in ["atr", "channel_high_3", "channel_low_3"]:
        assert expanded_prior[column] == pytest.approx(prior[column])
    assert (
        scanner.detect(history, _asset(), "D1").signal.raw_signal_type
        is SignalType.SYSTEM1_BREAKOUT
    )


def test_chronological_backtest_reports_asset_and_signal_metrics() -> None:
    bars = _bars([10] * 8 + [12, 13, 14, 12, 9, 8, 8])
    result = ChronologicalSignalValidator(_config(), initial_equity=10_000).run(
        bars,
        _asset(),
        "D1",
    )
    assert result.symbol == "TEST"
    assert result.timeframe == "D1"
    assert result.metrics["signal_count"] >= 1
    assert not result.equity_curve.empty


def test_detector_backtester_compatibility_alias_warns() -> None:
    from inv_trend.adapters.detector.backtest import DetectorBacktester

    with pytest.warns(DeprecationWarning, match="signal validator"):
        validator = DetectorBacktester(_config())
    assert isinstance(validator, ChronologicalSignalValidator)
