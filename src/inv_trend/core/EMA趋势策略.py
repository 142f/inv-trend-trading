"""Causal EMA lifecycle and staged, unlevered research execution."""
from dataclasses import dataclass

import numpy as np
import pandas as pd

PAIRS = ((10, 20), (10, 55), (20, 55))


@dataclass(frozen=True)
class Config:
    confirmation: int = 3
    weights: tuple = (0.25, 0.35, 0.40)
    cap: float = 0.90
    risk: float = 0.02
    stop_atr: float = 3.0
    quality: str = "none"
    quality_scale: float = 0.5
    short_scale: float = 1.0
    disabled: tuple = ()
    independent: bool = False
    fee: float = 0.0005
    slippage: float = 0.0005
    short_carry: float = 0.03

    def __post_init__(self):
        if (self.confirmation < 0 or len(self.weights) != 3 or min(self.weights) < 0
                or sum(self.weights) > 1.0000001 or not 0 < self.cap <= 1
                or self.risk <= 0 or self.stop_atr <= 0
                or not 0 <= self.short_scale <= 1 or not 0 <= self.quality_scale <= 1
                or min(self.fee, self.slippage, self.short_carry) < 0
                or self.quality not in ("none", "macd", "adx", "volume")):
            raise ValueError("invalid strategy configuration")


class Relation:
    def __init__(self, days=3):
        self.days = days
        self.previous = None
        self.age = 0
        self.active = False

    def step(self, value):
        """No initial synthetic cross; equality invalidates an active relation."""
        state = "失效"
        if self.previous is not None:
            if value > 0 and self.previous <= 0:
                self.age, self.active = 0, True
                state = "确认成功" if self.days == 0 else "首次触发"
            elif value > 0 and self.active:
                self.age += 1
                state = ("观察中" if self.age < self.days else
                         "确认成功" if self.age == self.days else "持续有效")
            else:
                self.active = False
                self.age = 0
        self.previous = value
        return state


