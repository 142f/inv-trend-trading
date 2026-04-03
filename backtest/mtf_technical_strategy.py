from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd
import yfinance as yf
from ta.trend import EMAIndicator, MACD, SMAIndicator
from ta.volatility import AverageTrueRange


YFINANCE_SYMBOLS = {
    "BTCUSD": "BTC-USD",
    "XAUUSD": "XAUUSD=X",
}

SYNTHETIC_PROFILES = {
    "BTCUSD": {"base_price": 35_000.0, "seed": 11, "vol": 0.0028, "trend_amp": 0.00018},
    "XAUUSD": {"base_price": 2_300.0, "seed": 23, "vol": 0.0012, "trend_amp": 0.00010},
}


def interval_to_timeframe(interval: str) -> str:
    mapping = {
        "1m": "1m",
        "2m": "2m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "60m": "1h",
        "90m": "90m",
        "1h": "1h",
        "1d": "1d",
    }
    if interval not in mapping:
        raise ValueError(f"Unsupported yfinance interval: {interval}")
    return mapping[interval]


def period_to_days(period: str) -> int:
    period = period.strip().lower()
    if period.endswith("d"):
        return int(period[:-1])
    if period.endswith("mo"):
        return int(period[:-2]) * 30
    if period.endswith("y"):
        return int(period[:-1]) * 365
    raise ValueError(f"Unsupported period format: {period}")


@dataclass
class StrategyConfig:
    symbol: str
    base_interval: str = "60m"
    entry_timeframe: str = "1h"
    trend_timeframe: str = "4h"
    initial_capital: float = 10_000.0
    position_fraction: float = 1.0
    leverage: float = 1.0
    dynamic_leverage: bool = False
    min_leverage: float = 1.0
    max_leverage: float = 1.0
    fee_rate: float = 0.0006
    slippage_rate: float = 0.0002
    allow_short: bool = True
    ema_primary_period: int = 144
    ema_secondary_period: int = 169
    ma_period: int = 55
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    atr_target_multiple: float = 4.0


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    leverage: float
    margin_used: float
    notional: float
    pnl: float
    return_pct: float
    bars_held: int
    exit_reason: str


@dataclass
class BacktestResult:
    config: StrategyConfig
    data_start: pd.Timestamp
    data_end: pd.Timestamp
    total_bars: int
    final_equity: float
    total_return: float
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    total_trades: int
    win_rate: float
    profit_factor: float
    avg_trade_return: float
    avg_holding_bars: float
    avg_holding_days: float
    avg_leverage: float
    min_trade_leverage: float
    max_trade_leverage: float
    leverage_1x_trades: int
    leverage_2x_trades: int
    leverage_3x_trades: int
    long_trades: int
    short_trades: int
    long_win_rate: float
    short_win_rate: float
    equity_curve: pd.Series
    trades: pd.DataFrame
    signal_frame: pd.DataFrame

    def summary_row(self) -> dict[str, Any]:
        row = asdict(self.config)
        row.update(
            {
                "data_start": self.data_start,
                "data_end": self.data_end,
                "total_bars": self.total_bars,
                "final_equity": self.final_equity,
                "total_return": self.total_return,
                "annual_return": self.annual_return,
                "sharpe_ratio": self.sharpe_ratio,
                "max_drawdown": self.max_drawdown,
                "total_trades": self.total_trades,
                "win_rate": self.win_rate,
                "profit_factor": self.profit_factor,
                "avg_trade_return": self.avg_trade_return,
                "avg_holding_bars": self.avg_holding_bars,
                "avg_holding_days": self.avg_holding_days,
                "avg_leverage": self.avg_leverage,
                "min_trade_leverage": self.min_trade_leverage,
                "max_trade_leverage": self.max_trade_leverage,
                "leverage_1x_trades": self.leverage_1x_trades,
                "leverage_2x_trades": self.leverage_2x_trades,
                "leverage_3x_trades": self.leverage_3x_trades,
                "long_trades": self.long_trades,
                "short_trades": self.short_trades,
                "long_win_rate": self.long_win_rate,
                "short_win_rate": self.short_win_rate,
            }
        )
        return row


