from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.EMA统一基准 import EMAOrders, common_specs, prepare, run
from inv_trend.core.EMA趋势策略 import Config, Relation, confirmed_targets, signals
from inv_trend.core.执行约束 import ExecutionPolicy
from inv_trend.adapters.multi_asset.models.domain import PortfolioState, Position, PositionUnit


def bars(size=420, volume=1e9):
    t = pd.date_range("2020-01-01", periods=size, tz="UTC")
    close = 100 + .015 * np.arange(size) + 12 * np.sin(np.arange(size) / 30)
    return pd.DataFrame(dict(timestamp=t, available_at=t + pd.Timedelta(days=1),
        open=close, high=close + .3, low=close - .3, close=close, volume=volume))


def row(side=1, confirmed=True, atr=.1):
    result = dict(close=100., atr=atr, ordinal=200, macd=1., adx=30., volume_ratio=1.,
                  bar_end=pd.Timestamp("2021-01-01", tz="UTC"))
    for direction in (1, -1):
        for j in range(3):
            result[f"state_{direction}_{j}"] = "持续有效" if direction == side and confirmed else "失效"
    return result


def config(**kwargs):
    return Config(position_mode="confirmed", cap=1., rebalance_band=0., **kwargs)


@pytest.mark.parametrize("active,expected", [(0, [0, 0]), (1, [1, 0]), (2, [.5, .5])])
def test_gross_budget(active, expected):
    result = confirmed_targets({"BTC": row(confirmed=active > 0),
                                "ETH": row(-1, active > 1)}, config())
    assert [abs(v["target_weight"]) for v in result.values()] == expected
    if active == 2:
        assert result["ETH"]["target_weight"] == -.5


def test_risk_reduction_is_not_redistributed():
    result = confirmed_targets({"BTC": row(atr=10), "ETH": row()}, config())
    assert result["BTC"]["target_weight"] == pytest.approx(.02 / 2 / .3)
    assert result["ETH"]["target_weight"] == .5
    assert result["BTC"]["risk_reasons"] == ["stop_risk"]


@pytest.mark.parametrize("change", ["partial", "warmup", "conflict", "atr"])
def test_incomplete_direction_is_flat(change):
    r = row()
    if change == "partial": r["state_1_2"] = "观察中"
    if change == "warmup": r["ordinal"] = 164
    if change == "conflict":
        for j in range(3): r[f"state_-1_{j}"] = "持续有效"
    if change == "atr": r["atr"] = float("nan")
    assert confirmed_targets({"BTC": r}, config())["BTC"]["target_weight"] == 0


@pytest.mark.parametrize("values", [{"cap": float("nan")}, {"risk": float("inf")},
    {"confirmation": 1.5}, {"position_mode": "bad"}, {"cap": 2.1}])
def test_invalid_config(values):
    with pytest.raises(ValueError): Config(**values)


def test_equality_invalidates_and_initial_positive_does_not_cross():
    r = Relation(0)
    assert r.step(1) == "失效"
    assert r.step(0) == "失效"
    assert r.step(1) == "确认成功"
    assert r.step(0) == "失效"


@pytest.mark.parametrize("problem", ["empty", "short", "duplicate", "unsorted", "nan", "negative", "volume", "availability"])
def test_invalid_bars(problem):
    f = bars()
    if problem == "empty": f = f.iloc[:0]
    if problem == "short": f = f.iloc[:59]
    if problem == "duplicate": f.loc[1, "timestamp"] = f.loc[0, "timestamp"]
    if problem == "unsorted": f = f.iloc[::-1]
    if problem == "nan": f.loc[2, "close"] = np.nan
    if problem == "negative": f.loc[2, "open"] = -1
    if problem == "volume": f.loc[2, "volume"] = np.nan
    if problem == "availability": f.loc[2, "available_at"] = f.loc[2, "timestamp"]
    with pytest.raises(ValueError): signals(f)


