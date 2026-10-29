"""RESEARCH_ONLY multi-asset acceptance backtest.

The final test window starts at 2025-01-01 UTC and is not an input to
candidate choice, baselines, walk-forward, or cost stress. Those steps receive
only bars strictly before that timestamp. Results are not a live-return claim.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, TurtleRules
from inv_trend.adapters.multi_asset.strategy.indicators import compute_turtle_indicators
from inv_trend.core.performance import analyze_equity
from inv_trend.core.执行约束 import ExecutionPolicy
from inv_trend.data.lineage import load_current_lineage
from inv_trend.data.storage import open_data_lake

HOLDOUT_START = pd.Timestamp("2025-01-01", tz="UTC")
CLOSED_WINDOW_END = pd.Timestamp("2026-08-26", tz="UTC")
# Locked after the shipped updater published this tail and before any
# strategy evaluation of it. The closed window above is not scored again.
NEXT_WINDOW_START = pd.Timestamp("2026-08-27", tz="UTC")
NEXT_WINDOW_END = pd.Timestamp("2026-10-09", tz="UTC")
# Locked while the 2026-10-10 UTC candle was still open, before any
# strategy evaluation of that session. The two windows above stay closed.
LATER_WINDOW_START = pd.Timestamp("2026-10-10", tz="UTC")
LATER_WINDOW_END = pd.Timestamp("2026-10-10", tz="UTC")
FROZEN_NEXT_CANDIDATE = "trend_filter"
# Frozen validation checkpoint of trend_filter. Study B may not move this book.
FROZEN_VALIDATION_CHECKPOINT = {
    "sharpe": 0.9211,
    "net_profit": 51593.17,
    "max_drawdown": -0.2152,
    "trade_count": 28,
}
STUDY_B_TEST_START = pd.Timestamp("2027-01-01", tz="UTC")
STUDY_B_TEST_END = pd.Timestamp("2027-06-30", tz="UTC")
TRAIN_START = pd.Timestamp("2018-01-01", tz="UTC")
TRAIN_END = pd.Timestamp("2022-12-31", tz="UTC")
VALIDATION_START = pd.Timestamp("2023-01-01", tz="UTC")
VALIDATION_END = pd.Timestamp("2024-12-31", tz="UTC")
SEED = 0
INITIAL_EQUITY = 100_000.0
PRIORITY_SYMBOLS = ("BTC", "ETH", "XAU", "XAUUSD")
LOCKED_FLOORS = {
    "annualized_net_return": 0.10,
    "max_drawdown": 0.30,
    "sharpe": 0.8,
    "calmar": 0.5,
    "profit_factor": 1.2,
}
COST = {
    "commission_bps": 10.0,
    "spread_bps": 5.0,
    "slippage_bps": 5.0,
    "financing_bps_per_year": 100.0,
    "short_borrow_bps_per_year": 200.0,
    "max_gross_leverage": 1.0,
    "max_symbol_leverage": 0.5,
}
# Pre-registered before the holdout run. Order is the tie break.
CANDIDATES = (
    {"id": "classic", "fast_entry": 20, "slow_entry": 55, "fast_exit": 10, "slow_exit": 20,
     "n_period": 20, "stop_n": 2.0, "skip_fast_after_win": True, "entry_ma_period": 0,
     "fast_system_enabled": True, "slow_system_enabled": True},
    {"id": "slow_only", "fast_entry": 20, "slow_entry": 55, "fast_exit": 10, "slow_exit": 20,
     "n_period": 20, "stop_n": 2.5, "skip_fast_after_win": True, "entry_ma_period": 0,
     "fast_system_enabled": False, "slow_system_enabled": True},
    {"id": "trend_filter", "fast_entry": 20, "slow_entry": 55, "fast_exit": 10, "slow_exit": 20,
     "n_period": 20, "stop_n": 2.0, "skip_fast_after_win": True, "entry_ma_period": 100,
     "fast_system_enabled": True, "slow_system_enabled": True},
    {"id": "no_skip_wide_stop", "fast_entry": 20, "slow_entry": 55, "fast_exit": 10, "slow_exit": 20,
     "n_period": 20, "stop_n": 3.0, "skip_fast_after_win": False, "entry_ma_period": 0,
     "fast_system_enabled": True, "slow_system_enabled": True},
)
WALK_FOLDS = (
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
)
ROOT = Path(__file__).resolve().parents[3]


def assert_research_window(start, end) -> None:
    """Refuse a research slice that touches the locked final test window."""

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    if start_ts >= HOLDOUT_START or end_ts >= HOLDOUT_START:
        raise ValueError("research window entered the locked holdout")


def _rules(spec: dict) -> TurtleRules:
    return TurtleRules(
        fast_entry=spec["fast_entry"], slow_entry=spec["slow_entry"],
        fast_exit=spec["fast_exit"], slow_exit=spec["slow_exit"],
        n_period=spec["n_period"], stop_n=spec["stop_n"],
        skip_fast_after_win=spec["skip_fast_after_win"],
        allow_short=bool(spec.get("allow_short", True)),
        fast_system_enabled=spec["fast_system_enabled"],
        slow_system_enabled=spec["slow_system_enabled"],
        entry_ma_period=spec["entry_ma_period"],
        entry_atr_percentile_max=float(spec.get("entry_atr_percentile_max", 1.0)),
        risk_atr_fraction_target=float(spec.get("risk_atr_fraction_target", 0.0)),
        entry_drawdown_halt=float(spec.get("entry_drawdown_halt", 0.0)),
        max_total_1n_risk_pct=0.04, max_direction_1n_risk_pct=0.03,
        default_cluster_1n_risk_pct=0.04,
        max_total_leverage=COST["max_gross_leverage"],
        max_direction_leverage=COST["max_gross_leverage"],
        default_cluster_leverage=COST["max_gross_leverage"],
    )


def _policy(multiple: float = 1.0) -> ExecutionPolicy:
    return ExecutionPolicy(
        event_clock=False, price_slippage=True,
        max_gross_leverage=COST["max_gross_leverage"],
        financing_bps_per_year=COST["financing_bps_per_year"] * multiple,
        short_borrow_bps_per_year=COST["short_borrow_bps_per_year"] * multiple,
        enforce_capital=True, enforce_volume=False, charge_calendar_carry=True,
        certified=False, reduce_excess_leverage=True,
    )


def _specs(symbols, multiple: float = 1.0) -> dict[str, AssetSpec]:
    return {
        symbol: AssetSpec(
            symbol, "crypto", "crypto", qty_step=0.001, min_qty=0.001, min_notional=10.0,
            can_short=True, max_units=2, unit_1n_risk_pct=0.01, max_symbol_1n_risk_pct=0.02,
            max_symbol_leverage=COST["max_symbol_leverage"],
            cost_bps=COST["commission_bps"] * multiple,
            slippage_bps=COST["slippage_bps"] * multiple,
            spread_bps=COST["spread_bps"] * multiple,
        )
        for symbol in symbols
    }


def _candidate(name: str) -> dict:
    for item in CANDIDATES:
        if item["id"] == name:
            return item
    raise KeyError(name)


def _plain(value):
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    return value


def _code_version() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    return f"{sha}:{digest}"


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "is_complete" in out.columns:
        out = out.loc[out["is_complete"].astype(bool)]
    out = out.sort_values("timestamp")
    if out["timestamp"].duplicated().any() or not out["timestamp"].is_monotonic_increasing:
        raise ValueError("bar timestamps are not strictly increasing")
    if out["timestamp"].dt.tz is None:
        raise ValueError("bar timestamps must be timezone-aware")
    steps = out["timestamp"].diff().dropna().dt.total_seconds()
    if not (steps == 86400).all():
        raise ValueError("certified daily bars are not aligned to 24h steps")
    view = out.loc[:, ["open", "high", "low", "close", "volume"]].copy()
    view.index = pd.DatetimeIndex(out["timestamp"])
    view.index.name = None
    return view


def load_certified(root: Path | None = None) -> dict:
    """Load priority symbols whose current D1 lineage is certified. No returns."""

    lake = open_data_lake(root or (ROOT / "data"), startup_check=False)
    notes = []
    frames = {}
    versions = {}
    for symbol in PRIORITY_SYMBOLS:
        try:
            lineage = load_current_lineage(lake, symbol, "D1")
        except Exception as exc:
            notes.append({"symbol": symbol, "certified": False, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        report = lineage.quality_report
        suitable = report.get("quality_status") == "CURATED" and report.get("backtest_suitable") is True
        note = {
            "symbol": symbol, "certified": suitable, "dataset_version": lineage.dataset_version,
            "rows": report.get("actual_bars"), "missing_intervals": len(report.get("missing_intervals") or []),
            "instrument_id": lineage.dataset_manifest.get("instrument_id"),
        }
        if not suitable:
            note["reason"] = "quality report is not a certified backtest set"
            notes.append(note)
            continue
        raw = lake.read_bars(symbol, "D1")
        if len(raw) != int(report["actual_bars"]):
            raise ValueError(f"{symbol} row count does not match the quality report")
        frames[symbol] = _prepare_frame(raw)
        versions[symbol] = note
        notes.append(note)
    if len(frames) < 2:
        raise RuntimeError("fewer than two certified priority symbols")
    indexes = list(frames.values())
    if any(not left.index.equals(indexes[0].index) for left in indexes[1:]):
        raise ValueError("certified symbol calendars are not aligned")
    causal = _indicator_prefix_stable(indexes[0])
    return {
        "frames": frames, "versions": versions, "notes": notes,
        "indicator_prefix_stable": causal, "label": "RESEARCH_ONLY",
    }


def _indicator_prefix_stable(frame: pd.DataFrame) -> bool:
    rules = _rules(CANDIDATES[0])
    if len(frame) < 120:
        return False
    cut = frame.index[100]
    full = compute_turtle_indicators(frame.iloc[:140], rules)
    prefix = compute_turtle_indicators(frame.iloc[:101], rules)
    columns = [name for name in prefix.columns if name == "n" or name.startswith(("high_", "low_"))]
    left = prefix.loc[cut, columns].to_numpy(dtype=float)
    right = full.loc[cut, columns].to_numpy(dtype=float)
    return bool(np.allclose(left, right, equal_nan=True))


def _before_holdout(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    sliced = {symbol: frame.loc[frame.index < HOLDOUT_START].copy() for symbol, frame in frames.items()}
    if any(frame.empty or frame.index.max() >= HOLDOUT_START for frame in sliced.values()):
        raise ValueError("pre-holdout slice is empty or leaked")
    return sliced


def _metrics(result, initial: float = INITIAL_EQUITY) -> dict:
    equity = result.equity_curve.astype(float)
    analysis = analyze_equity(equity)
    stats = analysis.metrics
    ending = float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    cagr = -1.0 if ending <= 0 else (ending / initial) ** (1.0 / years) - 1.0
    trades = result.trades
    pnl = trades["pnl"].astype(float) if not trades.empty and "pnl" in trades else pd.Series(dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    profit_factor = None
    if not losses.empty and float(losses.sum()) < 0:
        profit_factor = float(wins.sum() / abs(losses.sum())) if not wins.empty else 0.0
    payoff = None
    if not wins.empty and not losses.empty and float(losses.mean()) < 0:
        payoff = float(wins.mean() / abs(losses.mean()))
    orders = result.orders
    def _sum(column: str) -> float:
        if orders.empty or column not in orders:
            return 0.0
        return float(pd.to_numeric(orders[column], errors="coerce").fillna(0.0).sum())
    fee = _sum("fee_cost")
    spread = _sum("spread_cost")
    slippage = _sum("slippage_cost")
    funding = float(result.carry_cost)
    ending = float(equity.iloc[-1])
    by_side = {}
    by_symbol = {}
    if not trades.empty:
        for side, part in trades.groupby("side_name"):
            by_side[str(side)] = _trade_block(part)
        for symbol, part in trades.groupby("symbol"):
            by_symbol[str(symbol)] = _trade_block(part)
    drawdown = float(stats["max_drawdown"])
    sharpe = stats["sharpe_ratio"]
    calmar = (cagr / abs(drawdown)) if drawdown < 0 else None
    return {
        "label": "RESEARCH_ONLY",
        "年化净收益率": cagr,
        "net_profit": ending - initial,
        "ending_equity": ending,
        "max_drawdown": drawdown,
        "sharpe": None if sharpe is None else float(sharpe),
        "calmar": calmar,
        "profit_factor": profit_factor,
        "total_return": float(stats["total_return"]),
        "volatility": stats["volatility"],
        "trade_count": int(len(pnl)),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "payoff_ratio": payoff,
        "commission": fee,
        "spread": spread,
        "slippage": slippage,
        "funding": funding,
        "total_costs": fee + spread + slippage + funding,
        "long_short": by_side,
        "by_symbol": by_symbol,
        "equity_curve": {
            "start": equity.index[0].isoformat(),
            "end": equity.index[-1].isoformat(),
            "start_equity": float(equity.iloc[0]),
            "end_equity": ending,
            "min_equity": float(equity.min()),
            "max_equity": float(equity.max()),
            "points": int(len(equity)),
        },
    }


def _trade_block(part: pd.DataFrame) -> dict:
    pnl = part["pnl"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return {
        "trade_count": int(len(pnl)),
        "net_profit": float(pnl.sum()),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "payoff_ratio": (
            float(wins.mean() / abs(losses.mean()))
            if not wins.empty and not losses.empty and float(losses.mean()) < 0 else None
        ),
    }


def _run(frames, candidate: dict, start, end, *, multiple: float = 1.0, research: bool = False):
    if research:
        assert_research_window(start, end)
    start_ts = pd.Timestamp(start)
    end_ts = None if end is None else pd.Timestamp(end)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    if end_ts is not None and end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    bounded = _bounded_frames(frames, end_ts)
    result = TurtleBacktester(
        bounded, _specs(bounded, multiple), _rules(candidate),
        initial_equity=INITIAL_EQUITY, liquidate_at_end=True,
        evaluation_start=start_ts, evaluation_end=end_ts,
        execution_policy=_policy(multiple),
    ).run()
    metrics = _metrics(result)
    metrics["candidate_id"] = candidate["id"]
    metrics["cost_multiple"] = multiple
    metrics["window"] = {"start": start_ts.isoformat(), "end": None if end_ts is None else end_ts.isoformat()}
    return metrics


def _phase_table(result) -> list[dict]:
    """Describe calendar slices of the single acceptance equity curve."""

    equity = result.equity_curve.astype(float)
    trades = result.trades
    ranges = (
        ("2025H1", "2025-01-01", "2025-06-30"),
        ("2025H2", "2025-07-01", "2025-12-31"),
        ("2026", "2026-01-01", "2026-12-31"),
    )
    rows = []
    for name, start, end in ranges:
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end, tz="UTC")
        sl = equity.loc[(equity.index >= start_ts) & (equity.index <= end_ts)]
        if len(sl) < 2:
            continue
        change = float(sl.iloc[-1] - sl.iloc[0])
        analysis = analyze_equity(sl)
        exits = pd.to_datetime(trades["exit_time"], utc=True) if not trades.empty else pd.Series(dtype="datetime64[ns, UTC]")
        part = trades.loc[(exits >= start_ts) & (exits <= end_ts)] if not trades.empty else trades
        rows.append({
            "phase": name,
            "equity_change": change,
            "phase_return": float(analysis.metrics["total_return"]),
            "max_drawdown": float(analysis.metrics["max_drawdown"]),
            "trade_count": int(len(part)),
            "trade_net_profit": float(part["pnl"].sum()) if not part.empty else 0.0,
        })
    return rows


def _bounded_frames(frames, end_ts):
    bounded = {}
    for symbol, frame in frames.items():
        view = frame.loc[frame.index <= end_ts] if end_ts is not None else frame
        if view.empty:
            raise ValueError(f"{symbol} has no bars in the requested window")
        bounded[symbol] = view
    return bounded


def _eligible(metrics: dict) -> bool:
    sharpe = metrics["sharpe"]
    return bool(
        metrics["net_profit"] > 0
        and metrics["trade_count"] >= 5
        and metrics["max_drawdown"] is not None
        and metrics["max_drawdown"] >= -0.35
        and sharpe is not None
        and math.isfinite(sharpe)
    )


def select_candidate(pre_frames) -> dict:
    """Choose one pre-registered candidate from the validation window only."""

    assert_research_window(VALIDATION_START, VALIDATION_END)
    ranked = []
    for index, candidate in enumerate(CANDIDATES):
        metrics = _run(pre_frames, candidate, VALIDATION_START, VALIDATION_END, research=True)
        ranked.append({
            "id": candidate["id"], "order": index, "eligible": _eligible(metrics),
            "年化净收益率": metrics["年化净收益率"], "net_profit": metrics["net_profit"],
            "max_drawdown": metrics["max_drawdown"], "sharpe": metrics["sharpe"],
            "calmar": metrics["calmar"], "profit_factor": metrics["profit_factor"],
            "trade_count": metrics["trade_count"], "total_costs": metrics["total_costs"],
            "long_short": metrics["long_short"], "by_symbol": metrics["by_symbol"],
            "win_rate": metrics["win_rate"], "payoff_ratio": metrics["payoff_ratio"],
        })
    eligible = [row for row in ranked if row["eligible"]]
    pool = eligible or ranked
    pool.sort(key=lambda row: (
        -(row["sharpe"] if row["sharpe"] is not None else -1e9),
        -(row["calmar"] if row["calmar"] is not None else -1e9),
        row["order"],
    ))
    chosen = pool[0]
    return {
        "label": "RESEARCH_ONLY",
        "chosen_id": chosen["id"],
        "fallback_no_eligible": not bool(eligible),
        "rule": "validation 2023-01-01..2024-12-31 only; net profit > 0; trades >= 5; "
                "max drawdown not worse than -35%; highest Sharpe, then Calmar, then candidate order",
        "trials": ranked,
    }


def prepare_report(bundle=None) -> dict:
    bundle = bundle or load_certified()
    pre = _before_holdout(bundle["frames"])
    baselines = []
    classic = CANDIDATES[0]
    for symbol, frame in pre.items():
        metrics = _run({symbol: frame}, classic, TRAIN_START, VALIDATION_END, research=True)
        baselines.append({"symbol": symbol, "candidate_id": classic["id"], **{
            key: metrics[key] for key in (
                "年化净收益率", "net_profit", "max_drawdown", "sharpe", "calmar", "profit_factor",
                "trade_count", "win_rate", "payoff_ratio", "total_costs", "commission", "spread",
                "slippage", "funding", "long_short", "equity_curve",
            )
        }})
    selection = select_candidate(pre)
    chosen = _candidate(selection["chosen_id"])
    folds = []
    for name, start, end in WALK_FOLDS:
        metrics = _run(pre, chosen, start, end, research=True)
        folds.append({"fold": name, "年化净收益率": metrics["年化净收益率"], "net_profit": metrics["net_profit"],
                      "max_drawdown": metrics["max_drawdown"], "sharpe": metrics["sharpe"],
                      "calmar": metrics["calmar"], "profit_factor": metrics["profit_factor"],
                      "trade_count": metrics["trade_count"], "total_costs": metrics["total_costs"]})
    stress = []
    for multiple in (1.0, 2.0):
        metrics = _run(pre, chosen, VALIDATION_START, VALIDATION_END, multiple=multiple, research=True)
        stress.append({"cost_multiple": multiple, "年化净收益率": metrics["年化净收益率"],
                       "net_profit": metrics["net_profit"], "max_drawdown": metrics["max_drawdown"],
                       "sharpe": metrics["sharpe"], "total_costs": metrics["total_costs"],
                       "commission": metrics["commission"], "spread": metrics["spread"],
                       "slippage": metrics["slippage"], "funding": metrics["funding"]})
    train = _run(pre, chosen, TRAIN_START, TRAIN_END, research=True)
    return {
        "label": "RESEARCH_ONLY",
        "status": "IN_PROGRESS",
        "seed": SEED,
        "code_version": _code_version(),
        "holdout_start": HOLDOUT_START.isoformat(),
        "holdout_used_for_tuning": False,
        "floors": LOCKED_FLOORS,
        "costs": COST,
        "data": bundle["versions"],
        "certification_notes": bundle["notes"],
        "indicator_prefix_stable": bundle["indicator_prefix_stable"],
        "baselines": baselines,
        "selection": selection,
        "train": {key: train[key] for key in (
            "年化净收益率", "net_profit", "max_drawdown", "sharpe", "calmar", "profit_factor",
            "trade_count", "win_rate", "payoff_ratio", "total_costs", "long_short", "by_symbol",
        )},
        "walk_forward": folds,
        "cost_sensitivity": stress,
    }


def _gate(metrics: dict) -> dict:
    drawdown = metrics["max_drawdown"]
    checks = {
        "annualized_net_return": metrics["年化净收益率"] is not None and metrics["年化净收益率"] > LOCKED_FLOORS["annualized_net_return"],
        "net_profit": metrics["net_profit"] > 0,
        "max_drawdown": drawdown is not None and abs(drawdown) <= LOCKED_FLOORS["max_drawdown"] + 1e-12,
        "sharpe": metrics["sharpe"] is not None and metrics["sharpe"] >= LOCKED_FLOORS["sharpe"],
        "calmar": metrics["calmar"] is not None and metrics["calmar"] >= LOCKED_FLOORS["calmar"],
        "profit_factor": metrics["profit_factor"] is not None and metrics["profit_factor"] >= LOCKED_FLOORS["profit_factor"],
        "costs_present": metrics["commission"] > 0 and metrics["spread"] > 0 and metrics["slippage"] > 0 and metrics["funding"] > 0,
    }
    return {"passed": all(checks.values()), "checks": checks}


def acceptance_report(bundle=None) -> dict:
    """The 2025-01-01 window was evaluated once and must not be scored again."""

    raise ValueError(
        "closed holdout 2025-01-01 through 2026-08-26 was already evaluated"
    )


def next_window_report(bundle=None) -> dict:
    """Score only the locked tail. The closed holdout is not an input."""

    if NEXT_WINDOW_START <= CLOSED_WINDOW_END or NEXT_WINDOW_END < NEXT_WINDOW_START:
        raise ValueError("next window overlaps the closed holdout")
    bundle = bundle or load_certified()
    for symbol, frame in bundle["frames"].items():
        if NEXT_WINDOW_START not in frame.index or NEXT_WINDOW_END not in frame.index:
            raise ValueError(f"{symbol} does not contain the locked next window")
        if frame.index.max() > NEXT_WINDOW_END:
            raise ValueError(f"{symbol} extends past the locked next window")
    chosen = _candidate(FROZEN_NEXT_CANDIDATE)
    bounded = _bounded_frames(bundle["frames"], NEXT_WINDOW_END)
    result = TurtleBacktester(
        bounded, _specs(bounded), _rules(chosen),
        initial_equity=INITIAL_EQUITY, liquidate_at_end=True,
        evaluation_start=NEXT_WINDOW_START, evaluation_end=NEXT_WINDOW_END,
        execution_policy=_policy(),
    ).run()
    oos = _metrics(result)
    equity_start = pd.Timestamp(oos["equity_curve"]["start"])
    equity_end = pd.Timestamp(oos["equity_curve"]["end"])
    if equity_start < NEXT_WINDOW_START or equity_end > NEXT_WINDOW_END:
        raise ValueError("evaluation included bars outside the locked next window")
    oos["candidate_id"] = chosen["id"]
    oos["window"] = {"start": NEXT_WINDOW_START.isoformat(), "end": NEXT_WINDOW_END.isoformat()}
    gate = _gate(oos)
    robustness = {
        "independent_phases": 1,
        "required_independent_phases": 2,
        "passed": False,
        "reason": "2026-08-27 through 2026-10-09 is one phase; the closed phase already missed Sharpe",
    }
    return {
        "label": "RESEARCH_ONLY",
        "status": "BLOCKED",
        "seed": SEED,
        "code_version": _code_version(),
        "holdout_used_for_tuning": False,
        "closed_window_rescored": False,
        "floors": LOCKED_FLOORS,
        "costs": COST,
        "data": bundle["versions"],
        "chosen_id": chosen["id"],
        "selection_repeated": False,
        "gate": gate,
        "robustness": robustness,
        "oos": oos,
    }


def later_window_report(bundle=None) -> dict:
    """Score only 2026-10-10. Both earlier windows stay closed."""

    if (
        LATER_WINDOW_START <= NEXT_WINDOW_END
        or LATER_WINDOW_START <= CLOSED_WINDOW_END
        or LATER_WINDOW_END < LATER_WINDOW_START
    ):
        raise ValueError("later window overlaps a closed window")
    bundle = bundle or load_certified()
    for symbol, frame in bundle["frames"].items():
        if LATER_WINDOW_START not in frame.index or LATER_WINDOW_END not in frame.index:
            raise ValueError(f"{symbol} does not contain the locked later window")
        if frame.index.max() > LATER_WINDOW_END:
            raise ValueError(f"{symbol} extends past the locked later window")
    chosen = _candidate(FROZEN_NEXT_CANDIDATE)
    bounded = _bounded_frames(bundle["frames"], LATER_WINDOW_END)
    result = TurtleBacktester(
        bounded, _specs(bounded), _rules(chosen),
        initial_equity=INITIAL_EQUITY, liquidate_at_end=True,
        evaluation_start=LATER_WINDOW_START, evaluation_end=LATER_WINDOW_END,
        execution_policy=_policy(),
    ).run()
    oos = _metrics(result)
    equity_start = pd.Timestamp(oos["equity_curve"]["start"])
    equity_end = pd.Timestamp(oos["equity_curve"]["end"])
    if equity_start < LATER_WINDOW_START or equity_end > LATER_WINDOW_END:
        raise ValueError("evaluation included bars outside the locked later window")
    oos["candidate_id"] = chosen["id"]
    oos["window"] = {"start": LATER_WINDOW_START.isoformat(), "end": LATER_WINDOW_END.isoformat()}
    gate = _gate(oos)
    robustness = {
        "independent_phases": 1,
        "required_independent_phases": 2,
        "passed": False,
        "reason": (
            "2026-10-10 is one session. The closed window missed Sharpe and "
            "2026-08-27 through 2026-10-09 lost money."
        ),
    }
    return {
        "label": "RESEARCH_ONLY",
        "status": "COMPLETED" if gate["passed"] and robustness["passed"] else "BLOCKED",
        "seed": SEED,
        "code_version": _code_version(),
        "holdout_used_for_tuning": False,
        "closed_window_rescored": False,
        "floors": LOCKED_FLOORS,
        "costs": COST,
        "data": bundle["versions"],
        "chosen_id": chosen["id"],
        "selection_repeated": False,
        "gate": gate,
        "robustness": robustness,
        "oos": oos,
    }


def _study_b_specs() -> tuple[dict, ...]:
    """Two extensions of the frozen trend_filter row. Order is the tie break."""

    base = dict(CANDIDATES[2])
    if base["id"] != "trend_filter":
        raise RuntimeError("study B must extend the frozen trend_filter row")
    return (
        {**base, "id": "vol_cap", "entry_atr_percentile_max": 0.8},
        {**base, "id": "long_only", "allow_short": False},
    )


def _checkpoint_ok(metrics: dict) -> bool:
    checkpoint = FROZEN_VALIDATION_CHECKPOINT
    sharpe = metrics["sharpe"]
    drawdown = metrics["max_drawdown"]
    return bool(
        sharpe is not None
        and abs(float(sharpe) - checkpoint["sharpe"]) <= 0.005
        and abs(float(metrics["net_profit"]) - checkpoint["net_profit"]) <= 50.0
        and drawdown is not None
        and abs(float(drawdown) - checkpoint["max_drawdown"]) <= 0.005
        and int(metrics["trade_count"]) == checkpoint["trade_count"]
    )


def _study_row(spec: dict, pre) -> dict:
    validation = _run(pre, spec, VALIDATION_START, VALIDATION_END, research=True)
    train = _run(pre, spec, TRAIN_START, TRAIN_END, research=True)
    folds = []
    for name, start, end in WALK_FOLDS:
        fold = _run(pre, spec, start, end, research=True)
        folds.append({
            "fold": name,
            "年化净收益率": fold["年化净收益率"],
            "net_profit": fold["net_profit"],
            "max_drawdown": fold["max_drawdown"],
            "sharpe": fold["sharpe"],
            "calmar": fold["calmar"],
            "profit_factor": fold["profit_factor"],
            "trade_count": fold["trade_count"],
            "total_costs": fold["total_costs"],
        })
    keys = (
        "年化净收益率", "net_profit", "max_drawdown", "sharpe", "calmar", "profit_factor",
        "trade_count", "win_rate", "payoff_ratio", "total_costs", "long_short", "by_symbol",
    )
    return {
        "id": spec["id"],
        "eligible": _eligible(validation),
        "validation": {key: validation[key] for key in keys},
        "train": {key: train[key] for key in keys},
        "walk_forward": folds,
    }


def _adopt_study_b(reference: dict, trials: list[dict], checkpoint_ok: bool) -> dict:
    """Apply the rule locked before the run. No holdout metric is an input."""

    adopted = []
    reference_sharpe = reference["sharpe"]
    for order, row in enumerate(trials):
        train_dd = row["train"]["max_drawdown"]
        validation_sharpe = row["validation"]["sharpe"]
        reasons = []
        if not checkpoint_ok:
            reasons.append("reference checkpoint moved")
        if not row["eligible"]:
            reasons.append("validation eligibility failed")
        if (
            reference_sharpe is None
            or validation_sharpe is None
            or validation_sharpe <= reference_sharpe
        ):
            reasons.append("validation Sharpe did not beat the frozen reference")
        if train_dd is None or train_dd < -0.30:
            reasons.append("train max drawdown worse than -30%")
        if not reasons:
            adopted.append((order, row))
    adopted.sort(key=lambda item: (
        -(item[1]["validation"]["sharpe"]),
        -(item[1]["validation"]["calmar"] if item[1]["validation"]["calmar"] is not None else -1e9),
        item[0],
    ))
    chosen = adopted[0][1]["id"] if adopted else None
    return {
        "adopted_id": chosen,
        "rule": (
            "validation eligibility; validation Sharpe above the frozen trend_filter "
            "checkpoint; train max drawdown no worse than -30%; then Calmar, then order"
        ),
        "future_test_opened": chosen is not None,
    }


def study_b_report(bundle=None) -> dict:
    """Compare two pre-registered variants on bars strictly before 2025-01-01."""

    assert_research_window(TRAIN_START, TRAIN_END)
    assert_research_window(VALIDATION_START, VALIDATION_END)
    for _name, start, end in WALK_FOLDS:
        assert_research_window(start, end)
    if STUDY_B_TEST_START <= pd.Timestamp("2026-12-31", tz="UTC"):
        raise ValueError("study B test overlaps a reserved or closed window")
    bundle = bundle or load_certified()
    pre = _before_holdout(bundle["frames"])
    reference = _run(pre, _candidate(FROZEN_NEXT_CANDIDATE), VALIDATION_START, VALIDATION_END, research=True)
    checkpoint_ok = _checkpoint_ok(reference)
    trials = [_study_row(spec, pre) for spec in _study_b_specs()]
    decision = _adopt_study_b(reference, trials, checkpoint_ok)
    return {
        "label": "RESEARCH_ONLY",
        "status": "IN_PROGRESS" if decision["adopted_id"] else "BLOCKED",
        "seed": SEED,
        "code_version": _code_version(),
        "holdout_used_for_tuning": False,
        "closed_windows_rescored": False,
        "floors": LOCKED_FLOORS,
        "costs": COST,
        "reference_id": FROZEN_NEXT_CANDIDATE,
        "reference_checkpoint_ok": checkpoint_ok,
        "reference_validation": {
            key: reference[key] for key in (
                "年化净收益率", "net_profit", "max_drawdown", "sharpe", "calmar",
                "profit_factor", "trade_count", "total_costs", "long_short",
            )
        },
        "trials": trials,
        "decision": decision,
        "future_test": None if decision["adopted_id"] is None else {
            "start": STUDY_B_TEST_START.isoformat(),
            "end": STUDY_B_TEST_END.isoformat(),
            "scored": False,
        },
    }


def _study_c_spec() -> dict:
    base = dict(CANDIDATES[2])
    if base["id"] != "trend_filter":
        raise RuntimeError("study C must extend the frozen trend_filter row")
    return {**base, "id": "dd_brake", "entry_drawdown_halt": 0.20}


def study_c_report(bundle=None) -> dict:
    """Score the drawdown brake on bars strictly before 2025-01-01."""

    assert_research_window(TRAIN_START, TRAIN_END)
    assert_research_window(VALIDATION_START, VALIDATION_END)
    for _name, start, end in WALK_FOLDS:
        assert_research_window(start, end)
    if STUDY_B_TEST_START <= pd.Timestamp("2026-12-31", tz="UTC"):
        raise ValueError("study C test overlaps a reserved or closed window")
    bundle = bundle or load_certified()
    pre = _before_holdout(bundle["frames"])
    reference = _run(pre, _candidate(FROZEN_NEXT_CANDIDATE), VALIDATION_START, VALIDATION_END, research=True)
    checkpoint_ok = _checkpoint_ok(reference)
    trials = [_study_row(_study_c_spec(), pre)]
    decision = _adopt_study_b(reference, trials, checkpoint_ok)
    return {
        "label": "RESEARCH_ONLY",
        "status": "IN_PROGRESS" if decision["adopted_id"] else "BLOCKED",
        "seed": SEED,
        "code_version": _code_version(),
        "holdout_used_for_tuning": False,
        "closed_windows_rescored": False,
        "floors": LOCKED_FLOORS,
        "costs": COST,
        "reference_id": FROZEN_NEXT_CANDIDATE,
        "reference_checkpoint_ok": checkpoint_ok,
        "reference_validation": {
            key: reference[key] for key in (
                "年化净收益率", "net_profit", "max_drawdown", "sharpe", "calmar",
                "profit_factor", "trade_count",
            )
        },
        "trials": trials,
        "decision": decision,
        "future_test": None if decision["adopted_id"] is None else {
            "start": STUDY_B_TEST_START.isoformat(),
            "end": STUDY_B_TEST_END.isoformat(),
            "scored": False,
        },
    }


def _emit(payload, out: str | None) -> None:
    text = json.dumps(_plain(payload), ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI stays free of argparse so the application package keeps its boundary."""

    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else ""
    out = None
    modes = {"prepare", "accept", "accept-next", "accept-later", "study-b", "study-c"}
    if mode not in modes or len(args) not in {1, 3} or (len(args) == 3 and args[1] != "--out"):
        print(
            "usage: python -m inv_trend.application.多品种验收 prepare|accept|accept-next|accept-later|study-b|study-c [--out PATH]",
            file=sys.stderr,
        )
        return 2
    if len(args) == 3:
        out = args[2]
    if mode == "prepare":
        payload = prepare_report()
    elif mode == "accept":
        payload = acceptance_report()
    elif mode == "accept-next":
        payload = next_window_report()
    elif mode == "accept-later":
        payload = later_window_report()
    elif mode == "study-b":
        payload = study_b_report()
    else:
        payload = study_c_report()
    _emit(payload, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
