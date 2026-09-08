"""统一净权益指标；初始本金、非交易日、未平仓与费用口径明确。"""
from __future__ import annotations
import numpy as np
import pandas as pd


def summarize(equity, trades=None, orders=None, *, initial_equity=100000., start=None,
              carry_cost=0.0, open_positions=0):
    eq = pd.Series(equity, dtype=float).sort_index()
    if eq.empty or not np.isfinite(eq).all():
        raise ValueError("权益为空或非有限")
    if eq.index.has_duplicates:
        eq = eq.groupby(level=0).last()
    stamp = pd.Timestamp(start) if start is not None else eq.index[0] - pd.Timedelta(seconds=1)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    if stamp >= eq.index[0]:
        stamp = eq.index[0] - pd.Timedelta(nanoseconds=1)
    full = pd.concat([pd.Series([initial_equity], index=[stamp]), eq])
    daily = full.resample("1D").last().ffill()
    # 把第一天的损益保留下来，不能让resample吞掉初始资金。
    daily = pd.concat([pd.Series([initial_equity], index=[daily.index[0]-pd.Timedelta(days=1)]),daily])
    returns = daily.pct_change().dropna()
    years = max((eq.index[-1] - stamp).total_seconds() / (365.2425*86400), 1 / 365.2425)
    total = float(eq.iloc[-1]/initial_equity - 1)
    annual = float((eq.iloc[-1]/initial_equity)**(1/years)-1) if eq.iloc[-1]>0 else None
    dd = float(-(full/full.cummax()-1).min())
    vol = float(returns.std(ddof=1)) if len(returns)>1 else 0.
    sharpe = float(returns.mean()/vol*np.sqrt(365.2425)) if vol>0 else None
    tr = pd.DataFrame() if trades is None else trades
    od = pd.DataFrame() if orders is None else orders
    pnl = tr["pnl"].astype(float) if "pnl" in tr else pd.Series(dtype=float)
    win, loss = pnl[pnl>0], pnl[pnl<0]
    filled = od.loc[od.status.fillna("filled").eq("filled")] if "status" in od else od
    fee = float(filled["fee_cost"].sum()) if "fee_cost" in filled else float(filled["cost"].sum()) if "cost" in filled else 0.
    slip = float(filled["slippage_cost"].sum()) if "slippage_cost" in filled else 0.
    return dict(total_return=total, annualized_return=annual,max_drawdown=dd,
        sharpe_ratio=sharpe,calmar_ratio=annual/dd if dd>0 and annual is not None else None,
        win_rate=float((pnl>0).mean()) if len(pnl) else None,
        payoff_ratio=float(win.mean()/-loss.mean()) if len(win) and len(loss) else None,
        profit_factor=float(win.sum()/-loss.sum()) if len(loss) else None,
        trade_count=len(pnl),long_pnl=float(tr.loc[tr.side.eq(1),"pnl"].sum()) if len(tr) else 0.,
        short_pnl=float(tr.loc[tr.side.eq(-1),"pnl"].sum()) if len(tr) else 0.,
        long_count=int(tr.side.eq(1).sum()) if len(tr) else 0,
        short_count=int(tr.side.eq(-1).sum()) if len(tr) else 0,
        fee_cost=fee,slippage_cost=slip,carry_cost=float(carry_cost),
        closed_pnl=float(pnl.sum()),open_position_count=open_positions,
        terminal_equity=float(eq.iloc[-1]),calendar_days=len(returns),risk_free_rate=0.,
        risk_measure='calendar_daily_365.2425',bankrupt=bool((eq<=0).any()))


def daily_returns(equity, initial_equity=100000.):
    eq = equity.sort_index().groupby(level=0).last().resample("1D").last().ffill()
    seed = pd.Series([initial_equity],index=[eq.index[0]-pd.Timedelta(days=1)])
    return pd.concat([seed,eq]).pct_change().dropna()
