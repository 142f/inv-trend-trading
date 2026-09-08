"""Transparent training selection, out-of-sample metrics and eight-axis reviews."""
from __future__ import annotations

import math
from statistics import mean, median, pstdev, stdev
import numpy as np

from inv_trend.core.阶段协议_v1 import AccountSnapshot


def equity_metrics(equity, *, initial=100_000.0, fills=0, fees=0.0, slippage=0.0) -> dict:
    values = np.asarray(equity, dtype=float)
    if len(values) == 0 or not np.isfinite(values).all() or initial <= 0:
        raise ValueError("valid nonempty equity and positive initial capital required")
    full = np.r_[initial, values]
    returns = np.diff(full) / full[:-1]
    sd = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    total = float(values[-1] / initial - 1)
    dd = float(np.max(1 - full / np.maximum.accumulate(full)))
    return {"total_return": total,
            "annualized_return": float((1 + total) ** (252 / len(values)) - 1) if total > -1 else -1.,
            "max_drawdown": dd,
            "sharpe_ratio": float(returns.mean() / sd * math.sqrt(252)) if sd > 1e-14 else None,
            "fill_count": int(fills), "fees": float(fees), "slippage_cost": float(slippage),
            "terminal_equity": float(values[-1]), "bars": len(values),
            "risk_basis": "D1_CLOSE_252; rf=0; no terminal liquidation"}


def snapshot_metrics(rows: list[AccountSnapshot], *, initial=100_000.0,
                     initial_fees=0.0, initial_slippage=0.0) -> dict:
    return equity_metrics([r.equity for r in rows], initial=initial,
                          fills=sum(abs(f.quantity) > 0 for r in rows for f in r.fills),
                          fees=rows[-1].fees - initial_fees,
                          slippage=rows[-1].slippage_cost - initial_slippage)


def training_score(folds: list[dict]) -> tuple[float, bool]:
    """No outer/OOS object accepted. Hard threshold failure means CASH, not a forced winner."""
    if not folds or any(f.get("scope") != "TRAIN" for f in folds):
        raise ValueError("parameter selection requires TRAIN-tagged folds only")
    metrics = [f["metrics"] for f in folds]
    sr = [m["sharpe_ratio"] if m["sharpe_ratio"] is not None else -3.0 for m in metrics]
    worst = max(m["max_drawdown"] for m in metrics)
    positive = mean(m["total_return"] > 0 for m in metrics)
    count = sum(m["fill_count"] for m in metrics)
    score = median(sr) - pstdev(sr) - 2 * worst
    eligible = positive >= 2 / 3 and median(sr) > 0 and worst <= 0.35 and count >= 6
    return float(score), bool(eligible)


def block_bootstrap(returns, *, trials=36, seed=20260908, block=20, samples=400) -> dict:
    """Descriptive OOS mean-return CI; NOT DSR/PBO or a proof of live alpha.

    Bonferroni scales a one-sided centered moving-block bootstrap p-value. All trial
    counts remain disclosed. Samples are deliberately deterministic/reproducible.
    """
    r = np.asarray(returns, dtype=float)
    if len(r) < block * 2:
        return {"status": "INSUFFICIENT", "trials": trials}
    rng = np.random.default_rng(seed)
    length = len(r)
    means = []
    for _ in range(samples):
        starts = rng.integers(0, length, size=math.ceil(length / block))
        indices = (starts[:, None] + np.arange(block)[None, :]).ravel()[:length] % length
        means.append(float(r[indices].mean()))
    means = np.asarray(means)
    observed = float(r.mean())
    p = float((1 + np.sum(means - observed >= observed)) / (samples + 1))
    return {"status": "DESCRIPTIVE_ONLY", "mean_daily_return": observed,
            "ci95_mean_daily": np.quantile(means, [0.025, 0.975]).tolist(),
            "centered_one_sided_p": p, "bonferroni_p": min(1.0, p * trials),
            "trials": trials, "seed": seed, "block": block, "replications": samples,
            "limits": "selected universe/uncertified corporate actions; not DSR/PBO"}


def review_dimensions(metrics: dict, window_metrics: list[dict], stress_metrics: list[dict], *,
                      tests_passed: bool, seconds: float, baselines: dict | None = None,
                      code_checks: bool = True) -> dict:
    """0..5 internal engineering/research rubric. Every gate is explicit, not a probability."""
    clip = lambda x: round(max(0.0, min(5.0, float(x))), 3)
    positive = mean(m["total_return"] > 0 for m in window_metrics) if window_metrics else 0.
    stress_positive = mean(m["total_return"] > 0 for m in stress_metrics) if stress_metrics else 0.
    sr = metrics.get("sharpe_ratio") or 0.
    axes = {"正确性": 5.0 if tests_passed else 0.0,
            "收益": clip(2.5 + 2.5 * math.tanh(sr)),
            "回撤": clip(5 * (1 - metrics["max_drawdown"] / 0.4)),
            "稳定性": clip(5 * positive),
            "鲁棒性": clip(5 * stress_positive),
            "性能": clip(5 if seconds < 60 else 4 if seconds < 300 else 2),
            "代码质量": 4.0 if code_checks else 1.0,
            "可解释性": 5.0}
    return {"scores_0_to_5": axes, "unweighted_mean": round(mean(axes.values()), 3),
            "oos_positive_window_ratio": positive, "stress_positive_ratio": stress_positive,
            "metrics": metrics, "baselines": baselines or {}, "seconds": seconds,
            "production_enabled": False, "verdict": "RESEARCH_ONLY",
            "hard_blocks": ["source/session/corporate-action vintages not certified",
                            "historically selected equity universe; survivorship risk",
                            "no newly observed paper-forward evidence", "untouched holdout not evaluated"],
            "rubric": {"正确性": "targeted executable tests, not full-project certification",
                       "收益": "2.5+2.5*tanh(net OOS Sharpe)", "回撤": "5*(1-DD/0.40)",
                       "稳定性": "5*positive OOS symbol-window fraction",
                       "鲁棒性": "5*positive predeclared stress return fraction",
                       "性能": "<60s:5; <300s:4; otherwise2; workload-dependent",
                       "代码质量": "targeted syntax/architecture checks:4; else1; not external review",
                       "可解释性": "5: deterministic rules + complete decision/fill evidence"}}
