"""Shared D1 backtest helpers for examples and regression checks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

from inv_trend.application.backtest import UnifiedBacktestExecutor
from inv_trend.application.backtest.single_artifacts import write_single_backtest_outputs
from inv_trend.adapters.multi_asset import AssetSpec, TurtleRules
from inv_trend.adapters.multi_asset.config import BacktestConfig
from inv_trend.adapters.multi_asset.models import SHORT


CORE_SYMBOLS = [
    "XAUUSD_DUKAS",
    "XAGUSD_DUKAS",
    "BTCUSDT_BINANCE",
    "ETHUSDT_BINANCE",
]

EQUITY_SYMBOLS = [
    "NVDA",
    "MU",
    "AMD",
    "TSM",
    "SNDK",
    "AVGO",
    "QQQ",
    "SPY",
    "XLY",
    "ORCL",
    "MSFT",
    "PLTR",
    "NFLX",
    "META",
    "AAPL",
    "TSLA",
    "GOOGL",
    "AMZN",
]

EQUITY_CLUSTERS = {
    "NVDA": "semiconductors",
    "MU": "semiconductors",
    "AMD": "semiconductors",
    "TSM": "semiconductors",
    "SNDK": "semiconductors",
    "AVGO": "semiconductors",
    "QQQ": "broad_equity",
    "SPY": "broad_equity",
    "XLY": "consumer",
    "NFLX": "consumer",
    "TSLA": "consumer",
    "AMZN": "consumer",
    "ORCL": "software",
    "MSFT": "software",
    "PLTR": "software",
    "META": "communication",
    "GOOGL": "communication",
    "AAPL": "mega_cap_tech",
}

METALS = ["XAUUSD_DUKAS", "XAGUSD_DUKAS"]
CRYPTO = ["BTCUSDT_BINANCE", "ETHUSDT_BINANCE"]
SEMIS = ["NVDA", "AMD", "MU", "TSM", "AVGO"]
PLATFORM_CORE = ["MSFT", "META", "GOOGL", "AMZN"]
CONSUMER_ETF_WEAK = ["SPY", "QQQ", "XLY", "AAPL", "TSLA", "NFLX", "ORCL"]
SHORT_HISTORY = ["PLTR", "SNDK"]

PRESET_RUNS: dict[str, dict[str, Any]] = {
    "core4_macro_crypto": {
        "symbols": METALS + CRYPTO,
        "eth_mode": "long_short",
        "align_start": True,
        "description": "XAU/XAG long only + BTC/ETH long short",
    },
    "core4_eth_short_only": {
        "symbols": METALS + CRYPTO,
        "eth_mode": "short_only",
        "align_start": True,
        "description": "XAU/XAG long only + BTC long short + ETH short only",
    },
    "base_9_eth_short_only": {
        "symbols": METALS + CRYPTO + SEMIS,
        "eth_mode": "short_only",
        "align_start": True,
        "description": "Candidate 9 with ETH short only",
    },
    "revised_8_no_eth": {
        "symbols": METALS + ["BTCUSDT_BINANCE"] + SEMIS,
        "eth_mode": "excluded",
        "align_start": True,
        "description": "Revised 8 without ETH",
    },
    "compact_a_grade": {
        "symbols": METALS + ["BTCUSDT_BINANCE"] + ["NVDA", "AMD", "MU", "MSFT", "META"],
        "eth_mode": "excluded",
        "align_start": True,
        "description": "Compact A-grade set",
    },
    "previous_full20_no_pltr_sndk": {
        "symbols": CORE_SYMBOLS + [s for s in EQUITY_SYMBOLS if s not in SHORT_HISTORY],
        "eth_mode": "long_short",
        "align_start": True,
        "description": "20-symbol reference without PLTR/SNDK",
    },
    "metal_tech_core_10": {
        "symbols": ["AMD", "AMZN", "AVGO", "MSFT", "NVDA", "ORCL", "QQQ", "TSM", "XAGUSD_DUKAS", "XAUUSD_DUKAS"],
        "eth_mode": "excluded",
        "align_start": False,
        "description": "10-symbol metal + tech core",
    },
}


def load_universe(core_data_dir: Path, equity_data_dir: Path) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for symbol in CORE_SYMBOLS:
        path = _resolve_core_symbol_path(core_data_dir, symbol)
        if path is not None:
            data[symbol] = h4_to_d1(load_csv(path))
    for symbol in EQUITY_SYMBOLS:
        path = _resolve_equity_symbol_path(equity_data_dir, symbol)
        if path is not None:
            data[symbol] = load_csv(path)
    return data


def _resolve_core_symbol_path(core_data_dir: Path, symbol: str) -> Path | None:
    direct = core_data_dir / "processed" / "external" / "H4" / f"{symbol}.csv"
    if direct.exists():
        return direct
    cleaned_root = Path("processed_data") / "cleaned" / core_data_dir.name
    if not cleaned_root.exists():
        return None
    matches = sorted(cleaned_root.glob(f"*_{symbol.lower()}_*_h4_cleaned.csv"))
    return matches[0] if matches else None


def _resolve_equity_symbol_path(equity_data_dir: Path, symbol: str) -> Path | None:
    for source in ("nasdaq", "yahoo"):
        direct = equity_data_dir / "processed" / source / "D1" / f"{symbol}.csv"
        if direct.exists():
            return direct
    cleaned_root = Path("processed_data") / "cleaned" / equity_data_dir.name
    if not cleaned_root.exists():
        return None
    matches = sorted(cleaned_root.glob(f"*_{symbol.lower()}_*_d1_cleaned.csv"))
    return matches[0] if matches else None


def load_csv(path: Path) -> pd.DataFrame:
    # One parse instead of an initial sampling read plus a complete reread.
    # This compatibility loader remains an offline research helper, not a causal feed.
    df = pd.read_csv(path)
    time_col = "time" if "time" in df.columns else "date"
    df[time_col] = pd.to_datetime(df[time_col], utc=True)
    return df.set_index(time_col).sort_index()[["open", "high", "low", "close", "volume", "spread"]]


def h4_to_d1(df: pd.DataFrame) -> pd.DataFrame:
    grouped = df.resample("1D", label="left", closed="left")
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
            "spread": grouped["spread"].median(),
        }
    )
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[
        (out["open"] > 0)
        & (out["high"] > 0)
        & (out["low"] > 0)
        & (out["close"] > 0)
        & (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    ]
    return out


def trim_data(
    all_data: dict[str, pd.DataFrame],
    start: str | None = None,
    end: str | None = None,
) -> dict[str, pd.DataFrame]:
    out = all_data
    if start:
        start_ts = pd.Timestamp(start, tz="UTC")
        out = {symbol: df.loc[df.index >= start_ts] for symbol, df in out.items()}
    if end:
        end_ts = pd.Timestamp(end, tz="UTC")
        out = {symbol: df.loc[df.index <= end_ts] for symbol, df in out.items()}
    return {symbol: df for symbol, df in out.items() if not df.empty}


def align_data(
    data: dict[str, pd.DataFrame],
    align_start: bool,
    align_end: bool,
) -> dict[str, pd.DataFrame]:
    if not data:
        return data
    start = max(df.index[0] for df in data.values()) if align_start else None
    end = min(df.index[-1] for df in data.values()) if align_end else None
    aligned: dict[str, pd.DataFrame] = {}
    for symbol, df in data.items():
        out = df
        if start is not None:
            out = out.loc[out.index >= start]
        if end is not None:
            out = out.loc[out.index <= end]
        if not out.empty:
            aligned[symbol] = out
    return aligned


def rules_3x(include_equities: bool) -> TurtleRules:
    cluster_1n = {
        "precious_metals": 0.04,
        "crypto": 0.04,
        "semiconductors": 0.025,
        "broad_equity": 0.025,
        "consumer": 0.025,
        "software": 0.025,
        "communication": 0.02,
        "mega_cap_tech": 0.02,
    }
    cluster_leverage = {
        "precious_metals": 1.5,
        "crypto": 1.5,
        "semiconductors": 0.8,
        "broad_equity": 0.8,
        "consumer": 0.8,
        "software": 0.8,
        "communication": 0.6,
        "mega_cap_tech": 0.5,
    }
    if not include_equities:
        cluster_1n = {"precious_metals": 0.04, "crypto": 0.04}
        cluster_leverage = {"precious_metals": 1.5, "crypto": 1.5}

    return TurtleRules(
        n_period=20,
        fast_entry=20,
        slow_entry=55,
        fast_exit=10,
        slow_exit=20,
        stop_n=2.0,
        pyramid_step_n=0.5,
        trigger_mode="close",
        allow_short=True,
        max_total_1n_risk_pct=0.12,
        max_direction_1n_risk_pct=0.08,
        default_cluster_1n_risk_pct=0.02,
        cluster_1n_risk_pct=cluster_1n,
        max_total_leverage=3.0,
        max_direction_leverage=2.0,
        default_cluster_leverage=0.5,
        cluster_leverage=cluster_leverage,
    )


def build_specs(
    symbols: list[str],
    equity_short: bool,
    include_equities: bool,
) -> dict[str, AssetSpec]:
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        if symbol == "XAUUSD_DUKAS":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="metal",
                cluster="precious_metals",
                point_value=1.0,
                qty_step=0.01,
                min_qty=0.01,
                can_short=False,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=1.0,
                slippage_bps=3.0,
            )
        elif symbol == "XAGUSD_DUKAS":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="metal",
                cluster="precious_metals",
                point_value=1.0,
                qty_step=1.0,
                min_qty=1.0,
                can_short=False,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=1.5,
                slippage_bps=5.0,
            )
        elif symbol == "BTCUSDT_BINANCE":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="crypto",
                cluster="crypto",
                point_value=1.0,
                qty_step=0.0001,
                min_qty=0.0001,
                can_short=True,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=4.0,
                slippage_bps=8.0,
            )
        elif symbol == "ETHUSDT_BINANCE":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="crypto",
                cluster="crypto",
                point_value=1.0,
                qty_step=0.001,
                min_qty=0.001,
                can_short=True,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=4.0,
                slippage_bps=8.0,
            )
        else:
            cluster = EQUITY_CLUSTERS.get(symbol, "single_stock")
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="etf" if symbol in {"QQQ", "SPY", "XLY"} else "equity",
                cluster=cluster if include_equities else "us_equity",
                point_value=1.0,
                qty_step=1.0,
                min_qty=1.0,
                can_short=equity_short,
                max_units=3,
                unit_1n_risk_pct=0.0025,
                max_symbol_1n_risk_pct=0.01,
                max_symbol_leverage=0.30 if symbol not in {"QQQ", "SPY"} else 0.50,
                cost_bps=1.0,
                slippage_bps=5.0,
            )
    return specs


def build_run_specs(
    symbols: list[str],
    eth_mode: str = "long_short",
    *,
    equity_short: bool = False,
    include_equities: bool | None = None,
    cost_multiplier: float = 1.0,
) -> dict[str, AssetSpec]:
    include_equities = any(symbol in EQUITY_SYMBOLS for symbol in symbols) if include_equities is None else include_equities
    specs = build_specs(symbols, equity_short=equity_short, include_equities=include_equities)
    updated: dict[str, AssetSpec] = {}
    for symbol, spec in specs.items():
        next_spec = replace(
            spec,
            cost_bps=spec.cost_bps * cost_multiplier,
            slippage_bps=spec.slippage_bps * cost_multiplier,
        )
        if symbol == "ETHUSDT_BINANCE":
            if eth_mode == "short_only":
                next_spec = replace(next_spec, can_long=False, can_short=True)
            elif eth_mode == "excluded":
                next_spec = replace(next_spec, can_long=False, can_short=False)
        updated[symbol] = next_spec
    return updated


def apply_rule_overrides(
    rules: TurtleRules,
    *,
    cluster_leverage: dict[str, float] | None = None,
    rule_overrides: dict[str, Any] | None = None,
) -> TurtleRules:
    out = rules
    if cluster_leverage:
        merged = dict(out.cluster_leverage)
        merged.update(cluster_leverage)
        out = replace(out, cluster_leverage=merged)
    if rule_overrides:
        out = replace(out, **rule_overrides)
    return out


def symbol_trade_stats(trades: pd.DataFrame, symbols: list[str]) -> dict[str, float]:
    stats: dict[str, float] = {}
    if trades.empty:
        return stats
    for symbol in symbols:
        part = trades.loc[trades["symbol"] == symbol]
        if part.empty:
            stats[f"{symbol}_pnl"] = 0.0
            stats[f"{symbol}_trades"] = 0
            stats[f"{symbol}_short_pnl"] = 0.0
            continue
        stats[f"{symbol}_pnl"] = float(part["pnl"].sum())
        stats[f"{symbol}_trades"] = int(len(part))
        stats[f"{symbol}_short_pnl"] = float(part.loc[part["side"] == SHORT, "pnl"].sum())
    return stats


def max_leverage_stats(
    orders: pd.DataFrame,
    equity_curve: pd.Series,
) -> dict[str, float]:
    if orders.empty:
        return {
            "max_total_order_notional_to_equity": 0.0,
            "max_single_order_notional_to_equity": 0.0,
        }
    enriched = orders.copy()
    enriched["time"] = pd.to_datetime(enriched["time"], utc=True)
    enriched["equity"] = enriched["time"].map(equity_curve)
    enriched["notional_to_equity"] = enriched["notional"] / enriched["equity"]
    return {
        "max_single_order_notional_to_equity": float(enriched["notional_to_equity"].max()),
        "sum_order_notional_to_initial_equity": float(enriched["notional"].sum() / equity_curve.iloc[0]),
    }


def drawdown_window(equity_curve: pd.Series) -> dict[str, object]:
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    trough = drawdown.idxmin()
    peak = equity_curve.loc[:trough].idxmax()
    recovery_slice = equity_curve.loc[trough:]
    recovery = recovery_slice[recovery_slice >= equity_curve.loc[peak]]
    return {
        "dd_peak": peak,
        "dd_trough": trough,
        "dd_recovery": recovery.index[0] if not recovery.empty else "",
    }


def run_backtest(
    *,
    all_data: dict[str, pd.DataFrame],
    symbols: list[str],
    initial_equity: float,
    align_start: bool = True,
    align_end: bool = True,
    start: str | None = None,
    end: str | None = None,
    eth_mode: str = "long_short",
    equity_short: bool = False,
    include_equities: bool | None = None,
    cost_multiplier: float = 1.0,
    rules: TurtleRules | None = None,
    cluster_leverage: dict[str, float] | None = None,
    rule_overrides: dict[str, Any] | None = None,
    allow_partial_universe: bool = False,
) -> tuple[Any, dict[str, pd.DataFrame], dict[str, AssetSpec], TurtleRules]:
    missing = [symbol for symbol in symbols if symbol not in all_data]
    if missing and not allow_partial_universe:
        raise ValueError(f"missing requested symbols: {missing}")
    available = [symbol for symbol in symbols if symbol in all_data]
    data = {symbol: all_data[symbol] for symbol in available}
    # Preserve pre-start bars so N and breakout channels are warm at the
    # evaluation boundary. The backtester suppresses trading before `start`.
    data = trim_data(data, end=end)
    data = align_data(data, align_start=align_start, align_end=align_end)
    missing_after_alignment = [symbol for symbol in available if symbol not in data]
    if missing_after_alignment and not allow_partial_universe:
        raise ValueError(
            f"requested symbols empty after date alignment: {missing_after_alignment}"
        )
    if len(data) < 2:
        raise ValueError(f"not enough non-empty symbols for run: {symbols}")
    include_equities = any(symbol in EQUITY_SYMBOLS for symbol in data) if include_equities is None else include_equities
    specs = build_run_specs(
        list(data),
        eth_mode=eth_mode,
        equity_short=equity_short,
        include_equities=include_equities,
        cost_multiplier=cost_multiplier,
    )
    active_rules = rules or rules_3x(include_equities=include_equities)
    active_rules = apply_rule_overrides(
        active_rules,
        cluster_leverage=cluster_leverage,
        rule_overrides=rule_overrides,
    )
    result = UnifiedBacktestExecutor(
        data=data,
        specs=specs,
        rules=active_rules,
        config=BacktestConfig(initial_equity=initial_equity),
        evaluation_start=start,
    ).run()
    return result, data, specs, active_rules


def write_backtest_outputs(result: Any, out_dir: Path) -> None:
    write_single_backtest_outputs(result, out_dir)
