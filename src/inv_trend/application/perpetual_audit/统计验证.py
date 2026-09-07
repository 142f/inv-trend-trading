"""Selection-aware statistics. No metric substitutes for independent evidence."""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd


def metrics(returns):
    r = np.asarray(returns, dtype=float)
    if r.ndim != 1 or len(r) < 2 or not np.isfinite(r).all():
        raise ValueError('finite daily return series required')
    if (r <= -1).any():
        return dict(total_return=-1., sharpe=None, calmar=None, max_drawdown=1., annualized_return=-1.)
    curve = np.r_[1., np.cumprod(1 + r)]
    mdd = float(-np.min(curve / np.maximum.accumulate(curve) - 1))
    cagr = float(curve[-1] ** (365.25 / len(r)) - 1)
    std = float(r.std(ddof=1))
    return dict(total_return=float(curve[-1] - 1), annualized_return=cagr,
                sharpe=float(r.mean() / std * math.sqrt(365.25)) if std else None,
                max_drawdown=mdd, calmar=cagr / mdd if mdd else None,
                calmar_window_days=len(r), periods_per_year=365.25)


def deflated_sharpe(returns, *, trials, sharpe_variance):
    """Bailey/Lopez de Prado DSR, daily (unannualized) trial SR variance.

    The probability is a PSR against the estimated selection benchmark, not
    the probability of future profitability. IID approximation is disclosed.
    """
    r = np.asarray(returns, dtype=float)
    if len(r) < 3 or not np.isfinite(r).all() or trials < 1 or not math.isfinite(sharpe_variance) or sharpe_variance < 0:
        raise ValueError('invalid DSR inputs')
    std = float(r.std(ddof=1))
    if not std:
        return dict(deflated_sharpe=None, reason='ZERO_VARIANCE')
    sr = float(r.mean() / std)
    centered = r - r.mean()
    variance = float(np.mean(centered ** 2))
    skew = float(np.mean(centered ** 3) / variance ** 1.5)
    kurtosis = float(np.mean(centered ** 4) / variance ** 2)
    normal, gamma = NormalDist(), .5772156649015329
    expected_max = (math.sqrt(sharpe_variance) * ((1 - gamma) * normal.inv_cdf(1 - 1 / trials) +
                    gamma * normal.inv_cdf(1 - 1 / (trials * math.e)))) if trials > 1 else 0.
    denominator = 1 - skew * sr + (kurtosis - 1) * sr ** 2 / 4
    value = normal.cdf((sr - expected_max) * math.sqrt(len(r) - 1) / math.sqrt(denominator)) if denominator > 0 else None
    return dict(ordinary_sharpe=sr * math.sqrt(365.25), deflated_sharpe=value,
                sample_length=len(r), skewness=skew, kurtosis=kurtosis,
                trials=trials, sharpe_variance=sharpe_variance,
                selection_benchmark_daily=expected_max, approximation='IID_PSR; serial dependence not corrected')


def _summary(folds):
    values = [metrics(r) for r in folds]
    for name in ('sharpe', 'calmar'):
        if any(row[name] is None for row in values):
            return None
    return np.array([np.median([x['sharpe'] for x in values]),
                     np.median([x['calmar'] for x in values]),
                     max(x['max_drawdown'] for x in values)])


