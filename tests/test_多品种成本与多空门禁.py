"""Shipped TurtleBacktester gates: costs, directions, lookahead, caps."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.models.domain import (
    LONG,
    SHORT,
    AssetSpec,
    Order,
    PortfolioState,
    TurtleRules,
)
from inv_trend.core.执行约束 import ExecutionPolicy


def _rules(**overrides: object) -> TurtleRules:
    params: dict[str, object] = {
        "fast_entry": 2,
        "slow_entry": 3,
        "fast_exit": 2,
        "slow_exit": 3,
        "n_period": 2,
        "stop_n": 2.0,
        "skip_fast_after_win": False,
        "allow_short": True,
        "max_total_1n_risk_pct": 1.0,
        "max_direction_1n_risk_pct": 1.0,
        "default_cluster_1n_risk_pct": 1.0,
        "max_total_leverage": 5.0,
        "max_direction_leverage": 5.0,
        "default_cluster_leverage": 5.0,
    }
    params.update(overrides)
    return TurtleRules(**params)


def _spec(symbol: str, **overrides: object) -> AssetSpec:
    params: dict[str, object] = {
        "symbol": symbol,
        "asset_class": "synthetic",
        "cluster": "test",
        "qty_step": 1.0,
        "min_qty": 1.0,
        "max_units": 1,
        "unit_1n_risk_pct": 0.01,
        "max_symbol_1n_risk_pct": 1.0,
        "max_symbol_leverage": 5.0,
        "cost_bps": 0.0,
        "slippage_bps": 0.0,
        "spread_bps": 0.0,
    }
    params.update(overrides)
    return AssetSpec(**params)


def _bars(targets: dict[str, list[int]], *, price: float = 100.0, funding: float | None = None) -> dict[str, pd.DataFrame]:
    count = len(next(iter(targets.values())))
    index = pd.date_range("2024-01-01", periods=count, freq="D", tz="UTC")
    frames = {}
    for symbol, sides in targets.items():
        if len(sides) != count:
            raise ValueError("target length mismatch")
        frame = pd.DataFrame(
            {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "target_side": sides,
            },
            index=index,
        )
        if funding is not None:
            frame["funding_rate"] = funding
        frames[symbol] = frame
    return frames


class _TargetBook:
    """Trade the current bar's target only. Flat means do not open."""

    def __init__(self, weight: float = 0.0, fixed_qty: float = 1.0) -> None:
        self.weight = weight
        self.fixed_qty = fixed_qty

    def generate_orders(self, rows, state: PortfolioState, equity: float, tradable_symbols=None):
        orders = []
        active = rows if tradable_symbols is None else {k: v for k, v in rows.items() if k in tradable_symbols}
        for symbol in sorted(active):
            row = active[symbol]
            target = int(row["target_side"])
            price = float(row["close"])
            position = state.positions.get(symbol)
            held = 0 if position is None else int(position.side)
            if target == held:
                continue
            if position is not None:
                orders.append(Order(
                    symbol, "exit", position.side, position.total_qty, "target_flat_or_flip",
                    "fast", price, 1.0,
                ))
                continue
            if target == 0 or equity <= 0 or price <= 0:
                continue
            raw = (equity * self.weight / price) if self.weight else self.fixed_qty
            qty = math.floor(raw)
            if qty < 1:
                continue
            orders.append(Order(symbol, "open", target, float(qty), "target_open", "fast", price, 1.0))
        return orders


def _run(frames, specs, rules=None, **kwargs):
    return TurtleBacktester(
        frames,
        specs,
        rules or _rules(),
        initial_equity=10_000.0,
        liquidate_at_end=kwargs.pop("liquidate_at_end", True),
        strategy=kwargs.pop("strategy", _TargetBook()),
        **kwargs,
    ).run()


