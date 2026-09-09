"""Causal feature cache, four strategy families, shared ledger and metrics."""
from dataclasses import asdict, dataclass
import math

import numpy as np
import pandas as pd

from inv_trend.core.四资产合同_v1 import ASSUMPTION, CORE, research_contract
from inv_trend.core.组合账户_v1 import CostModel, PortfolioAccount
from inv_trend.core.组合风控_v1 import PortfolioAllocationStage, PortfolioInput, RiskConfig
from inv_trend.core.阶段协议_v1 import digest


@dataclass(frozen=True)
class Experiment:
    family: str = "momentum"
    lookback: int = 80
    threshold: float = 0.
    exit_lookback: int = 20
    buffer: float = .1
    core_fraction: float = 0.
    regime: bool = False
    risk: RiskConfig = RiskConfig()
    costs: CostModel = CostModel()

    def __post_init__(self):
        if self.family not in ("momentum", "breakout", "reversion", "core_tactical", "passive"):
            raise ValueError("unknown family")
        if type(self.lookback) is not int or self.lookback < 3 or not 1 <= self.exit_lookback <= self.lookback:
            raise ValueError("invalid strategy lookback")
        if not 0 <= self.core_fraction <= 1 or not all(math.isfinite(x) and x >= 0 for x in (self.threshold, self.buffer)):
            raise ValueError("invalid strategy parameters")

    @property
    def id(self):
        return self.family + "-" + digest(asdict(self))[:16]


