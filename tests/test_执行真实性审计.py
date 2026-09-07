from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pandas as pd
import pytest

from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, Order, PortfolioState, TurtleRules
from inv_trend.adapters.multi_asset.models.order_intent import PendingOrderIntent, ReservationBook
from inv_trend.adapters.multi_asset.risk.expiry import ExpiryPolicy
from inv_trend.application.backtest.models import ValidationPolicy
from inv_trend.application.backtest.service import _percentiles, _stable_region, _rank_results
from tests.unit.test_strategy_backtest_stage import _plan
from inv_trend.application.backtest.service import _failed_combination


def engine(*, cash_model="derivative", equity=10000., carry=False):
    frame = pd.DataFrame(dict(open=[100., 100., 80., 90.], high=[101., 110., 91., 91.],
                              low=[99., 99., 79., 89.], close=[100., 105., 90., 90.],
                              volume=1000., funding=.001),
                         index=pd.date_range("2024-01-01", periods=4, tz="UTC"))
    spec = AssetSpec("TEST", "crypto", "crypto", qty_step=.1, cost_bps=10, slippage_bps=5,
                     funding_rate_column="funding" if carry else None)
    return TurtleBacktester({"TEST": frame}, {"TEST": spec}, TurtleRules(),
                            initial_equity=equity, cash_model=cash_model)


def order(action="open", qty=1., side=1):
    return Order("TEST", action, side, qty, "audit", "fast", 100., 5.)


def test_exit_cannot_be_cancelled_by_entry_gap_filter():
    intent = PendingOrderIntent(order("exit"), "2024-01-01", "2024-01-01", 20, 100., 1., 0., 0.)
    assert ExpiryPolicy().check_before_fill(intent, None, 50.).status == "pending"
    intent.order = order("open")
    assert ExpiryPolicy().check_before_fill(intent, None, 50.).status == "cancelled"


def test_cash_buy_is_limited_by_cash_including_both_costs():
    runner, state = engine(cash_model="cash", equity=100.), PortfolioState()
    orders, trades, details = [], [], []
    cash, pending = runner._execute_orders(runner.market_data.calendar[0], [order(qty=10)],
                                          100., state, orders, trades, details)
    assert not pending and cash >= 0
    assert state.positions["TEST"].total_qty == pytest.approx(.9)
    assert cash == pytest.approx(100 - 90 - .135)


@pytest.mark.parametrize("cash_model", ["cash", "derivative"])
def test_forced_stop_and_regular_exit_share_identical_accounting(cash_model):
    runner = engine(cash_model=cash_model)
    records = []
    for forced in (False, True):
        state, orders, trades, details = PortfolioState(), [], [], []
        cash, _ = runner._execute_orders(runner.market_data.calendar[0], [order()], 10000., state, orders, trades, details)
        exit_order = replace(order("exit"), forced_fill_price=80. if forced else None)
        cash, _ = runner._execute_orders(runner.market_data.calendar[2], [exit_order], cash, state, orders, trades, details)
        records.append((cash, trades, details))
    assert records[0] == records[1]


def test_carry_cost_reconciles_cash_trade_and_unit_pnl():
    runner, state = engine(carry=True), PortfolioState()
    orders, trades, details = [], [], []
    day = runner.market_data.calendar[0]
    cash, _ = runner._execute_orders(day, [order()], 10000., state, orders, trades, details)
    cash = runner._apply_carry_costs(day, cash, state)
    assert state.positions["TEST"].carry_cost == pytest.approx(.1)
    cash, _ = runner._execute_orders(day, [order("add")], cash, state, orders, trades, details)
    assert [u.carry_cost for u in state.positions["TEST"].units] == [.1, 0.]
    cash, _ = runner._execute_orders(day, [order("exit")], cash, state, orders, trades, details)
    assert cash - 10000 == pytest.approx(trades[0]["pnl"])
    assert sum(row["pnl"] for row in details) == pytest.approx(trades[0]["pnl"])
    assert trades[0]["total_cost"] == pytest.approx(.7)


