"""Descriptive trade diagnostics; future excursions are never trading inputs."""
from __future__ import annotations

import numpy as np
import pandas as pd


def trade_statistics(trades):
    frame = pd.DataFrame(trades)
    p = frame['pnl'].astype(float) if len(frame) else pd.Series(dtype=float)
    wins, losses = p[p > 0], p[p < 0]
    return dict(trade_count=len(p), win_rate=float((p > 0).mean()) if len(p) else None,
                payoff_ratio=float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
                profit_factor=float(wins.sum() / -losses.sum()) if len(losses) else None,
                expectancy=float(p.mean()) if len(p) else None,
                median_trade=float(p.median()) if len(p) else None)


def attribute_trades(trades, daily):
    frame = pd.DataFrame(trades).copy()
    if frame.empty:
        return dict(trades=[], breakdowns={}, limitations=['NO_COMPLETED_TRADES'])
    for i, trade in frame.iterrows():
        bars = daily.get(trade['symbol'])
        if bars is None or bars.empty:
            continue
        entry, exit_time = pd.Timestamp(trade['entry_time']), pd.Timestamp(trade['exit_time'])
        history = bars.loc[bars.index < entry.normalize()]
        held = bars.loc[(bars.index >= entry.normalize()) & (bars.index <= exit_time.normalize())]
        price = float(trade.get('first_entry_price', trade.get('avg_entry', 0)))
        side = int(trade.get('side', 1))
        frame.loc[i, 'entry_year'] = entry.year
        if len(history) >= 200:
            ma = history.close.tail(200).mean()
            frame.loc[i, 'entry_regime'] = 'UP' if history.close.iloc[-1] > ma else 'DOWN'
            vol = history.close.pct_change().rolling(60).std() * np.sqrt(365.25)
            prior = vol.iloc[:-1].tail(365)
            frame.loc[i, 'entry_vol_regime'] = ('HIGH' if len(prior.dropna()) and vol.iloc[-1] > prior.quantile(.75) else 'NORMAL_OR_LOW')
        else:
            frame.loc[i, 'entry_regime'] = 'WARMUP_INSUFFICIENT'
        if price > 0 and len(held):
            favorable = held.high.max() / price - 1 if side == 1 else 1 - held.low.min() / price
            adverse = held.low.min() / price - 1 if side == 1 else 1 - held.high.max() / price
            frame.loc[i, 'mfe_price_fraction'] = float(max(favorable, 0))
            frame.loc[i, 'mae_price_fraction'] = float(min(adverse, 0))
            if 'exit_price' in trade:
                realized = side * (float(trade['exit_price']) / price - 1)
                frame.loc[i, 'price_giveback_fraction'] = float(max(0, favorable - realized))
            # Descriptive only. Exit-day daily extremes can occur after exit.
            frame.loc[i, 'excursion_precision'] = 'DAILY_RANGE_INCLUDES_UNKNOWN_INTRADAY_ORDER'
    breakdowns = {}
    for col in ('symbol', 'side', 'system', 'exit_reason', 'add_count', 'entry_year', 'entry_regime', 'entry_vol_regime'):
        if col in frame:
            breakdowns[col] = {str(k): dict(trade_statistics(g.to_dict('records')),
                                           net_pnl=float(g.pnl.sum()),
                                           total_cost=float(g.total_cost.sum()) if 'total_cost' in g else None)
                               for k, g in frame.groupby(col, dropna=False)}
    costs = float(frame.total_cost.sum()) if 'total_cost' in frame else None
    return dict(trades=frame.astype(object).where(pd.notnull(frame), None).to_dict('records'),
                breakdowns=breakdowns, fixed_ledger_cost=costs,
                fixed_ledger_gross_pnl=float(frame.gross_pnl.sum()) if 'gross_pnl' in frame else None,
                fixed_ledger_net_pnl=float(frame.pnl.sum()),
                limitations=['MFE/MAE仅作事后描述，日线极值不是精确盘中成交路径',
                             '按加仓次数分组不是加仓的因果贡献；必须以无加仓重跑对照',
                             '净成本归因与完整重跑分开，固定账本不能反映资金反馈'])