def experiment_grid():
    # Predeclared structured design: 4 families x 5 targets x 8 profiles = 160.
    # Coupled profiles are deliberately named, not represented as a full factorial.
    rows = []
    for family in ("momentum", "breakout", "reversion", "core_tactical"):
        for target in (.08, .10, .12, .15, .20):
            for j in range(8):
                risk = RiskConfig(method=("inverse_vol", "cluster")[j % 2],
                    portfolio_target_vol=target, lookback=(40, 60)[j % 2],
                    rebalance_frequency=(7, 14)[j % 2], crypto_budget=(.4, .6)[j % 2],
                    crypto_cluster_max_risk=(.55, .7)[j % 2],
                    metals_cluster_max_risk=(.7, .55)[j % 2],
                    single_asset_max_weight=(.35, .45)[j % 2],
                    single_asset_max_risk=(.5, .65)[j % 2],
                    min_vol_floor=(.03, .05)[j % 2],
                    max_leverage=(.85, .95)[j % 2], max_gross_exposure=(.85, .95)[j % 2])
                rows.append(Experiment(family, (60, 70, 80, 90)[j // 2],
                    threshold=(1.5 if family == "reversion" else .01 * (j % 2)),
                    exit_lookback=(10, 20)[j % 2], buffer=(.1, .2)[j % 2],
                    core_fraction=(0., .25, .4, .5)[j // 2] if family == "core_tactical" else 0.,
                    regime=bool(j % 2), risk=risk,
                    costs=CostModel(fee_bps=(5, 10)[j % 2], slippage_bps=(5, 10)[j % 2])))
    return rows


class FeatureCache:
    """Input identity + data version + window + feature config + code version.

    Cache construction is restricted to the caller's train/OOS slice. Every rolling
    statistic uses trailing rows; signals and risk keep separate knowledge clocks.
    """
    def __init__(self, bundle, code_version):
        self.bundle, self.code_version = bundle, code_version
        self.index, self.frames = bundle.aligned()
        self.cache = {}
        self.hits = self.misses = 0

    def get(self, experiment, start, end):
        c = experiment
        config = {k: v for k, v in asdict(c).items() if k not in ("costs",)}
        key = digest(dict(dataset_version=self.bundle.version, lineage=self.bundle.lineage,
                          window=(start, end), feature_config=config, code_version=self.code_version))
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        mask = (self.index >= pd.Timestamp(start)) & (self.index < pd.Timestamp(end))
        idx = self.index[mask]
        if len(idx) == 0:
            raise ValueError("empty feature window")
        # Only past warmup (400 calendar days) and the explicit authorized interval.
        warm = idx[0] - pd.Timedelta(days=400)
        fs = {s: f.loc[(f.index >= warm) & (f.index < pd.Timestamp(end))] for s, f in self.frames.items()}
        marks = pd.DataFrame({s: f.close for s, f in fs.items()}).ffill()
        returns = marks.pct_change(fill_method=None)
        vol = returns.rolling(c.risk.lookback, min_periods=c.risk.lookback).std().shift(1) * math.sqrt(365.25)
        corr = returns.rolling(c.risk.lookback, min_periods=c.risk.lookback).corr().shift(4)
        signals = pd.DataFrame(0., index=marks.index, columns=CORE)
        for s, f in fs.items():
            f = f.dropna(subset=["close"])
            close = f.close
            prior = close.shift(1)
            mom = close / close.shift(c.lookback) - 1
            z = (close - prior.rolling(c.lookback).mean()) / prior.rolling(c.lookback).std()
            atr = pd.concat([f.high-f.low, (f.high-prior).abs(), (f.low-prior).abs()], axis=1).max(axis=1).rolling(20).mean().shift(1)
            upper = f.high.shift(1).rolling(c.lookback).max() + c.buffer * atr
            lower = f.low.shift(1).rolling(c.exit_lookback).min()
            peak = prior.rolling(c.lookback).max()
            sigma = prior.pct_change(fill_method=None).rolling(c.risk.lookback).std() * math.sqrt(365.25)
            state, values = 0., []
            for k in range(len(f)):
                if c.family == "passive":
                    state = 1.
                elif c.family in ("momentum", "core_tactical"):
                    state = float(mom.iloc[k] > c.threshold)
                elif c.family == "breakout":
                    if close.iloc[k] > upper.iloc[k]:
                        state = 1.
                    elif close.iloc[k] < lower.iloc[k]:
                        state = 0.
                else:
                    if z.iloc[k] < -max(c.threshold, .5):
                        state = 1.
                    elif z.iloc[k] > -.2:
                        state = 0.
                factor = 1.
                if c.regime and (sigma.iloc[k] > .8 or close.iloc[k] / peak.iloc[k] < .7):
                    factor = .25
                values.append(state * factor)
            signals[s] = pd.Series(values, index=f.index).reindex(marks.index).ffill().fillna(0)
        arrays = dict(index=idx, key=key,
                      opens=np.column_stack([fs[s].open.reindex(idx).to_numpy(float) for s in CORE]),
                      closes=np.column_stack([fs[s].close.reindex(idx).to_numpy(float) for s in CORE]),
                      signals=signals.reindex(idx).to_numpy(float),
                      vols=vol.reindex(idx).to_numpy(float),
                      correlations=np.array([corr.loc[t].reindex(index=CORE, columns=CORE).to_numpy(float)
                                             for t in idx]))
        self.cache[key] = arrays
        return arrays


def run_backtest(bundle, experiment, *, start, end, code_version, cache=None,
                 account=None, buy_hold=False, single_asset=None, scenario="base", run_id="research"):
    if bundle.mode != ASSUMPTION:
        raise ValueError("formal input gate must pass before execution")
    cache = cache or FeatureCache(bundle, code_version)
    a = cache.get(experiment, start, end)
    account = account or PortfolioAccount({s: research_contract(s) for s in CORE},
                                          costs=experiment.costs, mode=ASSUMPTION)
    allocator = PortfolioAllocationStage()
    curve, risks = [], []
    initial = account.snapshot()
    entered = any(account.positions.values())
    for k, t in enumerate(a["index"]):
        at = t.isoformat()
        available = (t + pd.Timedelta(days=1)).isoformat()
        opens = {s: float(p) for s, p in zip(CORE, a["opens"][k]) if np.isfinite(p)}
        closes = {s: float(p) for s, p in zip(CORE, a["closes"][k]) if np.isfinite(p)}
        account.execute_open(at, opens)
        snapshot = account.mark_close(available, closes)
        curve.append(snapshot)
        entered = entered or any(account.positions.values())
        # Warmup missing any price: no fictitious holdings in unavailable assets.
        if not all(account.marks.values()):
            continue
        if buy_hold and entered:
            continue
        if not buy_hold and k % experiment.risk.rebalance_frequency:
            continue
        vols = a["vols"][k].copy()
        corr = a["correlations"][k].copy()
        if not np.isfinite(vols).all() or not np.isfinite(corr).all():
            continue
        if scenario == "volatility_shock":
            vols *= 2
        elif scenario in ("crypto_corr_one", "metals_corr_one", "all_corr_spike"):
            # Convex PSD stress; clustered perfect correlation, zero cross-cluster
            # assumption in the stressed matrix, blended near unity for numerical stability.
            stress = np.eye(4)
            if scenario == "all_corr_spike":
                stress[:] = 1
            elif scenario == "crypto_corr_one":
                stress[:2, :2] = 1
            else:
                stress[2:, 2:] = 1
            corr = .001 * corr + .999 * stress
        budget = experiment.risk.crypto_budget
        inp = PortfolioInput(run_id, available, tuple(a["signals"][k]), tuple(vols),
            tuple(map(tuple, corr)), tuple(account.positions.values()), account.cash, account.equity,
            (budget / 2, budget / 2, (1 - budget) / 2, (1 - budget) / 2), experiment.risk,
            at, (("dataset_version", bundle.version), ("feature_hash", a["key"])))
        out = allocator.run(inp, core_fraction=experiment.core_fraction if experiment.family == "core_tactical" else 0)
        weights = dict(zip(CORE, out.target_weights))
        if buy_hold:
            raw = np.ones(4) if experiment.risk.method == "equal" else 1 / np.maximum(vols, experiment.risk.min_vol_floor)
            weights = dict(zip(CORE, .95 * raw / raw.sum()))
        if single_asset:
            weights = {s: .95 if s == single_asset else 0 for s in CORE}
        account.submit(weights, available)
        risks.append(dict(timestamp=available, **asdict(out)))
    return account, curve, risks, performance(curve, initial)


def performance(curve, initial):
    if not curve:
        raise ValueError("empty equity curve")
    f = pd.DataFrame(curve).set_index("timestamp")
    f.index = pd.to_datetime(f.index, utc=True)
    e = f.equity.to_numpy(float)
    r = np.diff(np.r_[initial["equity"], e]) / np.r_[initial["equity"], e[:-1]]
    years = len(e) / 365.25
    cagr = (e[-1] / initial["equity"]) ** (1 / years) - 1
    vol = np.std(r, ddof=1) * math.sqrt(365.25) if len(r) > 1 else 0.
    dd = e / np.maximum.accumulate(np.r_[initial["equity"], e])[1:] - 1
    mdd = -float(dd.min())
    downside = math.sqrt(float(np.mean(np.minimum(r, 0) ** 2))) * math.sqrt(365.25)
    var = float(np.quantile(r, .05))
    cvar = float(r[r <= var].mean())
    series = pd.Series(r, index=f.index)
    months = (1 + series).resample("ME").prod() - 1
    quarters = (1 + series).resample("QE").prod() - 1
    rolling = (1 + series).rolling(365).apply(np.prod, raw=True) - 1
    rs = series.rolling(365).mean() / series.rolling(365).std() * math.sqrt(365.25)
    underwater = longest = 0
    for x in dd:
        underwater = underwater + 1 if x < -1e-10 else 0
        longest = max(longest, underwater)
    last = curve[-1]
    result = dict(CAGR=float(cagr), volatility=float(vol), MDD=mdd,
        Sharpe=float(np.mean(r) * 365.25 / vol) if vol else None,
        Sortino=float(np.mean(r) * 365.25 / downside) if downside else None,
        Calmar=float(cagr / mdd) if mdd else None, VaR=-var, CVaR=-cvar,
        Ulcer=float(np.sqrt(np.mean(dd ** 2))),
        turnover=float((last["turnover_notional"]-initial["turnover_notional"]) / f.equity.mean()),
        fees=last["fees"]-initial["fees"], slippage=last["slippage"]-initial["slippage"],
        funding=last["funding"]-initial["funding"],
        time_in_market=float((f.gross_exposure > .001).mean()),
        gross_exposure=float(f.gross_exposure.mean()), net_exposure=float(f.net_exposure.mean()),
        recovery_time_days=int(longest), recovery_censored=bool(dd[-1] < -1e-10),
        worst_month=float(months.min()), worst_quarter=float(quarters.min()),
        worst_rolling_1Y=float(rolling.min()) if rolling.notna().any() else None,
        rolling_Sharpe_min=float(rs.min()) if rs.notna().any() else None,
        rolling_Sharpe_last=float(rs.dropna().iloc[-1]) if rs.notna().any() else None,
        initial_equity=initial["equity"], final_equity=float(e[-1]), bars=len(e),
        total_return=float(e[-1] / initial["equity"] - 1),
        rolling_Sharpe=[(str(t), float(v)) for t, v in rs.dropna().items()])
    result["Cost"] = result["fees"] + result["slippage"] + result["funding"]
    return result
