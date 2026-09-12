from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.EMA统一基准 import prepare, run, EMAOrders, common_specs, choose_training
from inv_trend.adapters.multi_asset.models.domain import PortfolioState, Position, PositionUnit
from inv_trend.adapters.multi_asset.models.domain import Order, TurtleRules
from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.core.执行约束 import ExecutionPolicy
from inv_trend.core.EMA趋势策略 import Config
from inv_trend.storage.策略版本登记 import immutable, register, digest, encoded


def data():
    rng = np.random.default_rng(121)
    dates = pd.date_range("2017-01-01", periods=1100, tz="UTC")
    frames = {}
    for s in ("A", "B", "C", "D"):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, len(dates))))
        op = np.r_[close[0], close[:-1]]
        frames[s] = pd.DataFrame(
            dict(
                timestamp=dates,
                available_at=dates + pd.Timedelta(days=1),
                open=op,
                high=np.maximum(op, close) * 1.005,
                low=np.minimum(op, close) * 0.995,
                close=close,
                volume=10000.0,
            )
        )
    return prepare(frames)


def test_shared_engine_future_mutation_and_reconciliation():
    d = data()
    changed = {s: f.copy() for s, f in d.items()}
    for f in changed.values():
        f.loc[
            f.index >= pd.Timestamp("2019-01-01", tz="UTC"), ["open", "high", "low", "close"]
        ] *= 5
    a, ar = run(d, Config(), "2018-01-01", "2019-01-01")
    b, br = run(changed, Config(), "2018-01-01", "2019-01-01")
    pd.testing.assert_series_equal(a["equity"], b["equity"])
    pd.testing.assert_frame_equal(ar.orders, br.orders)
    assert ar.trade_details.pnl.sum() + ar.open_trade_details.get(
        "pnl", pd.Series(dtype=float)
    ).sum() == pytest.approx(a["metrics"]["ending_equity"] - 100000)
    assert sum(a["metrics"]["stages"].values()) == pytest.approx(a["metrics"]["total"])


def test_passive_is_once_per_asset_same_costs_and_no_stops():
    summ, r = run(data(), Config(), "2018-01-01", "2019-01-01", kind="passive")
    fills = r.orders
    if "status" in fills:
        fills = fills.loc[fills.status.fillna("filled") == "filled"]
    assert (fills.action == "open").sum() == 4
    assert (fills.action == "exit").sum() == 0
    assert len(r.open_trade_details) == 4
    assert summ["metrics"]["fees"] > 0 and summ["metrics"]["slippage"] > 0
    assert summ["metrics"]["short_pnl"] == 0


def test_stage_failure_reduces_only_failed_units():
    specs = common_specs(["A", "B", "C", "D"])
    adapter = EMAOrders(specs, Config())
    row = dict(
        close=100.0, atr=2.0, ordinal=200, bar_end="2018-01-01", macd=1, adx=30, volume_ratio=1
    )
    for side in (1, -1):
        for j in range(3):
            row[f"state_{side}_{j}"] = "持续有效" if side == 1 and j < 2 else "失效"
    pos = Position(
        "A",
        1,
        "ema",
        [PositionUnit(1.0, 100.0, 2.0, reason=f"EMA阶段{j}") for j in (1, 2, 3)],
        100.0,
        94.0,
    )
    state = PortfolioState(positions={"A": pos})
    orders = adapter.generate_orders_for_date(
        pd.Timestamp("2018-01-01", tz="UTC"), {"A": row}, state, 100000, {"A"}
    )
    assert len(orders) == 1 and orders[0].metadata["unit_reasons"] == ["EMA阶段3"]
    assert orders[0].metadata["decision_id"] == adapter.last_decisions[0]["decision_id"]
    assert orders[0].metadata["candidate_id"] == "fixed"


def test_each_entry_carries_exact_decision_and_candidate_trace():
    summ, result = run(data(), Config(), "2018-01-01", "2019-01-01")
    orders = result.orders
    entries = orders.loc[orders.action.isin(["open", "add"])]
    assert len(entries) > 0
    assert entries.decision_id.notna().all()
    assert entries.trigger.eq("EMA关系确认且前缀阶段有效").all()
    decisions = result.decisions.set_index("decision_id")
    assert set(entries.decision_id).issubset(decisions.index)
    assert entries.candidate_id.eq("fixed").all()
    assert summ["metrics"]["trades"] > 0