def signals(frame, cfg=Config()):
    f = frame.copy().reset_index(drop=True)
    if len(f) < 60:
        raise ValueError("at least 60 daily bars required")
    t = pd.to_datetime(f.timestamp, utc=True)
    if t.duplicated().any() or not t.is_monotonic_increasing:
        raise ValueError("duplicate or unsorted bars")
    p = f[["open", "high", "low", "close"]].to_numpy(float)
    if (not np.isfinite(p).all() or (p <= 0).any()
            or (f.high < f[["open", "close"]].max(axis=1)).any()
            or (f.low > f[["open", "close"]].min(axis=1)).any()):
        raise ValueError("invalid OHLC")
    f["timestamp"] = t
    if "available_at" in f and (pd.to_datetime(f.available_at, utc=True) > t.shift(-1)).any():
        raise ValueError("bar unavailable at next open")
    for n in (10, 12, 20, 26, 55):
        f[f"ema{n}"] = f.close.ewm(span=n, adjust=False).mean()
    tr = pd.concat([f.high-f.low, (f.high-f.close.shift()).abs(),
                    (f.low-f.close.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    macd = f.ema12-f.ema26
    f["macd"] = macd-macd.ewm(span=9, adjust=False).mean()
    up, down = f.high.diff(), -f.low.diff()
    plus = up.where((up > down) & (up > 0), 0).ewm(alpha=1/14, adjust=False).mean()
    minus = down.where((down > up) & (down > 0), 0).ewm(alpha=1/14, adjust=False).mean()
    f["adx"] = (100*(plus-minus).abs()/(plus+minus).replace(0, np.nan)).ewm(
        alpha=1/14, adjust=False).mean().fillna(0)
    f["volume_ratio"] = f.volume/f.volume.rolling(20, min_periods=20).mean().replace(0, np.nan)
    for side in (1, -1):
        for j, (a, b) in enumerate(PAIRS):
            rel = Relation(cfg.confirmation)
            values = (side*(f[f"ema{a}"]-f[f"ema{b}"])).to_numpy()
            # Warm-up relationships are tracked but never traded before bar 165.
            f[f"state_{side}_{j}"] = [rel.step(v) for v in values]
    return f


def backtest(features, cfg, start, end):
    """Close decision -> next actual bar open. Separate stage lots reconcile exactly.

    Each fold starts flat, indicators retain causal warm-up. Terminal liquidation is
    a predeclared market-on-close research assumption with costs charged.
    """
    f = features
    times = pd.DatetimeIndex(f.timestamp)
    indices = np.flatnonzero((times >= pd.Timestamp(start)) & (times < pd.Timestamp(end)))
    if not len(indices) or indices[0] < 165:
        raise ValueError("empty window or inadequate warm-up")
    equity = 1.0
    units = np.zeros(3)
    target = np.zeros(3)
    stops = np.full(3, np.nan)
    episode = np.zeros(3)
    records, trades = [], []
    price = f.close.to_numpy(float)
    opens, highs, lows = (f[x].to_numpy(float) for x in ("open", "high", "low"))
    atr = f.atr.to_numpy(float)
    states = {(s,j): f[f"state_{s}_{j}"].to_numpy() for s in (1,-1) for j in range(3)}
    quality = {k: f[k].to_numpy(float) for k in ("macd", "adx", "volume_ratio")}
    cost = cfg.fee + cfg.slippage
    for i in indices:
        before = equity
        pnl = units*(opens[i]-price[i-1])
        episode += pnl
        equity += pnl.sum()
        available = max(equity-abs(units).sum()*opens[i]*cost, 0)
        desired = target*available/(opens[i]*(1+cfg.cap*cost))
        turnover = float(np.abs(desired-units).sum()*opens[i]/max(equity, 1e-12))
        # A previous stop crossed by the opening gap must exit before re-entry.
        gap = ((units > 0) & (opens[i] <= stops)) | ((units < 0) & (opens[i] >= stops))
        desired[gap] = 0
        for j in range(3):
            delta_cost = abs(desired[j]-units[j])*opens[i]*cost
            if units[j] and np.sign(desired[j]) != np.sign(units[j]):
                exit_cost = abs(units[j])*opens[i]*cost
                trades.append(dict(stage=j+1, side=int(np.sign(units[j])), pnl=episode[j]-exit_cost,
                                   exit=str(times[i])))
                episode[j] = -(delta_cost-exit_cost)
                stops[j] = np.nan
            else:
                episode[j] -= delta_cost
            pnl[j] -= delta_cost
            equity -= delta_cost
            if desired[j] and (not units[j] or np.sign(desired[j]) != np.sign(units[j])):
                stops[j] = opens[i]-np.sign(desired[j])*cfg.stop_atr*atr[i-1]
        units = desired
        side_at_open = np.sign(units).copy()
        exposure = float(np.abs(units).sum()*opens[i]/max(equity, 1e-12))
        carry_days = (times[i]-times[i-1]).total_seconds()/86400
        carry = np.where(units < 0, abs(units)*opens[i]*cfg.short_carry*carry_days/365, 0)
        endprice = np.full(3, price[i])
        hit = ((units > 0) & (lows[i] <= stops)) | ((units < 0) & (highs[i] >= stops))
        endprice[hit] = stops[hit]
        intraday = units*(endprice-opens[i])-carry
        pnl += intraday
        episode += intraday
        equity += intraday.sum()
        for j in range(3):
            if hit[j] or (i == indices[-1] and units[j]):
                fee = abs(units[j])*endprice[j]*cost
                equity -= fee
                pnl[j] -= fee
                episode[j] -= fee
                trades.append(dict(stage=j+1, side=int(np.sign(units[j])), pnl=episode[j], exit=str(times[i])))
                episode[j], units[j], stops[j] = 0, 0, np.nan
        if equity <= 0:
            raise ValueError("insolvent sleeve")
        records.append(dict(timestamp=times[i], equity=equity, ret=equity/before-1,
                            exposure=exposure, turnover=turnover,
                            close_notional=float(abs(units).sum()*price[i]),
                            **{f"stage{j+1}": pnl[j] for j in range(3)},
                            direction=int(side_at_open.sum())))
        target[:] = 0
        for side in (1, -1):
            valid = [states[side,j][i] in ("确认成功", "持续有效") for j in range(3)]
            scale = 1.0
            weak = ((cfg.quality == "macd" and quality["macd"][i]*side <= 0)
                    or (cfg.quality == "adx" and quality["adx"][i] < 20)
                    or (cfg.quality == "volume" and not quality["volume_ratio"][i] >= 1))
            if weak:
                scale = cfg.quality_scale
            budget = min(cfg.cap, cfg.risk/(cfg.stop_atr*atr[i]/price[i]))*scale
            budget *= cfg.short_scale if side < 0 else 1
            for j in range(3):
                if (valid[j] and (cfg.independent or all(valid[:j+1])) and j not in cfg.disabled):
                    target[j] += side*cfg.weights[j]*budget
        for j in range(3):
            if units[j] > 0:
                stops[j] = max(stops[j], price[i]-cfg.stop_atr*atr[i])
            elif units[j] < 0:
                stops[j] = min(stops[j], price[i]+cfg.stop_atr*atr[i])
    daily = pd.DataFrame(records).set_index("timestamp")
    return daily, pd.DataFrame(trades, columns=["stage", "side", "pnl", "exit"])


def metrics(returns, trades=None):
    r = pd.Series(returns, dtype=float)
    nav = np.r_[1., (1+r).cumprod().to_numpy()]
    dd = float(np.max(1-nav/np.maximum.accumulate(nav)))
    annual = float(nav[-1]**(365/max(len(r),1))-1)
    sharpe = float(r.mean()/r.std(ddof=1)*np.sqrt(365)) if r.std(ddof=1) > 0 else 0.
    result = dict(total=float(nav[-1]-1), annual=annual, drawdown=dd, sharpe=sharpe,
                  calmar=annual/dd if dd else 0.)
    if trades is not None:
        p = trades.pnl
        win, loss = p[p > 0], p[p < 0]
        result.update(trades=len(p), win_rate=float((p > 0).mean()) if len(p) else 0.,
                      payoff=float(win.mean()/-loss.mean()) if len(win) and len(loss) else None,
                      profit_factor=float(win.sum()/-loss.sum()) if len(loss) else None,
                      long_pnl=float(p[trades.side == 1].sum()),
                      short_pnl=float(p[trades.side == -1].sum()),
                      stages={str(j): float(p[trades.stage == j].sum()) for j in (1,2,3)})
    return result
