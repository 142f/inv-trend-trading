"""Minimal chronological validator for detector behavior, one asset at a time."""

from __future__ import annotations

from dataclasses import dataclass
import warnings

import pandas as pd

from ..engine.scanner import TurtleScanner
from ..models import (
    AssetConfig,
    DetectorState,
    Direction,
    SignalType,
    StrategyConfig,
)
from .metrics import compute_metrics


@dataclass(frozen=True)
class SignalValidationResult:
    symbol: str
    timeframe: str
    signals: pd.DataFrame
    trades: pd.DataFrame
    equity_curve: pd.Series
    metrics: dict[str, float]


class ChronologicalSignalValidator:
    """Validates transitions in time order; it never randomly splits observations."""

    def __init__(
        self,
        config: StrategyConfig | None = None,
        initial_equity: float = 100_000.0,
        cost_bps: float = 0.0,
    ) -> None:
        if initial_equity <= 0 or cost_bps < 0:
            raise ValueError("invalid backtest capital or costs")
        self.config = config or StrategyConfig()
        self.initial_equity = float(initial_equity)
        self.cost_bps = float(cost_bps)

    def run(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
    ) -> SignalValidationResult:
        scanner = TurtleScanner(self.config)
        prepared = scanner.prepare(bars, asset, timeframe)
        state = DetectorState(asset.symbol, timeframe.upper())
        cash = self.initial_equity
        position: dict[str, float | str | int] | None = None
        signals: list[dict] = []
        trades: list[dict] = []
        equity_points: list[tuple[pd.Timestamp, float]] = []
        start = max(1, self.config.warmup_bars - 1)

        for idx in range(start, len(prepared)):
            # Indicators are prepared once; each prefix remains causal because
            # every rolling feature is backward-looking and Donchian is shifted.
            # ``detect_prepared`` only reads the final row.  Supplying that
            # row avoids allocating O(n²) prefix frames during chronology.
            result = scanner.detect_row(prepared.iloc[idx], asset, timeframe, state)
            state = result.state
            signal = result.signal
            close = float(prepared.iloc[idx]["close"])
            if signal.signal_type is not SignalType.NO_SIGNAL:
                signals.append(signal.to_dict())
            if signal.tradeable and signal.signal_type in {
                SignalType.SYSTEM1_BREAKOUT,
                SignalType.SYSTEM2_BREAKOUT,
                SignalType.CLOSE_CONFIRMED,
            } and position is None:
                position = {
                    "direction": signal.direction.value,
                    "entry_price": signal.trigger_price,
                    "entry_time": signal.signal_time,
                    "qty": 1.0,
                    "holding_bars": 0,
                }
                cash -= signal.trigger_price * self.cost_bps / 10_000
            elif signal.signal_type is SignalType.PYRAMID_ADD and position is not None:
                qty = float(position["qty"])
                position["entry_price"] = (
                    float(position["entry_price"]) * qty + signal.trigger_price
                ) / (qty + 1)
                position["qty"] = qty + 1
                cash -= signal.trigger_price * self.cost_bps / 10_000
            elif signal.signal_type in {
                SignalType.EXIT_SIGNAL,
                SignalType.ATR_STOP,
                SignalType.FALSE_BREAKOUT,
                SignalType.TREND_INVALIDATED,
            } and position is not None:
                side = 1 if position["direction"] == Direction.LONG.value else -1
                qty = float(position["qty"])
                gross = side * qty * (
                    signal.trigger_price - float(position["entry_price"])
                )
                exit_cost = qty * signal.trigger_price * self.cost_bps / 10_000
                cash += gross - exit_cost
                trades.append(
                    {
                        "symbol": asset.symbol,
                        "timeframe": timeframe.upper(),
                        "entry_time": position["entry_time"],
                        "exit_time": signal.signal_time,
                        "direction": position["direction"],
                        "entry_price": position["entry_price"],
                        "exit_price": signal.trigger_price,
                        "qty": qty,
                        "pnl": gross - exit_cost,
                        "holding_bars": position["holding_bars"],
                        "exit_type": signal.signal_type.value,
                    }
                )
                position = None
            if position is not None:
                position["holding_bars"] = int(position["holding_bars"]) + 1
                side = 1 if position["direction"] == Direction.LONG.value else -1
                marked = cash + side * float(position["qty"]) * (
                    close - float(position["entry_price"])
                )
            else:
                marked = cash
            equity_points.append((prepared.index[idx], marked))

        signal_frame = pd.DataFrame(signals)
        trade_frame = pd.DataFrame(trades)
        equity = pd.Series(
            [value for _, value in equity_points],
            index=[timestamp for timestamp, _ in equity_points],
            name="equity",
            dtype=float,
        )
        false_count = (
            int((signal_frame["signal_type"] == SignalType.FALSE_BREAKOUT.value).sum())
            if not signal_frame.empty
            else 0
        )
        return SignalValidationResult(
            symbol=asset.symbol,
            timeframe=timeframe.upper(),
            signals=signal_frame,
            trades=trade_frame,
            equity_curve=equity,
            metrics=compute_metrics(
                equity,
                trade_frame,
                len(signal_frame),
                false_count,
            ),
        )


DetectorBacktestResult = SignalValidationResult


class DetectorBacktester(ChronologicalSignalValidator):
    """Deprecated compatibility name for the chronological signal validator."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        warnings.warn(
            "DetectorBacktester is a signal validator; use "
            "ChronologicalSignalValidator",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)
