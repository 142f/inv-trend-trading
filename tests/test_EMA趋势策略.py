from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from inv_trend.core.EMA趋势策略 import Config, Relation, backtest, signals


def bars():
    rng = np.random.default_rng(44)
    close = 100*np.exp(np.cumsum(.0002+.02*rng.normal(size=900)))
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame(dict(timestamp=pd.date_range("2020-01-01", periods=900, tz="UTC"),
        open=op, high=np.maximum(op,close)*1.005, low=np.minimum(op,close)*.995,
        close=close, volume=100.))


def test_lifecycle_and_failed_observation():
    r = Relation(3)
    assert [r.step(x) for x in [-1,1,2,3,4,5,0]] == [
        "失效","首次触发","观察中","观察中","确认成功","持续有效","失效"]
    r = Relation(3)
    assert [r.step(x) for x in [-1,1,2,-1,1]][-2:] == ["失效","首次触发"]
    r = Relation(3)
    assert all(r.step(x) == "失效" for x in [1,2,3,4,5])


def test_future_mutation_cannot_change_prefix():
    f = bars()
    changed = f.copy()
    changed.loc[600:, ["open","high","low","close"]] *= 8
    a, b = signals(f), signals(changed)
    pd.testing.assert_frame_equal(a.iloc[:600], b.iloc[:600])
    start, end = f.timestamp.iloc[200], f.timestamp.iloc[599]
    da, ta = backtest(a, Config(), start, end)
    db, tb = backtest(b, Config(), start, end)
    pd.testing.assert_frame_equal(da, db)
    pd.testing.assert_frame_equal(ta, tb)


def test_accounting_attribution_and_cap():
    f = signals(bars())
    d,t = backtest(f, Config(), f.timestamp.iloc[200], f.timestamp.iloc[-1])
    assert len(t) > 10
    assert t.pnl.sum() == pytest.approx(d.equity.iloc[-1]-1, abs=1e-10)
    assert d[["stage1","stage2","stage3"]].sum().sum() == pytest.approx(t.pnl.sum())
    assert d.exposure.max() <= Config().cap+1e-10
    for j in (1,2,3):
        assert t.loc[t.stage==j,"pnl"].sum() == pytest.approx(d[f"stage{j}"].sum())


def test_confirmation_executes_only_next_open_and_no_stage_skip():
    f = signals(bars())
    for side in (1,-1):
        for j in range(3):
            f[f"state_{side}_{j}"] = "失效"
    f.loc[210:215,"state_1_2"] = "持续有效"
    f.loc[220:230,"state_1_0"] = "持续有效"
    f.loc[220,"state_1_0"] = "确认成功"
    d,_ = backtest(f, Config(), f.timestamp.iloc[200], f.timestamp.iloc[240])
    assert (d.loc[:f.timestamp.iloc[220],"exposure"] == 0).all()
    assert d.loc[f.timestamp.iloc[221],"exposure"] > 0
    assert (d.stage2 == 0).all() and (d.stage3 == 0).all()


def test_gap_stop_uses_open_not_stale_stop():
    f = signals(bars())
    for side in (1,-1):
        for j in range(3):
            f[f"state_{side}_{j}"] = "失效"
    f.loc[200:205,"state_1_0"] = "持续有效"
    f.loc[199:203,["open","high","low","close"]] = [100.,101.,99.,100.]
    f.loc[199:203,"atr"] = 2.
    f.loc[202,["open","high","low","close"]] = 50.
    d,t = backtest(f, Config(), f.timestamp.iloc[200], f.timestamp.iloc[204])
    assert d.loc[f.timestamp.iloc[202],"stage1"] < 0
    assert d.loc[f.timestamp.iloc[202],"exposure"] == 0
    assert t.iloc[0].pnl < -.02  # stale 94 stop would materially understate gap loss
    assert t.pnl.sum() == pytest.approx(d.equity.iloc[-1]-1)


def test_short_signal_and_stage_execution_are_symmetric():
    f = signals(bars())
    for side in (1,-1):
        for j in range(3):
            f[f"state_{side}_{j}"] = "失效"
    f.loc[210:220,"state_-1_0"] = "持续有效"
    f.loc[212:220,"state_-1_1"] = "持续有效"
    f.loc[214:220,"state_-1_2"] = "持续有效"
    d,t = backtest(f, Config(), f.timestamp.iloc[200], f.timestamp.iloc[225])
    assert d.loc[f.timestamp.iloc[211],"direction"] == -1
    assert d.loc[f.timestamp.iloc[213],"direction"] == -2
    assert d.loc[f.timestamp.iloc[215],"direction"] == -3
    assert set(t.side) == {-1}


def test_data_validation_and_zero_confirmation():
    f = bars()
    f.loc[1,"timestamp"] = f.loc[0,"timestamp"]
    with pytest.raises(ValueError):
        signals(f)
    r = Relation(0)
    assert r.step(-1) == "失效" and r.step(1) == "确认成功"
    with pytest.raises(ValueError):
        replace(Config(), weights=(1,1,1))