def test_closed_holdout_cannot_be_scored_again() -> None:
    from inv_trend.application.多品种验收 import (
        CLOSED_WINDOW_END,
        FROZEN_NEXT_CANDIDATE,
        NEXT_WINDOW_END,
        NEXT_WINDOW_START,
        acceptance_report,
        next_window_report,
    )

    assert next_window_report is not acceptance_report

    assert NEXT_WINDOW_START == pd.Timestamp("2026-08-27", tz="UTC")
    assert NEXT_WINDOW_END == pd.Timestamp("2026-10-09", tz="UTC")
    assert NEXT_WINDOW_START > CLOSED_WINDOW_END
    assert FROZEN_NEXT_CANDIDATE == "trend_filter"
    with pytest.raises(ValueError, match="already evaluated"):
        acceptance_report()


def _later_bundle(end: str) -> dict:
    index = pd.date_range(end=end, periods=160, freq="D", tz="UTC")
    frames = {}
    for symbol, base in (("BTC", 100.0), ("ETH", 50.0)):
        close = base + pd.Series(range(len(index)), dtype=float)
        frames[symbol] = pd.DataFrame(
            {
                "open": close.to_numpy(),
                "high": (close + 1.0).to_numpy(),
                "low": (close - 1.0).to_numpy(),
                "close": close.to_numpy(),
                "volume": 1_000.0,
            },
            index=index,
        )
    return {"frames": frames, "versions": {"BTC": {"dataset_version": "synthetic"}, "ETH": {"dataset_version": "synthetic"}}}


def test_later_window_is_one_unused_day_and_stays_blocked() -> None:
    from inv_trend.application.多品种验收 import (
        LATER_WINDOW_END,
        LATER_WINDOW_START,
        NEXT_WINDOW_END,
        later_window_report,
        next_window_report,
    )

    assert LATER_WINDOW_START == pd.Timestamp("2026-10-10", tz="UTC")
    assert LATER_WINDOW_END == LATER_WINDOW_START
    assert LATER_WINDOW_START > NEXT_WINDOW_END
    with pytest.raises(ValueError, match="does not contain"):
        later_window_report(_later_bundle("2026-10-09"))
    with pytest.raises(ValueError, match="extends past"):
        later_window_report(_later_bundle("2026-10-11"))
    with pytest.raises(ValueError, match="extends past"):
        next_window_report(_later_bundle("2026-10-10"))
    report = later_window_report(_later_bundle("2026-10-10"))
    assert report["label"] == "RESEARCH_ONLY"
    assert report["status"] == "BLOCKED"
    assert report["robustness"]["passed"] is False
    assert report["selection_repeated"] is False
    assert report["closed_window_rescored"] is False
    assert report["chosen_id"] == "trend_filter"
    assert report["oos"]["window"]["start"].startswith("2026-10-10")
    assert report["oos"]["equity_curve"]["start"].startswith("2026-10-10")
    assert report["oos"]["equity_curve"]["end"].startswith("2026-10-10")


def test_study_b_keeps_the_frozen_candidate_and_the_holdout() -> None:
    import pandas as pd

    from inv_trend.application.多品种验收 import (
        CANDIDATES,
        FROZEN_NEXT_CANDIDATE,
        STUDY_B_TEST_START,
        _candidate,
        _rules,
        _study_b_specs,
        assert_research_window,
    )

    frozen = _rules(_candidate(FROZEN_NEXT_CANDIDATE))
    assert frozen.allow_short is True
    assert frozen.entry_atr_percentile_max == 1.0
    assert frozen.risk_atr_fraction_target == 0.0
    assert frozen.entry_drawdown_halt == 0.0
    from inv_trend.application.多品种验收 import _study_c_spec
    assert _study_c_spec()["entry_drawdown_halt"] == 0.20
    assert _rules(_study_c_spec()).entry_drawdown_halt == 0.20
    assert CANDIDATES[2]["id"] == "trend_filter"
    specs = _study_b_specs()
    assert [item["id"] for item in specs] == ["vol_cap", "long_only"]
    assert _rules(specs[0]).entry_atr_percentile_max == 0.8
    assert _rules(specs[0]).allow_short is True
    assert _rules(specs[1]).allow_short is False
    assert STUDY_B_TEST_START == pd.Timestamp("2027-01-01", tz="UTC")
    with pytest.raises(ValueError, match="holdout"):
        assert_research_window("2024-01-01", "2025-01-01")