def paired_bootstrap(baseline_folds, candidate_folds, *, block=30, repetitions=2000, seed=20260907):
    if len(baseline_folds) != len(candidate_folds) or not baseline_folds:
        raise ValueError('paired folds required')
    pairs = []
    for a, b in zip(baseline_folds, candidate_folds):
        if isinstance(a, pd.Series) and isinstance(b, pd.Series) and not a.index.equals(b.index):
            raise ValueError('paired returns have different dates')
        a, b = np.asarray(a, float), np.asarray(b, float)
        if len(a) != len(b) or len(a) < block or not np.isfinite(a).all() or not np.isfinite(b).all():
            raise ValueError('invalid paired returns')
        pairs.append((a, b))
    base, cand = _summary([x[0] for x in pairs]), _summary([x[1] for x in pairs])
    if base is None or cand is None:
        return dict(status='UNAVAILABLE', reason='undefined point metric')
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(repetitions):
        aa, bb = [], []
        for a, b in pairs:
            starts = rng.integers(0, len(a), size=math.ceil(len(a) / block))
            indices = ((starts[:, None] + np.arange(block)) % len(a)).ravel()[:len(a)]
            aa.append(a[indices])
            bb.append(b[indices])
        x, y = _summary(aa), _summary(bb)
        if x is not None and y is not None:
            samples.append(y - x)
    if len(samples) < .95 * repetitions:
        return dict(status='UNAVAILABLE', reason='too many undefined bootstrap metrics', valid_repetitions=len(samples))
    ci = np.quantile(samples, [.025, .975], axis=0)
    return dict(status='READY', block=block, repetitions=repetitions, seed=seed,
                valid_repetitions=len(samples),
                **{key: dict(delta=float(cand[i] - base[i]), lower=float(ci[0, i]), upper=float(ci[1, i]))
                   for i, key in enumerate(('sharpe', 'calmar', 'max_drawdown'))})


def concentration(pnls, *, initial_equity, ending_equity, seed=20260907):
    p = np.asarray(pnls, float)
    if not np.isfinite(p).all() or initial_equity <= 0:
        raise ValueError('invalid trade ledger')
    winners = np.sort(p[p > 0])[::-1]
    gross_profit = float(winners.sum())
    net = ending_equity - initial_equity
    result = dict(trade_count=len(p), median_trade=float(np.median(p)) if len(p) else None,
                  expectancy=float(p.mean()) if len(p) else None,
                  contribution_denominator='gross_positive_closed_trade_pnl',
                  net_profit=net, status='ANALYZABLE' if len(p) >= 30 else 'EVIDENCE_INSUFFICIENT')
    for k in (1, 3, 5):
        result[f'top_{k}_contribution'] = float(winners[:k].sum() / gross_profit) if gross_profit else None
    for k in (1, 3):
        result[f'return_without_top_{k}'] = float((net - winners[:k].sum()) / initial_equity)
    if len(p) >= 2:
        rng = np.random.default_rng(seed)
        # Adjacent completed-trade blocks retain some serial dependence.
        size = min(5, len(p))
        starts = rng.integers(0, len(p), size=(2000, math.ceil(len(p) / size)))
        indices = ((starts[:, :, None] + np.arange(size)) % len(p)).reshape(2000, -1)[:, :len(p)]
        result['expectancy_ci'] = np.quantile(p[indices].mean(axis=1), [.025, .975]).tolist()
        result['expectancy_ci_assumption'] = 'circular blocks of up to 5 adjacent completed trades'
    else:
        result['expectancy_ci'] = None
    return result


def candidate_decision(candidate_folds, *, trades, liquidations, comparison, complete_execution):
    if not complete_execution:
        return 'E0', 'EXECUTION_EVIDENCE_INCOMPLETE'
    if len(candidate_folds) < 3 or trades < 30:
        return 'E0', 'EVIDENCE_INSUFFICIENT'
    stats = [metrics(r) for r in candidate_folds]
    if liquidations or max(s['max_drawdown'] for s in stats) > .2 or sum(s['total_return'] > 0 for s in stats) / len(stats) < 2 / 3:
        return 'E0', 'ABSOLUTE_GATE_FAILED'
    if comparison.get('status') != 'READY':
        return 'E0', 'COMPARISON_UNAVAILABLE'
    if (comparison['sharpe']['delta'] < .1 or comparison['sharpe']['lower'] <= 0 or
            comparison['calmar']['delta'] < 0 or comparison['max_drawdown']['delta'] > 0):
        return 'E0', 'IMPROVEMENT_NOT_PROVEN'
    return 'E1', 'DEVELOPMENT_SUPPORTED_ONLY'
