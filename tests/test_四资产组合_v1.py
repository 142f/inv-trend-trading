from dataclasses import replace
import sqlite3

import numpy as np
import pandas as pd
import pytest

from inv_trend.core.四资产合同_v1 import ASSUMPTION, CORE, research_contract
from inv_trend.core.组合账户_v1 import CostModel, PortfolioAccount
from inv_trend.core.组合风控_v1 import PortfolioAllocationStage, PortfolioInput, RiskConfig
from inv_trend.data.四资产行情_v1 import MarketBundle, quality_checks
from inv_trend.application.组合回测_v1 import Experiment, FeatureCache, experiment_grid, run_backtest
from inv_trend.storage.结构化存储_v3 import UnifiedStore
from inv_trend.storage.组合研究_v1 import PortfolioRepository


def account(**kwargs):
    return PortfolioAccount({s: research_contract(s) for s in CORE}, mode=ASSUMPTION, **kwargs)


def at(day):
    return f"2020-01-{day:02d}T00:00:00+00:00"


def ready(a):
    a.execute_open(at(1), {s: 100. for s in CORE})
    a.mark_close(at(2), {s: 100. for s in CORE})


def bundle():
    rng = np.random.default_rng(241)
    dates = pd.date_range("2018-01-01", periods=1100, tz="UTC")
    fs = {}
    for i, s in enumerate(CORE):
        idx = dates if i < 2 else dates[dates.dayofweek < 5]
        close = 100 * np.exp(np.cumsum(rng.normal(.0003, .012, len(idx))))
        op = np.r_[close[0], close[:-1]] * 1.001
        fs[s] = pd.DataFrame(dict(timestamp=idx, available_at=idx + pd.Timedelta(days=1),
            open=op, close=close, high=np.maximum(op, close)*1.01, low=np.minimum(op, close)*.99,
            volume=1000., instrument_id=s, currency="USD", source="synthetic_test",
            dataset_version="test-v1", quality_status=ASSUMPTION))
    return MarketBundle(fs, {s: {"input_hash": s} for s in CORE}, {}, "test-v1", ASSUMPTION)


def test_formal_contract_fails_closed():
    with pytest.raises(ValueError, match="Execution contract invalid"):
        research_contract("XAU").validate(at(1))


@pytest.mark.parametrize("field,value", [("contract_multiplier", 0), ("price_tick", -1),
    ("quantity_step", float("nan")), ("initial_margin", 2), ("effective_from", at(3))])
def test_invalid_contract(field, value):
    with pytest.raises(ValueError):
        replace(research_contract("BTC"), **{field: value}).validate(at(1), ASSUMPTION)


def test_shared_cash_next_bar_gap_fees_and_accounting():
    a = account(initial_cash=10000, costs=CostModel(carry_bps_year=0))
    ready(a)
    a.submit({s: .25 for s in CORE}, at(2))
    assert not a.fills
    a.execute_open(at(2), {s: 200. for s in CORE})
    a.mark_close(at(3), {s: 180. for s in CORE})
    assert a.cash >= -1e-7
    assert any(f["status"] in ("PARTIAL", "REJECTED") for f in a.fills)
    assert all(f["price"] >= 200 for f in a.fills if f["quantity"] > 0)
    assert a.fees > 0 and a.slippage > 0
    assert sum(f["quantity"] * f["price"] + f["fee"] for f in a.fills) <= 10000 + 1e-5
    a.check()


def test_missing_metal_bar_never_fills_and_union_retains_weekends():
    a = account(costs=CostModel(carry_bps_year=0))
    ready(a)
    a.submit({s: .2 for s in CORE}, at(2))
    a.execute_open(at(2), {"BTC": 100., "ETH": 100.})
    assert a.positions["XAU"] == 0
    assert len([o for o in a.orders if o["status"] == "PENDING"]) == 2
    b = bundle()
    index, fs = b.aligned()
    assert len(index) == len(b.frames["BTC"])
    assert fs["XAU"].loc[index.dayofweek == 5, "close"].isna().all()


def test_partial_fill_uses_prior_capacity():
    a = account(costs=CostModel(capacity_equity=.01, carry_bps_year=0))
    ready(a)
    a.submit({s: .2 for s in CORE}, at(2))
    a.execute_open(at(2), {s: 100. for s in CORE})
    assert all(f["status"] == "PARTIAL" for f in a.fills)
    assert all(abs(f["quantity"] * f["price"]) <= 1000 + 1e-5 for f in a.fills)


def test_funding_carry_and_position_aggregation():
    a = account(costs=CostModel(carry_bps_year=365.25))
    ready(a)
    a.submit({s: .2 for s in CORE}, at(2))
    a.execute_open(at(2), {s: 100. for s in CORE})
    a.mark_close(at(3), {s: 100. for s in CORE})
    assert a.funding > 0 and a.margin_used > 0
    assert np.isclose(a.equity, a.cash + a.margin_used)
    assert a.available_cash == a.free_margin == a.cash
    a.submit({s: 0 for s in CORE}, at(3))
    a.execute_open(at(3), {s: 105. for s in CORE})
    a.mark_close(at(4), {s: 105. for s in CORE})
    assert a.realized_pnl > 0
    a.check()


def test_checkpoint_tamper_and_exact_resume():
    a = account()
    ready(a)
    a.submit({s: .2 for s in CORE}, at(2))
    checkpoint = a.checkpoint()
    b = account()
    b.restore(checkpoint)
    for obj in (a, b):
        obj.execute_open(at(2), {s: 101. for s in CORE})
        obj.mark_close(at(3), {s: 102. for s in CORE})
    assert a.snapshot() == b.snapshot()
    checkpoint["state"]["cash"] += 1
    with pytest.raises(ValueError, match="Checkpoint invalid"):
        b.restore(checkpoint)


