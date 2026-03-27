from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List

import numpy as np
import pandas as pd


@dataclass
class InstitutionalMetrics:
    final_equity: float
    total_return: float
    cagr: float
    annual_volatility: float
    sharpe_rf0: float
    sortino_rf0: float
    max_drawdown: float
    calmar: float
    max_drawdown_duration_days: float
    daily_var_95: float
    daily_cvar_95: float
    trade_count: int
    win_rate: float
    profit_factor: float
    payoff_ratio: float
    avg_net_pnl_per_trade: float
    avg_holding_hours: float
    gross_pnl_total: float
    net_pnl_total: float
    total_fees: float
    total_slippage: float
    fee_to_abs_gross_pnl: float
    exposure_ratio_proxy: float
    exit_reason_distribution: Dict[str, float]
    direction_net_pnl: Dict[str, float]
    symbol_net_pnl: Dict[str, float]


def _safe_float(v: float | int | np.floating) -> float:
    return float(v) if np.isfinite(v) else 0.0


def _max_drawdown_duration_days(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    roll_max = equity.cummax()
    under = equity < roll_max
    if not under.any():
        return 0.0

    max_duration = pd.Timedelta(0)
    start_ts = None
    for ts, is_under in under.items():
        if is_under and start_ts is None:
            start_ts = ts
        if (not is_under) and start_ts is not None:
            max_duration = max(max_duration, ts - start_ts)
            start_ts = None
    if start_ts is not None:
        max_duration = max(max_duration, equity.index[-1] - start_ts)
    return max_duration.total_seconds() / 86400.0


def _daily_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    daily_eq = equity.resample("1D").last().dropna()
    return daily_eq.pct_change().dropna()


def _build_closed_trades(fills: pd.DataFrame) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "side",
                "entry_ts",
                "exit_ts",
                "holding_hours",
                "gross_pnl",
                "fees",
                "slippage",
                "net_pnl",
                "exit_reason",
            ]
        )

    fills = fills.sort_values("timestamp").copy()
    states: Dict[str, Dict[str, float | str | pd.Timestamp | None]] = {}
    trades: List[dict] = []

    for _, row in fills.iterrows():
        symbol = str(row["symbol"])
        key = symbol
        action = str(row["action"])
        side = str(row["side"])
        ts = pd.Timestamp(row["timestamp"])
        px = float(row["price"])
        qty = float(row["quantity"])
        fee = float(row.get("fee", 0.0))
        slip = float(row.get("slippage_cost", 0.0))
        reason = str(row.get("reason", ""))

        st = states.get(key)
        if st is None:
            st = {
                "side": side,
                "qty": 0.0,
                "avg_entry": 0.0,
                "entry_ts": None,
                "gross_pnl": 0.0,
                "fees": 0.0,
                "slippage": 0.0,
                "exit_reason": "",
            }
            states[key] = st

        if action in {"open", "add"}:
            old_qty = float(st["qty"])
            new_qty = old_qty + qty
            avg_entry = float(st["avg_entry"])
            if new_qty > 0:
                st["avg_entry"] = (avg_entry * old_qty + px * qty) / new_qty
            st["qty"] = new_qty
            st["side"] = side
            st["fees"] = float(st["fees"]) + fee
            st["slippage"] = float(st["slippage"]) + slip
            if st["entry_ts"] is None:
                st["entry_ts"] = ts
            continue

        # reduce / close
        open_qty = float(st["qty"])
        if open_qty <= 0:
            continue
        close_qty = min(qty, open_qty)
        avg_entry = float(st["avg_entry"])
        pnl = (px - avg_entry) * close_qty if st["side"] == "long" else (avg_entry - px) * close_qty
        st["gross_pnl"] = float(st["gross_pnl"]) + pnl
        st["fees"] = float(st["fees"]) + fee * (close_qty / qty if qty > 0 else 1.0)
        st["slippage"] = float(st["slippage"]) + slip * (close_qty / qty if qty > 0 else 1.0)
        st["qty"] = open_qty - close_qty
        st["exit_reason"] = reason or str(st["exit_reason"])

        if float(st["qty"]) <= 1e-12:
            entry_ts = pd.Timestamp(st["entry_ts"]) if st["entry_ts"] is not None else ts
            exit_ts = ts
            holding_hours = (exit_ts - entry_ts).total_seconds() / 3600.0
            gross = float(st["gross_pnl"])
            fees = float(st["fees"])
            slippage = float(st["slippage"])
            net = gross - fees - slippage
            trades.append(
                {
                    "symbol": symbol,
                    "side": str(st["side"]),
                    "entry_ts": entry_ts,
                    "exit_ts": exit_ts,
                    "holding_hours": holding_hours,
                    "gross_pnl": gross,
                    "fees": fees,
                    "slippage": slippage,
                    "net_pnl": net,
                    "exit_reason": str(st["exit_reason"]),
                }
            )
            states[key] = {
                "side": "",
                "qty": 0.0,
                "avg_entry": 0.0,
                "entry_ts": None,
                "gross_pnl": 0.0,
                "fees": 0.0,
                "slippage": 0.0,
                "exit_reason": "",
            }

    return pd.DataFrame(trades)