def test_holdout_window_is_locked_before_acceptance() -> None:
    from inv_trend.application.多品种验收 import (
        HOLDOUT_START,
        LOCKED_FLOORS,
        assert_research_window,
    )

    assert HOLDOUT_START == pd.Timestamp("2025-01-01", tz="UTC")
    assert LOCKED_FLOORS["max_drawdown"] <= 0.30
    assert LOCKED_FLOORS["sharpe"] >= 0.8
    assert LOCKED_FLOORS["calmar"] >= 0.5
    assert LOCKED_FLOORS["profit_factor"] >= 1.2
    assert LOCKED_FLOORS["annualized_net_return"] > 0.10 or LOCKED_FLOORS["annualized_net_return"] == 0.10
    with pytest.raises(ValueError, match="holdout"):
        assert_research_window("2025-01-01", "2025-06-30")


def test_spread_must_be_finite_and_non_negative() -> None:
    with pytest.raises(ValueError):
        _spec("AAA", spread_bps=-1.0)
    with pytest.raises(ValueError):
        TurtleRules(max_total_leverage=float("inf"))


def test_portfolio_holds_opposite_directions_and_can_stay_flat() -> None:
    frames = _bars({"AAA": [0, 0, 1, 1, 1, 1, 1], "BBB": [0, 0, -1, -1, -1, -1, -1]})
    result = _run(frames, {s: _spec(s) for s in frames})
    trades = result.trades
    assert set(trades["symbol"]) == {"AAA", "BBB"}
    assert set(trades["side"]) == {LONG, SHORT}
    long_row = trades.loc[trades["symbol"] == "AAA"].iloc[0]
    short_row = trades.loc[trades["symbol"] == "BBB"].iloc[0]
    assert long_row["entry_time"] < short_row["exit_time"]
    assert short_row["entry_time"] < long_row["exit_time"]

    flat = _run(_bars({"AAA": [0, 0, 0, 0, 0]}), {"AAA": _spec("AAA")})
    assert flat.orders.empty or set(flat.orders["action"]) == set()
    assert flat.equity_curve.iloc[-1] == pytest.approx(10_000.0)


def test_position_reverses_after_the_prior_side_is_closed() -> None:
    frames = _bars({"AAA": [0, 1, 1, 1, -1, -1, -1, 0]})
    result = _run(frames, {"AAA": _spec("AAA")})
    sides = result.trades["side"].tolist()
    assert sides == [LONG, SHORT]
    assert result.trades.iloc[0]["exit_time"] <= result.trades.iloc[1]["entry_time"]


def test_each_cost_cuts_net_pnl_only_when_positive() -> None:
    sides = [0, 1, 1, 1, 1, 1, 0]
    base_frames = _bars({"AAA": sides})
    base = _run(base_frames, {"AAA": _spec("AAA")})
    base_equity = float(base.equity_curve.iloc[-1])
    assert base_equity == pytest.approx(10_000.0)

    cases = {
        "commission": {"cost_bps": 50.0},
        "spread": {"spread_bps": 50.0},
        "slippage": {"slippage_bps": 50.0},
    }
    for name, overrides in cases.items():
        charged = _run(base_frames, {"AAA": _spec("AAA", **overrides)})
        untouched = _run(base_frames, {"AAA": _spec("AAA", **{key: 0.0 for key in overrides})})
        assert charged.equity_curve.iloc[-1] < base_equity
        assert untouched.equity_curve.iloc[-1] == pytest.approx(base_equity)
        assert name

    funded_frames = _bars({"AAA": sides}, funding=0.01)
    zero_frames = _bars({"AAA": sides}, funding=0.0)
    funded = _run(funded_frames, {"AAA": _spec("AAA", funding_rate_column="funding_rate")})
    zero_rate = _run(zero_frames, {"AAA": _spec("AAA", funding_rate_column="funding_rate")})
    absent = _run(base_frames, {"AAA": _spec("AAA")})
    assert funded.carry_cost > 0
    assert funded.equity_curve.iloc[-1] < base_equity
    assert zero_rate.equity_curve.iloc[-1] == pytest.approx(absent.equity_curve.iloc[-1])
    assert zero_rate.carry_cost == pytest.approx(0.0)