def risk_input(**kwargs):
    d = dict(run_id="test", timestamp=at(3), asset_signals=(1.,)*4,
        asset_volatility=(.7, .8, .2, .3), correlation_matrix=tuple(map(tuple, np.eye(4))),
        current_positions=(0.,)*4, cash=100000., equity=100000., risk_budget=(.25,)*4,
        portfolio_constraints=RiskConfig(), known_before=at(2), lineage=(("version", "test"),))
    return PortfolioInput(**{**d, **kwargs})


@pytest.mark.parametrize("method", ["equal", "inverse_vol", "cluster"])
def test_risk_target_cluster_and_leverage_caps(method):
    c = replace(RiskConfig(), method=method, crypto_cluster_max_risk=.2, single_asset_max_weight=.3)
    out = PortfolioAllocationStage().run(risk_input(portfolio_constraints=c))
    assert sum(out.target_weights) <= c.max_leverage + 1e-8
    assert max(out.target_weights) <= c.single_asset_max_weight + 1e-8
    assert sum(abs(x) for x in out.risk_contributions[:2]) <= c.crypto_cluster_max_risk * c.portfolio_target_vol + 1e-8
    assert all(v for _, v in out.constraint_results)


def test_core_tactical_retains_core_at_flat_trend():
    inp = risk_input(asset_signals=(0.,)*4)
    zero = PortfolioAllocationStage().run(inp, core_fraction=0)
    core = PortfolioAllocationStage().run(inp, core_fraction=.4)
    assert sum(zero.target_weights) == 0
    assert sum(core.target_weights) > 0


@pytest.mark.parametrize("change", [dict(known_before=at(3)), dict(asset_volatility=(float("nan"),)*4),
    dict(correlation_matrix=((1, 2, 0, 0), (2, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)))])
def test_future_or_invalid_risk_rejected(change):
    with pytest.raises(ValueError):
        PortfolioAllocationStage().run(risk_input(**change))


def test_future_bars_volatility_and_correlation_invisible():
    b1, b2 = bundle(), bundle()
    cutoff = pd.Timestamp("2020-06-01", tz="UTC")
    for f in b2.frames.values():
        f.loc[f.timestamp >= cutoff, ["open", "high", "low", "close", "volume"]] *= 50
    kwargs = dict(start="2020-01-01T00:00:00+00:00", end="2020-09-01T00:00:00+00:00", code_version="test")
    a1, c1, _, _ = run_backtest(b1, Experiment(), **kwargs)
    a2, c2, _, _ = run_backtest(b2, Experiment(), **kwargs)
    assert [x for x in c1 if pd.Timestamp(x["timestamp"]) <= cutoff] == [x for x in c2 if pd.Timestamp(x["timestamp"]) <= cutoff]
    assert [f for f in a1.fills if pd.Timestamp(f["filled_at"]) < cutoff] == [f for f in a2.fills if pd.Timestamp(f["filled_at"]) < cutoff]


@pytest.mark.parametrize("defect,expected", [("duplicate", "duplicate timestamp"),
    ("order", "non-monotonic timestamp"), ("price", "negative price/nonfinite price"),
    ("ohlc", "OHLC inconsistency"), ("version", "dataset version mismatch"),
    ("timezone", "timezone mismatch"), ("missing", "missing interval")])
def test_data_quality(defect, expected):
    f = bundle().frames["BTC"].copy()
    idx = pd.DatetimeIndex(f.timestamp)
    if defect == "duplicate":
        f = pd.concat([f, f.iloc[[-1]]])
    elif defect == "order":
        f = f.iloc[::-1]
    elif defect == "price":
        f.loc[0, "close"] = -1
    elif defect == "ohlc":
        f.loc[0, "high"] = .01
    elif defect == "version":
        f.loc[0, "dataset_version"] = "future"
    elif defect == "timezone":
        f["timestamp"] = f.timestamp.dt.tz_convert("Asia/Shanghai")
    else:
        f = f.drop(index=2)
    assert expected in quality_checks(f, expected_version="test-v1", expected_index=idx)["errors"]


def test_source_revision_and_cache_identity():
    b = bundle()
    assert "source revision" in quality_checks(b.frames["BTC"], expected_version="test-v1", previous_hash="a", current_hash="b")["errors"]
    ca = FeatureCache(b, "code1")
    args = (Experiment(), "2020-01-01T00:00:00+00:00", "2020-02-01T00:00:00+00:00")
    old = ca.get(*args)["key"]
    assert ca.get(*args)["key"] == old and ca.hits == 1
    assert FeatureCache(b, "code2").get(*args)["key"] != old


def test_immutable_parameter_lock_and_train_separation(tmp_path):
    UnifiedStore(tmp_path, create=True)
    repo = PortfolioRepository(tmp_path)
    repo.begin("test", dict(mode=ASSUMPTION, core=list(CORE)), [research_contract(s) for s in CORE], {})
    win = dict(id="one", train_start=at(1), train_end=at(2), oos_start=at(2), oos_end=at(3))
    repo.lock("test", win, "momentum", Experiment(), 1, "data", "code")
    with repo.store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE parameter_locks SET selection_score=20")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM parameter_locks")
    with pytest.raises(ValueError, match="separation"):
        repo.lock("test", {**win, "train_end": at(3)}, "reversion", Experiment(), 1, "data", "code")
    assert len(experiment_grid()) == len({c.id for c in experiment_grid()}) == 160