def build_institutional_metrics(equity: pd.Series, fills: pd.DataFrame, initial_equity: float) -> InstitutionalMetrics:
    if equity.index.tz is None:
        equity.index = equity.index.tz_localize("UTC")
    else:
        equity.index = equity.index.tz_convert("UTC")

    equity = equity.sort_index()
    final_equity = float(equity.iloc[-1]) if not equity.empty else float(initial_equity)
    total_return = (final_equity - initial_equity) / initial_equity if initial_equity > 0 else 0.0

    period_days = max((equity.index[-1] - equity.index[0]).total_seconds() / 86400.0, 0.0) if len(equity) > 1 else 0.0
    period_years = period_days / 365.25 if period_days > 0 else 0.0
    cagr = (final_equity / initial_equity) ** (1.0 / period_years) - 1.0 if period_years > 0 and initial_equity > 0 and final_equity > 0 else 0.0

    dret = _daily_returns(equity)
    annual_vol = float(dret.std(ddof=1) * np.sqrt(365.0)) if len(dret) > 1 else 0.0
    sharpe = float((dret.mean() / dret.std(ddof=1)) * np.sqrt(365.0)) if len(dret) > 1 and dret.std(ddof=1) > 0 else 0.0
    downside_diff = np.minimum(dret.values, 0.0) if len(dret) > 0 else np.array([])
    downside_dev_daily = float(np.sqrt(np.mean(np.square(downside_diff)))) if len(downside_diff) > 0 else 0.0
    sortino = float((dret.mean() * np.sqrt(365.0)) / downside_dev_daily) if downside_dev_daily > 0 else 0.0

    if equity.empty:
        max_dd = 0.0
    else:
        max_dd = float((equity / equity.cummax() - 1.0).min())
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0
    dd_duration_days = _max_drawdown_duration_days(equity)

    var95 = float(dret.quantile(0.05)) if len(dret) > 0 else 0.0
    cvar95 = float(dret[dret <= var95].mean()) if len(dret[dret <= var95]) > 0 else 0.0

    closed = _build_closed_trades(fills)
    trade_count = int(len(closed))
    wins = closed[closed["net_pnl"] > 0]
    losses = closed[closed["net_pnl"] < 0]
    win_rate = float(len(wins) / trade_count) if trade_count > 0 else 0.0

    gross_profit = float(wins["net_pnl"].sum()) if not wins.empty else 0.0
    gross_loss_abs = float(abs(losses["net_pnl"].sum())) if not losses.empty else 0.0
    profit_factor = gross_profit / gross_loss_abs if gross_loss_abs > 0 else 0.0

    avg_win = float(wins["net_pnl"].mean()) if not wins.empty else 0.0
    avg_loss_abs = float(abs(losses["net_pnl"].mean())) if not losses.empty else 0.0
    payoff_ratio = avg_win / avg_loss_abs if avg_loss_abs > 0 else 0.0

    avg_net = float(closed["net_pnl"].mean()) if trade_count > 0 else 0.0
    avg_hold_h = float(closed["holding_hours"].mean()) if trade_count > 0 else 0.0

    gross_pnl_total = float(closed["gross_pnl"].sum()) if trade_count > 0 else 0.0
    total_fees = float(fills["fee"].sum()) if (not fills.empty and "fee" in fills.columns) else 0.0
    total_slippage = float(fills["slippage_cost"].sum()) if (not fills.empty and "slippage_cost" in fills.columns) else 0.0
    net_pnl_total = final_equity - initial_equity
    fee_to_abs_gross = total_fees / abs(gross_pnl_total) if abs(gross_pnl_total) > 0 else 0.0

    exit_dist: Dict[str, float] = {}
    direction_pnl: Dict[str, float] = {}
    symbol_pnl: Dict[str, float] = {}
    if trade_count > 0:
        exit_share = closed.groupby("exit_reason")["net_pnl"].count() / trade_count
        exit_dist = {k: _safe_float(v) for k, v in exit_share.items()}
        direction_net = closed.groupby("side")["net_pnl"].sum()
        direction_pnl = {k: _safe_float(v) for k, v in direction_net.items()}
        symbol_net = closed.groupby("symbol")["net_pnl"].sum()
        symbol_pnl = {k: _safe_float(v) for k, v in symbol_net.items()}

    # Proxy exposure: sum of closed-trade holding durations divided by total backtest span.
    total_span_h = period_days * 24.0
    exposure_ratio = float(closed["holding_hours"].sum() / total_span_h) if total_span_h > 0 and trade_count > 0 else 0.0

    return InstitutionalMetrics(
        final_equity=_safe_float(final_equity),
        total_return=_safe_float(total_return),
        cagr=_safe_float(cagr),
        annual_volatility=_safe_float(annual_vol),
        sharpe_rf0=_safe_float(sharpe),
        sortino_rf0=_safe_float(sortino),
        max_drawdown=_safe_float(max_dd),
        calmar=_safe_float(calmar),
        max_drawdown_duration_days=_safe_float(dd_duration_days),
        daily_var_95=_safe_float(var95),
        daily_cvar_95=_safe_float(cvar95),
        trade_count=trade_count,
        win_rate=_safe_float(win_rate),
        profit_factor=_safe_float(profit_factor),
        payoff_ratio=_safe_float(payoff_ratio),
        avg_net_pnl_per_trade=_safe_float(avg_net),
        avg_holding_hours=_safe_float(avg_hold_h),
        gross_pnl_total=_safe_float(gross_pnl_total),
        net_pnl_total=_safe_float(net_pnl_total),
        total_fees=_safe_float(total_fees),
        total_slippage=_safe_float(total_slippage),
        fee_to_abs_gross_pnl=_safe_float(fee_to_abs_gross),
        exposure_ratio_proxy=_safe_float(exposure_ratio),
        exit_reason_distribution=exit_dist,
        direction_net_pnl=direction_pnl,
        symbol_net_pnl=symbol_pnl,
    )


def metrics_to_dict(metrics: InstitutionalMetrics) -> dict:
    return asdict(metrics)