def test_future_bars_do_not_change_the_decision_already_known_at_t() -> None:
    short_targets = [0, 1, 1, 1, 1, 1]
    long_targets = short_targets + [-1, -1, 0, 0]
    specs = {"AAA": _spec("AAA", cost_bps=10.0, spread_bps=5.0, slippage_bps=5.0)}
    early = _run(_bars({"AAA": short_targets}), specs, liquidate_at_end=False)
    later = _run(_bars({"AAA": long_targets}), specs, liquidate_at_end=False)
    first_extra = pd.Timestamp("2024-01-07", tz="UTC")
    shared = early.equity_curve.index.intersection(later.equity_curve.index)
    assert len(shared) >= 4
    pd.testing.assert_series_equal(early.equity_curve.loc[shared], later.equity_curve.loc[shared])
    columns = ["time", "symbol", "action", "side", "qty", "fill_price", "cost"]
    early_orders = early.orders.loc[early.orders["time"] < first_extra, columns].reset_index(drop=True)
    later_orders = later.orders.loc[later.orders["time"] < first_extra, columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(early_orders, later_orders)
    assert (later.orders["time"] >= first_extra).any()


def test_two_identical_runs_match() -> None:
    frames = _bars({"AAA": [0, 1, 1, 1, -1, -1, 0], "BBB": [0, -1, -1, -1, 1, 1, 0]})
    specs = {s: _spec(s, cost_bps=8.0, spread_bps=4.0, slippage_bps=3.0) for s in frames}
    policy = ExecutionPolicy(event_clock=False, price_slippage=True, max_gross_leverage=1.0,
                             financing_bps_per_year=100.0, short_borrow_bps_per_year=50.0,
                             enforce_capital=True, enforce_volume=False, charge_calendar_carry=True)
    first = _run(frames, specs, execution_policy=policy)
    second = _run(frames, specs, execution_policy=policy)
    pd.testing.assert_series_equal(first.equity_curve, second.equity_curve)
    assert first.orders["cost"].sum() == pytest.approx(second.orders["cost"].sum())
    assert float(first.orders["spread_cost"].sum()) > 0
    assert first.carry_cost == pytest.approx(second.carry_cost)
    assert first.carry_cost > 0


def test_leverage_and_concentration_caps_are_finite_and_clip() -> None:
    frames = _bars({"AAA": [0, 1, 1, 1, 1], "BBB": [0, 1, 1, 1, 1]}, price=100.0)
    tight = _rules(max_total_leverage=0.15, max_direction_leverage=0.15, default_cluster_leverage=0.15)
    loose = _rules(max_total_leverage=1.0, max_direction_leverage=1.0, default_cluster_leverage=1.0)
    specs = {s: _spec(s, max_symbol_leverage=0.10) for s in frames}
    capped = _run(frames, specs, tight, strategy=_TargetBook(weight=0.4))
    wider = _run(frames, specs, loose, strategy=_TargetBook(weight=0.4))
    solo = _run(
        {"AAA": frames["AAA"]},
        {"AAA": specs["AAA"]},
        _rules(max_total_leverage=1.0, max_direction_leverage=1.0, default_cluster_leverage=1.0),
        strategy=_TargetBook(weight=0.4),
    )
    def _filled_open_qty(orders: pd.DataFrame) -> pd.DataFrame:
        view = orders.loc[orders["action"].eq("open")].copy()
        if "status" in view.columns:
            view = view.loc[view["status"].isna() | ~view["status"].isin(["rejected", "cancelled"])]
        return view

    filled = _filled_open_qty(capped.orders)
    equity = 10_000.0
    assert not filled.empty
    for _, row in filled.iterrows():
        assert row["qty"] * row["fill_price"] <= 0.10 * equity + 1e-6
    assert float(capped.cash_ledger["gross_leverage"].max()) <= 0.15 + 1e-9
    assert (capped.orders["status"] == "rejected").any()
    solo_qty = float(_filled_open_qty(solo.orders)["qty"].sum())
    assert solo_qty == pytest.approx(10.0)
    wider_qty = float(_filled_open_qty(wider.orders)["qty"].sum())
    assert wider_qty > float(filled["qty"].sum())