def test_indicator_prefix_and_prepared_variants():
    frame = bars()
    full, prefix = prepare({"BTC": frame})["BTC"], prepare({"BTC": frame.iloc[:300]})["BTC"]
    pd.testing.assert_frame_equal(full.iloc[:300], prefix)
    for days in (0, 2, 4):
        reference = signals(frame, Config(confirmation=days))
        for side in (1, -1):
            for j in range(3):
                assert full[f"c{days}_{side}_{j}"].tolist() == reference[f"state_{side}_{j}"].tolist()


def test_partial_exit_and_empty_tradable_set():
    specs = common_specs(["BTC", "ETH"])
    strategy = EMAOrders(specs, config())
    state = PortfolioState(positions={"BTC": Position("BTC", 1, "ema",
        [PositionUnit(1000, 100, .1, reason="EMA目标再平衡")], 100, 90)})
    rows = {"BTC": row(), "ETH": row()}
    orders = strategy.generate_orders_for_date(pd.Timestamp("2021-01-01", tz="UTC"), rows, state, 100000.)
    assert orders[0].action == "exit" and orders[0].qty == 500
    assert orders[1].action == "open" and orders[1].qty == 500
    assert strategy.generate_orders_for_date(pd.Timestamp("2021-01-01", tz="UTC"), rows, state, 100000., set()) == []


@pytest.mark.parametrize("volume,delay", [(1e9, 1), (1., 1), (0., 1), (1e9, 2)])
def test_shared_account_reconciles_and_execution_is_causal(volume, delay):
    f = bars(volume=volume)
    data = prepare({"BTC": f, "ETH": f})
    summary, result = run(data, config(risk=.2), f.timestamp.iloc[165], f.available_at.iloc[-1],
                          signal_delay_bars=delay)
    assert summary["metrics"]["ending_equity"] > 0
    pnl = result.trade_details.pnl.sum() if not result.trade_details.empty else 0.
    pnl += result.open_trade_details.pnl.sum() if not result.open_trade_details.empty else 0.
    assert pnl == pytest.approx(summary["metrics"]["ending_equity"] - 100000, abs=1e-6)
    assert (result.cash_ledger.gross_leverage < 2).all()
    assert result.decisions.groupby("time").target_weight.apply(lambda x: x.abs().sum()).max() <= 1
    if volume == 0:
        assert result.cash_ledger.gross_leverage.max() == 0
    elif volume == 1e9:
        assert result.cash_ledger.gross_leverage.max() > .9
    if not result.orders.empty and "signal_bar_time" in result.orders:
        filled = result.orders[result.orders.status.fillna("filled").isin(["filled", "partial"])]
        known = filled.dropna(subset=["signal_bar_time"])
        assert (pd.to_datetime(known.time, utc=True) > pd.to_datetime(known.signal_bar_time, utc=True)).all()


def test_execution_policy_default_is_compatible():
    assert ExecutionPolicy().volume_asset_classes == ("equity",)
    assert not ExecutionPolicy().reduce_excess_leverage
    assert Config().position_mode == "staged"
    with pytest.raises(ValueError):
        ExecutionPolicy(volume_asset_classes="crypto")


def test_stop_requires_a_new_confirmation_before_reentry():
    specs = common_specs(["BTC"])
    strategy = EMAOrders(specs, config(risk=.2))
    when = pd.Timestamp("2021-01-01", tz="UTC")
    active = {"BTC": row()}
    assert strategy.generate_orders_for_date(when, active, PortfolioState(), 100000.)[0].action == "open"
    held = PortfolioState(positions={"BTC": Position("BTC", 1, "ema",
        [PositionUnit(1000, 100, .1, reason="EMA目标再平衡")], 100, 90)})
    strategy.generate_orders_for_date(when, active, held, 100000.)
    assert strategy.generate_orders_for_date(when, active, PortfolioState(), 100000.) == []
    inactive = {"BTC": row(confirmed=False)}
    assert strategy.generate_orders_for_date(when, inactive, PortfolioState(), 100000.) == []
    assert strategy.generate_orders_for_date(when, active, PortfolioState(), 100000.)[0].action == "open"