def test_config_cap_is_effective_in_shared_execution_adapter():
    specs = common_specs(["A"])
    row = dict(
        close=100.0, atr=0.1, ordinal=200, bar_end="2018-01-01", macd=1, adx=30,
        volume_ratio=1,
    )
    for side in (1, -1):
        for j in range(3):
            row[f"state_{side}_{j}"] = "持续有效" if side == 1 else "失效"
    low = EMAOrders(specs, replace(Config(), cap=0.4)).generate_orders_for_date(
        pd.Timestamp("2018-01-01", tz="UTC"), {"A": row}, PortfolioState(), 100000, {"A"}
    )[0]
    high = EMAOrders(specs, replace(Config(), cap=0.9)).generate_orders_for_date(
        pd.Timestamp("2018-01-01", tz="UTC"), {"A": row}, PortfolioState(), 100000, {"A"}
    )[0]
    assert low.qty / high.qty == pytest.approx(0.4 / 0.9)


def test_selection_rejects_outer_data_and_ties_are_deterministic():
    m = dict(sharpe=1, calmar=1, drawdown=0.1)
    with pytest.raises(ValueError):
        choose_training([dict(scope="OUTER", metrics=m)])
    rows = [dict(scope="INNER_VALIDATION", candidate_id=x, metrics=m) for x in ("b", "a")]
    assert choose_training(rows) == "a"
    from dataclasses import asdict

    with pytest.raises(ValueError):
        EMAOrders(
            common_specs(["A"]),
            Config(),
            [
                dict(
                    effective_at="2020-01-01",
                    trained_through="2020-01-02",
                    parameters=asdict(Config()),
                )
            ],
        )


def test_registry_never_overwrites_and_hashes_results(tmp_path):
    p = tmp_path / "结果.json"
    immutable(p, {"total": 1})
    with pytest.raises(FileExistsError):
        immutable(p, {"total": 2})
    kwargs = dict(
        strategy_id="EMA",
        strategy_version="v1",
        previous=None,
        parameters={"confirmation": 3},
        context={"data_version": "abc"},
        summary={"total": 1},
        differences="initial",
        results=[p],
    )
    location = register(tmp_path, **kwargs)
    record = json.loads(__import__("pathlib").Path(location).read_text(encoding="utf-8"))
    h = record.pop("record_hash")
    assert h == digest(encoded(record))
    assert record["results"][str(p)] == digest(p.read_bytes())
    with pytest.raises(FileExistsError):
        register(tmp_path, **kwargs)


def test_cash_for_disabled_stages_and_empty_confirmation_entries():
    summ, r = run(data(), replace(Config(), disabled=(0, 1, 2)), "2018-01-01", "2019-01-01")
    assert summ["metrics"]["total"] == 0
    assert r.orders.empty


def test_partial_exit_keeps_remaining_short_carry_clock():
    d = data()
    specs = common_specs(d)
    runner = TurtleBacktester(
        d,
        specs,
        TurtleRules(),
        execution_policy=ExecutionPolicy(
            financing_bps_per_year=0.0, short_borrow_bps_per_year=300.0, enforce_volume=False
        ),
    )
    when = pd.Timestamp("2018-01-10", tz="UTC")
    units = [
        PositionUnit(
            10.0, 100.0, 2.0, entry_time=when - pd.Timedelta(days=20 - j), reason=f"EMA阶段{j}"
        )
        for j in (1, 2, 3)
    ]
    state = PortfolioState(positions={"A": Position("A", -1, "ema", units, 100.0, 110.0)})
    runner._known_prices = {"A": 100.0}
    for j, u in enumerate(units):
        runner._carry_clock[("A", u.entry_time, j)] = when
    order = Order(
        "A",
        "exit",
        -1,
        10.0,
        "选择退出",
        "ema",
        100.0,
        2.0,
        metadata={"unit_reasons": ["EMA阶段1"]},
    )
    cash, _ = runner._execute_orders(when, [order], 100000.0, state, [], [], [])
    assert [u.reason for u in state.positions["A"].units] == ["EMA阶段2", "EMA阶段3"]
    after = runner._accrue_calendar_cost(when + pd.Timedelta(days=1), cash, state)
    assert cash - after == pytest.approx(20 * 100 * 0.03 / 365.2425)


def test_nontrading_terminal_date_marks_open_units_without_fake_fill():
    d = data()
    for s in ("C", "D"):
        d[s] = d[s].loc[d[s].index < pd.Timestamp("2018-12-28", tz="UTC")]
    summ, r = run(d, Config(), "2018-01-01", "2019-01-01", kind="passive")
    assert len(r.open_trade_details) == 4
    assert not (r.orders.action == "exit").any()
    assert r.open_trade_details.pnl.sum() / 100000 == pytest.approx(summ["metrics"]["total"])