def timeframe_to_offset(timeframe: str) -> pd.Timedelta:
    unit = timeframe[-1].lower()
    value = int(timeframe[:-1])
    if unit == "m":
        return pd.Timedelta(minutes=value)
    if unit == "h":
        return pd.Timedelta(hours=value)
    if unit == "d":
        return pd.Timedelta(days=value)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def timeframe_to_pandas_freq(timeframe: str) -> str:
    unit = timeframe[-1].lower()
    value = int(timeframe[:-1])
    if unit == "m":
        return f"{value}min"
    if unit == "h":
        return f"{value}h"
    if unit == "d":
        return f"{value}d"
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def yfinance_ticker(symbol: str) -> str:
    return YFINANCE_SYMBOLS.get(symbol.upper(), symbol)


def _normalize_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if df.empty:
        raise ValueError("OHLCV data is empty")

    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    rename_map = {}
    for col in out.columns:
        lower = str(col).lower()
        if lower in {"open", "high", "low", "close", "volume"}:
            rename_map[col] = lower
    out = out.rename(columns=rename_map)

    required = ["open", "high", "low", "close", "volume"]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise ValueError(f"OHLCV data missing required columns: {missing}")

    idx = pd.DatetimeIndex(out.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    out.index = idx
    out.index.name = "open_ts"
    out = out[required].sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out = out.astype(float)
    out["close_ts"] = out.index + timeframe_to_offset(timeframe)
    return out


def fetch_yfinance_ohlcv(
    symbol: str,
    interval: str = "60m",
    period: Optional[str] = "365d",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    ticker = yfinance_ticker(symbol)
    df = yf.download(
        ticker,
        interval=interval,
        period=period if start is None and end is None else None,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise ValueError(f"No data returned for {symbol} ({ticker})")
    return _normalize_ohlcv(df, timeframe=interval_to_timeframe(interval))


def build_regime_synthetic_ohlcv(
    symbol: str,
    start: str,
    periods_15m: int,
) -> pd.DataFrame:
    profile = SYNTHETIC_PROFILES.get(symbol.upper(), {"base_price": 100.0, "seed": 29, "vol": 0.0015, "trend_amp": 0.00012})
    rng = np.random.default_rng(int(profile["seed"]))
    idx = pd.date_range(start=start, periods=periods_15m, freq="15min", tz="UTC")
    steps = np.arange(periods_15m, dtype=float)

    # Regime cycles alternate between bullish and bearish drift so multi-year tests contain both long and short environments.
    regime_span = 96 * 45
    regime_phase = np.sin(2 * np.pi * steps / regime_span)
    slow_cycle = np.sin(2 * np.pi * steps / (96 * 180))
    drift = profile["trend_amp"] * np.sign(regime_phase + 0.35 * slow_cycle)
    noise = rng.normal(0.0, profile["vol"], periods_15m)
    returns = drift + 0.35 * profile["trend_amp"] * slow_cycle + noise

    close = float(profile["base_price"]) * np.exp(np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]]
    wick_up = rng.uniform(0.0002, 0.0030, periods_15m)
    wick_down = rng.uniform(0.0002, 0.0030, periods_15m)
    high = np.maximum(open_, close) * (1.0 + wick_up)
    low = np.minimum(open_, close) * (1.0 - wick_down)
    volume = rng.uniform(50.0, 500.0, periods_15m)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    df.index.name = "open_ts"
    df["close_ts"] = df.index + pd.Timedelta(minutes=15)
    return df


def load_ohlcv_csv(path: str | Path, timeframe: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "open_ts" not in df.columns:
        raise ValueError("CSV must contain an open_ts column")
    df["open_ts"] = pd.to_datetime(df["open_ts"], utc=True)
    df = df.set_index("open_ts")
    return _normalize_ohlcv(df, timeframe=timeframe)


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = timeframe_to_pandas_freq(timeframe)
    out = (
        df.resample(rule, label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
    )
    out.index.name = "open_ts"
    out["close_ts"] = out.index + timeframe_to_offset(timeframe)
    return out


def add_indicators(df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    out = df.copy()

    out["ema144"] = EMAIndicator(out["close"], window=config.ema_primary_period).ema_indicator()
    out["ema169"] = EMAIndicator(out["close"], window=config.ema_secondary_period).ema_indicator()
    out["ma55"] = SMAIndicator(out["close"], window=config.ma_period).sma_indicator()

    macd = MACD(
        out["close"],
        window_fast=config.macd_fast,
        window_slow=config.macd_slow,
        window_sign=config.macd_signal,
    )
    out["macd_line"] = macd.macd()
    out["macd_signal"] = macd.macd_signal()
    out["macd_hist"] = macd.macd_diff()
    out["macd_cross_up"] = (out["macd_line"] > out["macd_signal"]) & (
        out["macd_line"].shift(1) <= out["macd_signal"].shift(1)
    )
    out["macd_cross_down"] = (out["macd_line"] < out["macd_signal"]) & (
        out["macd_line"].shift(1) >= out["macd_signal"].shift(1)
    )

    out["atr"] = AverageTrueRange(
        high=out["high"],
        low=out["low"],
        close=out["close"],
        window=config.atr_period,
    ).average_true_range()
    out["ma55_slope"] = out["ma55"].diff()
    out["ema_spread"] = out["ema144"] - out["ema169"]
    return out


def indicator_score(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(0.0, index=df.index, dtype=float)
    score += np.where(df["close"] > df[["ema144", "ema169"]].max(axis=1), 1.0, 0.0)
    score += np.where(df["close"] < df[["ema144", "ema169"]].min(axis=1), -1.0, 0.0)
    score += np.where(df["ema_spread"] > 0, 1.0, np.where(df["ema_spread"] < 0, -1.0, 0.0))
    score += np.where(
        (df["ma55"] > df["ema144"]) & (df["ma55_slope"] > 0),
        1.0,
        np.where((df["ma55"] < df["ema144"]) & (df["ma55_slope"] < 0), -1.0, 0.0),
    )
    score += np.where(
        (df["macd_line"] > df["macd_signal"]) & (df["macd_hist"] > 0),
        1.0,
        np.where((df["macd_line"] < df["macd_signal"]) & (df["macd_hist"] < 0), -1.0, 0.0),
    )
    return score


def classify_trend(df: pd.DataFrame) -> pd.Series:
    required = ["ema144", "ema169", "ma55", "ma55_slope", "macd_line", "macd_signal", "macd_hist"]
    score = indicator_score(df)
    out = pd.Series(np.select([score >= 2.0, score <= -2.0], [1.0, -1.0], default=0.0), index=df.index, dtype=float)
    out[df[required].isna().any(axis=1)] = np.nan
    return out


def align_trend_to_entry(entry_df: pd.DataFrame, trend_df: pd.DataFrame) -> pd.DataFrame:
    entry_reset = entry_df.copy()
    entry_reset.index.name = "open_ts"
    entry_reset = entry_reset.reset_index()
    trend_cols = [
        "close_ts",
        "trend_score",
        "trend_bias",
        "ema144",
        "ema169",
        "ma55",
        "ma55_slope",
        "macd_line",
        "macd_signal",
        "macd_hist",
    ]
    trend_reset = trend_df[trend_cols].dropna(subset=["trend_bias"]).reset_index(drop=True)
    # Merge by close timestamp so the lower timeframe only sees the latest fully closed higher-timeframe bar.
    merged = pd.merge_asof(
        entry_reset.sort_values("close_ts"),
        trend_reset.sort_values("close_ts"),
        on="close_ts",
        direction="backward",
        suffixes=("", "_trend"),
    )
    merged = merged.rename(
        columns={
            "trend_score": "trend_score_htf",
            "trend_bias": "trend_bias_htf",
            "ema144_trend": "ema144_htf",
            "ema169_trend": "ema169_htf",
            "ma55_trend": "ma55_htf",
            "ma55_slope_trend": "ma55_slope_htf",
            "macd_line_trend": "macd_line_htf",
            "macd_signal_trend": "macd_signal_htf",
            "macd_hist_trend": "macd_hist_htf",
        }
    )
    return merged.set_index("open_ts")


def build_signal_frame(raw_df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """
    Build the multi-timeframe signal table.

    Design choice:
    - Higher timeframe defines direction only.
    - Entry timeframe is responsible for timing.
    - Alignment uses higher-bar close timestamps, so lower bars never see an unfinished higher bar.
    """
    entry_df = resample_ohlcv(raw_df, config.entry_timeframe)
    trend_df = resample_ohlcv(raw_df, config.trend_timeframe)

    entry_df = add_indicators(entry_df, config)
    trend_df = add_indicators(trend_df, config)
    entry_df["entry_score"] = indicator_score(entry_df)
    trend_df["trend_score"] = indicator_score(trend_df)
    trend_df["trend_bias"] = classify_trend(trend_df)

    aligned = align_trend_to_entry(entry_df, trend_df)
    aligned["entry_score"] = indicator_score(aligned)

    bullish_reclaim = (aligned["close"] > aligned["ma55"]) & (aligned["close"].shift(1) <= aligned["ma55"].shift(1))
    bearish_reject = (aligned["close"] < aligned["ma55"]) & (aligned["close"].shift(1) >= aligned["ma55"].shift(1))
    bullish_breakout = (aligned["close"] > aligned["high"].shift(1)) & (aligned["macd_hist"] > aligned["macd_hist"].shift(1))
    bearish_breakout = (aligned["close"] < aligned["low"].shift(1)) & (aligned["macd_hist"] < aligned["macd_hist"].shift(1))
    long_trigger = aligned["macd_cross_up"] | bullish_reclaim | bullish_breakout
    short_trigger = aligned["macd_cross_down"] | bearish_reject | bearish_breakout

    aligned["long_entry_signal"] = (
        (aligned["trend_bias_htf"] == 1)
        & (aligned["entry_score"] >= 1)
        & long_trigger
    )
    aligned["short_entry_signal"] = (
        (aligned["trend_bias_htf"] == -1)
        & (aligned["entry_score"] <= -1)
        & short_trigger
    )
    aligned["long_exit_signal"] = (
        aligned["macd_cross_down"]
        | (aligned["close"] < aligned["ma55"])
        | (aligned["entry_score"] <= -1)
        | (aligned["trend_bias_htf"] <= 0)
    )
    aligned["short_exit_signal"] = (
        aligned["macd_cross_up"]
        | (aligned["close"] > aligned["ma55"])
        | (aligned["entry_score"] >= 1)
        | (aligned["trend_bias_htf"] >= 0)
    )
    return aligned


def _fill_price(open_price: float, side: str, slippage_rate: float, is_entry: bool) -> float:
    direction = 1.0 if side == "long" else -1.0
    sign = direction if is_entry else -direction
    return open_price * (1.0 + sign * slippage_rate)


def _annualization_factor(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 1.0
    spacing = index.to_series().diff().dropna().median()
    if pd.isna(spacing) or spacing <= pd.Timedelta(0):
        return 1.0
    seconds = spacing.total_seconds()
    return (365.0 * 24.0 * 60.0 * 60.0) / seconds


def _resolve_entry_leverage(bar: Any, config: StrategyConfig) -> float:
    if not config.dynamic_leverage:
        return float(config.leverage)

    trend_strength = abs(float(getattr(bar, "trend_score_htf", 0.0) or 0.0))
    entry_strength = abs(float(getattr(bar, "entry_score", 0.0) or 0.0))
    close_price = float(getattr(bar, "close", 0.0) or 0.0)
    atr = float(getattr(bar, "atr", 0.0) or 0.0)
    atr_ratio = atr / close_price if close_price > 0 else np.inf

    leverage = config.min_leverage
    if trend_strength >= 3.0 and entry_strength >= 2.0:
        leverage = min(2.0, config.max_leverage)
    if trend_strength >= 4.0 and entry_strength >= 3.0 and atr_ratio <= 0.02:
        leverage = min(3.0, config.max_leverage)
    return float(np.clip(leverage, config.min_leverage, config.max_leverage))


def run_backtest(signal_frame: pd.DataFrame, config: StrategyConfig) -> BacktestResult:
    """
    Event-driven backtest.

    Design choice:
    - Signals are generated on bar close and executed on the next bar open.
    - ATR stop/target are checked intrabar.
    - One position per symbol keeps the example simple and easy to extend.
    """
    df = signal_frame.copy()
    df = df.dropna(subset=["open", "high", "low", "close", "atr"])
    if df.empty:
        raise ValueError("Signal frame is empty after indicator warmup")
    if config.leverage <= 0:
        raise ValueError("leverage must be > 0")
    if not (0 < config.position_fraction <= 1):
        raise ValueError("position_fraction must be within (0, 1]")
    if config.min_leverage <= 0 or config.max_leverage <= 0:
        raise ValueError("min_leverage and max_leverage must be > 0")
    if config.min_leverage > config.max_leverage:
        raise ValueError("min_leverage must be <= max_leverage")

    realized_equity = config.initial_capital
    equity_points: list[tuple[pd.Timestamp, float]] = []
    trades: list[BacktestTrade] = []

    position: Optional[dict[str, Any]] = None
    pending_entry: Optional[dict[str, Any]] = None
    pending_exit: Optional[dict[str, Any]] = None
    skip_entry_on_bar = False

    for bar in df.itertuples():
        ts = pd.Timestamp(bar.Index)
        skip_entry_on_bar = False

        if pending_exit and position is not None:
            # Signal exits are decided on the prior bar close and executed on the next bar open.
            exit_price = _fill_price(bar.open, position["side"], config.slippage_rate, is_entry=False)
            notional = position["quantity"] * exit_price
            exit_fee = notional * config.fee_rate
            pnl = position["quantity"] * (exit_price - position["entry_price"])
            if position["side"] == "short":
                pnl = -pnl
            realized_equity += pnl - exit_fee
            trades.append(
                BacktestTrade(
                    symbol=config.symbol,
                    side=position["side"],
                    entry_time=position["entry_time"],
                    exit_time=ts,
                    entry_price=position["entry_price"],
                    exit_price=exit_price,
                    quantity=position["quantity"],
                    leverage=position["applied_leverage"],
                    margin_used=position["margin_used"],
                    notional=position["notional_at_entry"],
                    pnl=pnl - position["entry_fee"] - exit_fee,
                    return_pct=(pnl - position["entry_fee"] - exit_fee) / position["equity_at_entry"],
                    bars_held=position["bars_held"],
                    exit_reason=pending_exit["reason"],
                )
            )
            position = None
            pending_exit = None
            skip_entry_on_bar = True

        if pending_entry and position is None and not skip_entry_on_bar:
            side = pending_entry["side"]
            entry_price = _fill_price(bar.open, side, config.slippage_rate, is_entry=True)
            equity_at_entry = realized_equity
            margin_used = realized_equity * config.position_fraction
            applied_leverage = _resolve_entry_leverage(bar, config)
            notional_at_entry = margin_used * applied_leverage
            if notional_at_entry > 0:
                quantity = notional_at_entry / entry_price
                entry_fee = notional_at_entry * config.fee_rate
                realized_equity -= entry_fee
                atr = pending_entry["atr"]
                if side == "long":
                    stop_price = entry_price - atr * config.atr_stop_multiple
                    target_price = entry_price + atr * config.atr_target_multiple
                else:
                    stop_price = entry_price + atr * config.atr_stop_multiple
                    target_price = entry_price - atr * config.atr_target_multiple
                position = {
                    "side": side,
                    "entry_time": ts,
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "applied_leverage": applied_leverage,
                    "equity_at_entry": equity_at_entry,
                    "margin_used": margin_used,
                    "notional_at_entry": notional_at_entry,
                    "entry_fee": entry_fee,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "bars_held": 0,
                }
            pending_entry = None

        if position is not None:
            position["bars_held"] += 1

            stop_hit = False
            target_hit = False
            if position["side"] == "long":
                stop_hit = bar.low <= position["stop_price"]
                target_hit = bar.high >= position["target_price"]
            else:
                stop_hit = bar.high >= position["stop_price"]
                target_hit = bar.low <= position["target_price"]

            if stop_hit or target_hit:
                # Conservative assumption: if stop and target are both touched in one bar, stop wins.
                exit_reason = "atr_stop" if stop_hit else "atr_target"
                exit_raw_price = position["stop_price"] if stop_hit else position["target_price"]
                exit_price = _fill_price(exit_raw_price, position["side"], config.slippage_rate, is_entry=False)
                notional = position["quantity"] * exit_price
                exit_fee = notional * config.fee_rate
                pnl = position["quantity"] * (exit_price - position["entry_price"])
                if position["side"] == "short":
                    pnl = -pnl
                realized_equity += pnl - exit_fee
                trades.append(
                    BacktestTrade(
                        symbol=config.symbol,
                        side=position["side"],
                    entry_time=position["entry_time"],
                    exit_time=ts,
                    entry_price=position["entry_price"],
                    exit_price=exit_price,
                    quantity=position["quantity"],
                    leverage=position["applied_leverage"],
                    margin_used=position["margin_used"],
                    notional=position["notional_at_entry"],
                    pnl=pnl - position["entry_fee"] - exit_fee,
                        return_pct=(pnl - position["entry_fee"] - exit_fee) / position["equity_at_entry"],
                        bars_held=position["bars_held"],
                        exit_reason=exit_reason,
                    )
                )
                position = None
                pending_exit = None

        mark_equity = realized_equity
        if position is not None:
            mark_pnl = position["quantity"] * (bar.close - position["entry_price"])
            if position["side"] == "short":
                mark_pnl = -mark_pnl
            mark_equity += mark_pnl
        equity_points.append((ts, mark_equity))

        if position is not None:
            if position["side"] == "long" and bar.long_exit_signal:
                pending_exit = {"reason": "signal_exit"}
            elif position["side"] == "short" and bar.short_exit_signal:
                pending_exit = {"reason": "signal_exit"}
        elif not skip_entry_on_bar:
            if bar.long_entry_signal:
                pending_entry = {"side": "long", "atr": float(bar.atr)}
            elif config.allow_short and bar.short_entry_signal:
                pending_entry = {"side": "short", "atr": float(bar.atr)}

    if position is not None:
        last_bar = df.iloc[-1]
        last_ts = pd.Timestamp(df.index[-1])
        exit_price = _fill_price(float(last_bar["close"]), position["side"], config.slippage_rate, is_entry=False)
        notional = position["quantity"] * exit_price
        exit_fee = notional * config.fee_rate
        pnl = position["quantity"] * (exit_price - position["entry_price"])
        if position["side"] == "short":
            pnl = -pnl
        realized_equity += pnl - exit_fee
        trades.append(
            BacktestTrade(
                symbol=config.symbol,
                side=position["side"],
                entry_time=position["entry_time"],
                exit_time=last_ts,
                entry_price=position["entry_price"],
                exit_price=exit_price,
                quantity=position["quantity"],
                leverage=position["applied_leverage"],
                margin_used=position["margin_used"],
                notional=position["notional_at_entry"],
                pnl=pnl - position["entry_fee"] - exit_fee,
                return_pct=(pnl - position["entry_fee"] - exit_fee) / position["equity_at_entry"],
                bars_held=position["bars_held"],
                exit_reason="forced_close_last_bar",
            )
        )
        equity_points[-1] = (last_ts, realized_equity)

    equity_curve = pd.Series([value for _, value in equity_points], index=[ts for ts, _ in equity_points], name="equity")
    returns = equity_curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    annualization = _annualization_factor(pd.DatetimeIndex(equity_curve.index))

    if returns.empty or returns.std(ddof=0) == 0:
        sharpe_ratio = 0.0
    else:
        sharpe_ratio = float(np.sqrt(annualization) * returns.mean() / returns.std(ddof=0))

    drawdown = 1.0 - equity_curve / equity_curve.cummax()
    max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0

    total_days = max((equity_curve.index[-1] - equity_curve.index[0]).total_seconds() / 86400.0, 1.0)
    annual_return = float((equity_curve.iloc[-1] / config.initial_capital) ** (365.0 / total_days) - 1.0)

    trades_df = pd.DataFrame([asdict(trade) for trade in trades])
    gross_profit = float(trades_df.loc[trades_df["pnl"] > 0, "pnl"].sum()) if not trades_df.empty else 0.0
    gross_loss = float(-trades_df.loc[trades_df["pnl"] < 0, "pnl"].sum()) if not trades_df.empty else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = np.inf
    else:
        profit_factor = 0.0
    win_rate = float((trades_df["pnl"] > 0).mean()) if not trades_df.empty else 0.0
    avg_trade_return = float(trades_df["return_pct"].mean()) if not trades_df.empty else 0.0
    avg_holding_bars = float(trades_df["bars_held"].mean()) if not trades_df.empty else 0.0
    avg_holding_days = (
        float(((trades_df["exit_time"] - trades_df["entry_time"]).dt.total_seconds() / 86400.0).mean())
        if not trades_df.empty
        else 0.0
    )
    avg_leverage = float(trades_df["leverage"].mean()) if not trades_df.empty else 0.0
    min_trade_leverage = float(trades_df["leverage"].min()) if not trades_df.empty else 0.0
    max_trade_leverage = float(trades_df["leverage"].max()) if not trades_df.empty else 0.0
    leverage_1x_trades = int((trades_df["leverage"].round(6) == 1.0).sum()) if not trades_df.empty else 0
    leverage_2x_trades = int((trades_df["leverage"].round(6) == 2.0).sum()) if not trades_df.empty else 0
    leverage_3x_trades = int((trades_df["leverage"].round(6) == 3.0).sum()) if not trades_df.empty else 0
    long_trades = int((trades_df["side"] == "long").sum()) if not trades_df.empty else 0
    short_trades = int((trades_df["side"] == "short").sum()) if not trades_df.empty else 0
    long_win_rate = (
        float((trades_df.loc[trades_df["side"] == "long", "pnl"] > 0).mean()) if long_trades > 0 else 0.0
    )
    short_win_rate = (
        float((trades_df.loc[trades_df["side"] == "short", "pnl"] > 0).mean()) if short_trades > 0 else 0.0
    )

    return BacktestResult(
        config=config,
        data_start=pd.Timestamp(df.index[0]),
        data_end=pd.Timestamp(df.index[-1]),
        total_bars=len(df),
        final_equity=float(equity_curve.iloc[-1]),
        total_return=float(equity_curve.iloc[-1] / config.initial_capital - 1.0),
        annual_return=annual_return,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        total_trades=int(len(trades_df)),
        win_rate=win_rate,
        profit_factor=float(profit_factor),
        avg_trade_return=avg_trade_return,
        avg_holding_bars=avg_holding_bars,
        avg_holding_days=avg_holding_days,
        avg_leverage=avg_leverage,
        min_trade_leverage=min_trade_leverage,
        max_trade_leverage=max_trade_leverage,
        leverage_1x_trades=leverage_1x_trades,
        leverage_2x_trades=leverage_2x_trades,
        leverage_3x_trades=leverage_3x_trades,
        long_trades=long_trades,
        short_trades=short_trades,
        long_win_rate=long_win_rate,
        short_win_rate=short_win_rate,
        equity_curve=equity_curve,
        trades=trades_df,
        signal_frame=df,
    )


def run_strategy_from_ohlcv(ohlcv: pd.DataFrame, config: StrategyConfig) -> BacktestResult:
    signal_frame = build_signal_frame(ohlcv, config)
    return run_backtest(signal_frame, config)


def run_strategy_from_yfinance(
    config: StrategyConfig,
    period: Optional[str] = "365d",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> BacktestResult:
    raw_df = fetch_yfinance_ohlcv(
        symbol=config.symbol,
        interval=config.base_interval,
        period=period,
        start=start,
        end=end,
    )
    return run_strategy_from_ohlcv(raw_df, config)


def run_strategy_from_synthetic(
    config: StrategyConfig,
    period: str = "365d",
    synthetic_start: str = "2024-01-01",
) -> BacktestResult:
    days = period_to_days(period)
    periods_15m = (days + 45) * 96
    raw_df = build_regime_synthetic_ohlcv(
        symbol=config.symbol,
        start=synthetic_start,
        periods_15m=periods_15m,
    )
    cutoff = raw_df.index.min() + pd.Timedelta(days=45)
    raw_df = raw_df[raw_df.index >= cutoff]
    return run_strategy_from_ohlcv(raw_df, config)


def run_batch(
    symbols: Sequence[str],
    periods: Sequence[str],
    base_interval: str = "60m",
    entry_timeframe: str = "1h",
    trend_timeframe: str = "4h",
    initial_capital: float = 10_000.0,
    leverage: float = 1.0,
    leverages: Optional[Sequence[float]] = None,
    dynamic_leverage: bool = False,
    min_leverage: float = 1.0,
    max_leverage: float = 1.0,
    allow_short: bool = True,
    use_synthetic: bool = False,
) -> tuple[pd.DataFrame, list[BacktestResult]]:
    results: list[BacktestResult] = []
    summary_rows: list[dict[str, Any]] = []
    leverage_values = [leverage] if dynamic_leverage else (list(leverages) if leverages is not None else [leverage])

    for symbol in symbols:
        for period in periods:
            for leverage_value in leverage_values:
                config = StrategyConfig(
                    symbol=symbol,
                    base_interval=base_interval,
                    entry_timeframe=entry_timeframe,
                    trend_timeframe=trend_timeframe,
                    initial_capital=initial_capital,
                    leverage=leverage_value,
                    dynamic_leverage=dynamic_leverage,
                    min_leverage=min_leverage,
                    max_leverage=max_leverage,
                    allow_short=allow_short,
                )
                if use_synthetic:
                    result = run_strategy_from_synthetic(config=config, period=period)
                else:
                    result = run_strategy_from_yfinance(config=config, period=period)
                results.append(result)
                row = result.summary_row()
                row["period"] = period
                row["data_source"] = "synthetic" if use_synthetic else "yfinance"
                summary_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        numeric_cols = [
            "final_equity",
            "total_return",
            "annual_return",
            "sharpe_ratio",
            "max_drawdown",
            "win_rate",
            "profit_factor",
            "avg_trade_return",
            "avg_holding_bars",
            "avg_holding_days",
            "avg_leverage",
            "min_trade_leverage",
            "max_trade_leverage",
            "long_win_rate",
            "short_win_rate",
        ]
        for col in numeric_cols:
            summary[col] = pd.to_numeric(summary[col], errors="coerce")
    return summary, results


def save_result(result: BacktestResult, output_dir: str | Path, period_label: str) -> None:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if result.config.dynamic_leverage:
        lev_part = f"dyn_{result.config.min_leverage:g}x_{result.config.max_leverage:g}x"
    else:
        lev_part = f"fix_{result.config.leverage:g}x"
    stem = f"{result.config.symbol}_{result.config.entry_timeframe}_{result.config.trend_timeframe}_{period_label}_{lev_part}"
    result.equity_curve.to_csv(out_dir / f"{stem}_equity.csv", header=True)
    result.signal_frame.to_csv(out_dir / f"{stem}_signals.csv")
    if not result.trades.empty:
        result.trades.to_csv(out_dir / f"{stem}_trades.csv", index=False)
