"""Reproduce old/new bug cases and count actual feature computations."""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from 五轮重构基准 import ROOT, baseline_module, bars
from inv_trend.core import features, math_utils
from inv_trend.core.performance import drawdown_duration


def feature_counts(module, math_module):
    counts = {"validation": 0, "sma": 0, "ema": 0}

    def counted(operation, name):
        def run(*args, **kwargs):
            counts[name] += 1
            return operation(*args, **kwargs)
        return run

    with ExitStack() as stack:
        for owner, attribute, name in (
            (module, "_validate_ohlcv", "validation"),
            (module, "simple_moving_average", "sma"),
            (module, "exponential_moving_average", "ema"),
            (math_module, "exponential_moving_average", "ema"),
        ):
            stack.enter_context(patch.object(owner, attribute, counted(getattr(owner, attribute), name)))
        request = module.FeatureRequest(
            sma_lags=tuple((p, lag) for p in (10, 20, 60) for lag in (0, 1, 2)),
            ema_periods=(12, 26, 60), macd_periods=(12, 26, 9),
            volume_sma_lags=((20, 0), (20, 1)),
        )
        module.FeatureCache().prepare(bars(200), request)
    return counts


def cache_probe(module, field):
    frame = bars(100)
    if field == "auxiliary":
        frame["provider"] = "old"
    elif field == "metadata":
        frame.attrs["dataset_version"] = "old"
    cache, spec = module.FeatureCache(), module.FeatureRequest()
    cache.prepare(frame, spec)
    updated = frame.copy()
    if field == "auxiliary":
        updated["provider"] = "new"
        return cache.prepare(updated, spec).frame.provider.iloc[0] == "new"
    if field == "metadata":
        updated.attrs["dataset_version"] = "new"
        return cache.prepare(updated, spec).frame.attrs["dataset_version"] == "new"
    updated.index = updated.index.tz_convert("Asia/Shanghai")
    return cache.prepare(updated, spec).frame.index.identical(updated.index)


def empty_dmi(module):
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    try:
        return module.directional_movement_index(empty, empty, empty).empty
    except IndexError:
        return False


def main():
    old_math = baseline_module("src/inv_trend/core/math_utils.py", "inv_trend.core._baseline_math")
    old_features = baseline_module(
        "src/inv_trend/core/features.py", "inv_trend.core._baseline_features",
        (("from .math_utils", "from ._baseline_math"),),
    )
    baseline_module("src/inv_trend/core/performance.py", "inv_trend.core._baseline_performance")
    old_metrics = baseline_module(
        "src/inv_trend/application/backtest/metrics.py", "_baseline_metrics",
        (("inv_trend.core.performance", "inv_trend.core._baseline_performance"),),
    )
    recovered = pd.Series([0., -.1, 0., -.5, -.3, -.2, 0.])
    unrecovered = recovered.iloc[:-1]
    cases = {
        "empty_dmi": (empty_dmi(old_math), empty_dmi(math_utils)),
        **{f"cache_{field}": (cache_probe(old_features, field), cache_probe(features, field))
           for field in ("auxiliary", "metadata", "timezone")},
        "deeper_drawdown_recovered": (
            old_metrics._drawdown_duration(recovered) == (3, 3),
            drawdown_duration(recovered) == (3, 3),
        ),
        "deeper_drawdown_unrecovered": (
            old_metrics._drawdown_duration(unrecovered) == (3, None),
            drawdown_duration(unrecovered) == (3, None),
        ),
    }
    assert all(not before and after for before, after in cases.values())
    report = {
        "bugs": {name: dict(before_passed=before, after_passed=after)
                 for name, (before, after) in cases.items()},
        "feature_operation_counts": {
            "before": feature_counts(old_features, old_math),
            "after": feature_counts(features, math_utils),
        },
    }
    destination = ROOT / "重构验证" / "缺陷与复用证据.json"
    Path(destination).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