def test_open_stop_uses_open_only_not_current_bar_low():
    runner, state = engine(), PortfolioState()
    orders, trades, details = [], [], []
    cash, _ = runner._execute_orders(runner.market_data.calendar[0], [order()], 10000., state, orders, trades, details)
    day = runner.market_data.calendar[1]
    state.positions["TEST"].stop_price = 99.5
    runner._process_intraday_stops(day, cash, state, orders, trades, details, open_only=True)
    assert "TEST" in state.positions
    runner._process_intraday_stops(day, cash, state, orders, trades, details)
    assert not state.positions and trades[0]["exit_price"] == 99.5


def test_requested_end_liquidates_only_when_explicitly_enabled():
    runner, state = engine(), PortfolioState()
    runner._apply_entry_fill(runner.market_data.calendar[0], order(), 100., 0., state)
    runner.evaluation_end = runner.market_data.calendar[1]
    assert runner._end_of_data_exit_orders(runner.market_data.calendar[1], state)
    assert not runner._end_of_data_exit_orders(runner.market_data.calendar[0], state)


def result(identifier, score=.5, holdout=.1):
    item = _failed_combination({"rules.stop_n": score}, ValueError("fixture"))
    return replace(item, combination_id=identifier, status="COMPLETED", qualified=True, score=90.,
                   metrics={"bankrupt": True},
                   validation_metrics=dict(sharpe_ratio=1., mar_ratio=1., annualized_return=.1,
                                           max_drawdown=-.1, positive_fold_ratio=1., closed_trade_count=20),
                   holdout_metrics={"total_return": holdout})


def test_percentile_ties_and_missing_metrics_are_id_independent():
    rows = [result("A"), result("B"), replace(result("C"), validation_metrics={})]
    assert _percentiles(rows, "sharpe_ratio") == {"A": 50., "B": 50., "C": 0.}


def test_holdout_cannot_choose_parameter_neighbors_and_missing_is_not_pass():
    plan = _plan(parameter_space={"rules.stop_n": (1.5, 2.)})
    candidate, neighbor = result("A", 1.5), result("B", 2., -.99)
    assert _stable_region([candidate, neighbor], plan, candidate) == ("A", "B")
    rows, best, _ = _rank_results([replace(candidate, holdout_metrics={})], plan, "READY", True)
    assert rows[0].qualified  # Future full-history bankruptcy cannot affect OOS selection.
    assert best is None


def test_overlapping_validation_and_nonfinite_equity_fail_explicitly():
    with pytest.raises(ValueError, match="overlap"):
        ValidationPolicy(100, 60, 30, 60, 3)
    with pytest.raises(ValueError, match="positive"):
        engine(equity=np.inf)


def test_later_pending_fill_uses_equity_after_earlier_fees():
    base=engine()
    runner=TurtleBacktester({key:base.data["TEST"] for key in ("A","B")},
                            {key:replace(base.specs["TEST"],symbol=key) for key in ("A","B")})
    book=ReservationBook()
    for key in ("A","B"):
        book.reserve(PendingOrderIntent(replace(order(),symbol=key),"2024-01-01","2024-01-01",20,100.,1.,0.,0.))
    observed=[]

    class Guard:
        def validate(self,intent,price,state,reservations,equity,prices):
            observed.append(equity)
            return SimpleNamespace(allowed=True,approved_qty=1.)

    runner._execute_pending_intents(runner.market_data.calendar[1],10000.,PortfolioState(),book,
                                    Guard(),ExpiryPolicy(),[],[],[])
    assert observed==pytest.approx([10000.,9999.85])


def test_gap_stop_precedes_pending_add_without_reopening_position():
    runner=engine()

    class Strategy:
        def generate_orders_for_date(self,date,rows,state,equity,tradable_symbols):
            if date==runner.market_data.calendar[0]:
                return [order()]
            if date==runner.market_data.calendar[1]:
                return [replace(order("add"),n_at_signal=20.,signal_price=105.)]
            return []

    runner.strategy=Strategy()
    result=runner.run()
    assert len(result.trades)==1 and result.trades.iloc[0].unit_count==1
    assert result.trades.iloc[0].exit_price==80.
    add=result.orders.loc[result.orders.action=="add"].iloc[0]
    assert add.status=="cancelled"
    assert add.resolution=="position no longer exists"
