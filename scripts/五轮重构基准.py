"""Compare working code with an immutable Git baseline; no I/O in timed regions."""
from __future__ import annotations

import argparse
import gc
from functools import partial
import json
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
from time import perf_counter
import tracemalloc
import types

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BASELINE = "fa274b51e5ac0bc1f85252b6aebbe71d4fd2fcfa"


def baseline_module(path, name, replacements=()):
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:{path}"], cwd=ROOT
    ).decode("utf-8")
    for old, new in replacements:
        source = source.replace(old, new)
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"{BASELINE}:{path}", "exec"), module.__dict__)
    return module


def bars(size):
    rng = np.random.default_rng(20260906)
    close = 100 + np.cumsum(rng.normal(0.02, 0.3, size))
    return pd.DataFrame(
        dict(open=close, high=close + 1, low=close - 1, close=close, volume=1000.0),
        index=pd.date_range("2000-01-01", periods=size, tz="UTC"),
    )


def compare(before, after, runs=7):
    expected, actual = before(), after()
    if isinstance(expected, pd.DataFrame):
        pd.testing.assert_frame_equal(expected, actual, rtol=1e-12, atol=1e-12)
    elif isinstance(expected, pd.Series):
        pd.testing.assert_series_equal(expected, actual, rtol=1e-12, atol=1e-12)
    else:
        assert expected == actual
    timings = [[], []]
    # Alternate order to avoid always assigning warm caches to the new code.
    for run in range(runs):
        for index in ((0, 1) if run % 2 == 0 else (1, 0)):
            gc.collect()
            start = perf_counter()
            (before, after)[index]()
            timings[index].append(perf_counter() - start)
    peaks = []
    for operation in (before, after):
        gc.collect()
        tracemalloc.start()
        operation()
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
    old, new = map(median, timings)
    return dict(before_seconds=old, after_seconds=new, speedup=old / new,
                before_peak_bytes=peaks[0], after_peak_bytes=peaks[1],
                samples_seconds=timings, equivalent=True)


def scanner_comparison():
    from inv_trend.adapters.detector.engine.scanner import TurtleScanner
    from inv_trend.adapters.detector.models import AssetConfig, Market, StrategyConfig

    baseline_module("src/inv_trend/core/features.py", "inv_trend.core._baseline_features",
                    (("from .math_utils", "from ._baseline_math"),))
    old = baseline_module(
        "src/inv_trend/adapters/detector/engine/scanner.py",
        "inv_trend.adapters.detector.engine._baseline_scanner",
        (("inv_trend.core.features", "inv_trend.core._baseline_features"),
         ("inv_trend.core.math_utils", "inv_trend.core._baseline_math")),
    )
    frame = bars(2000)
    asset = AssetConfig(symbol="BENCH", instrument="BENCH_SPOT", market=Market.CRYPTO,
                        data_source="fixture", timeframes=("D1",), adjustment="none")
    config = StrategyConfig()

    def prepare(scanner_class):
        result = scanner_class(config).prepare(frame, asset, "D1")
        result.attrs = {}
        return result

    return compare(partial(prepare, old.TurtleScanner), partial(prepare, TurtleScanner))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from inv_trend.core import math_utils, resampling, features
    from inv_trend.application.backtest import metrics

    old_math = baseline_module("src/inv_trend/core/math_utils.py", "inv_trend.core._baseline_math")
    if args.round == 1:
        values = bars(2000).close.round(1)
        before = partial(old_math.rolling_percentile, values, 120)
        after = partial(math_utils.rolling_percentile, values, 120)
        scenario = "rolling_percentile_2000"
    elif args.round == 2:
        frame = bars(20000)
        before = partial(old_math.true_range, frame.high, frame.low, frame.close)
        after = partial(math_utils.true_range, frame.high, frame.low, frame.close)
        scenario = "true_range_20000"
    elif args.round == 3:
        old = baseline_module("src/inv_trend/core/resampling.py", "inv_trend.core._baseline_resampling")
        frame = bars(5000)
        before = partial(old.aggregate_completed_sessions, frame, 5, anchor=frame.index[0])
        after = partial(resampling.aggregate_completed_sessions, frame, 5, anchor=frame.index[0])
        scenario = "aggregate_sessions_5000"
    elif args.round == 4:
        old = baseline_module("src/inv_trend/core/features.py", "inv_trend.core._baseline_features",
                              (("from .math_utils", "from ._baseline_math"),))
        frame = bars(2000)
        kwargs = dict(atr_period=20, donchian_periods=(10, 20, 55),
                      sma_lags=tuple((p, lag) for p in (10, 20, 60) for lag in (0, 1, 2)),
                      ema_periods=(12, 26, 60), macd_periods=(12, 26, 9), dmi_period=14,
                      volume_sma_lags=((20, 0), (20, 1)))
        old_request, request = old.FeatureRequest(**kwargs), features.FeatureRequest(**kwargs)
        def prepare(module, spec):
            result = module.FeatureCache().prepare(frame, spec).frame
            result.attrs = {}  # Classes in isolated baseline have different identities.
            return result
        before = partial(prepare, old, old_request)
        after = partial(prepare, features, request)
        scenario = "shared_features_2000"
    else:
        baseline_module("src/inv_trend/core/performance.py", "inv_trend.core._baseline_performance")
        old = baseline_module("src/inv_trend/application/backtest/metrics.py", "_baseline_metrics",
                              (("inv_trend.core.performance", "inv_trend.core._baseline_performance"),))
        # One trough/recovery makes the performance comparison independent of
        # the separately tested fix for recovery after multiple deeper troughs.
        equity = pd.Series(np.r_[np.linspace(100, 200, 5000),
                                 np.linspace(199, 100, 2500), np.linspace(101, 250, 2500)],
                           index=bars(10000).index) * 1000
        trades = pd.DataFrame(dict(pnl=[10., -5., 20.], holding_bars=[2, 3, 4]))
        before = partial(old.performance_metrics, equity, trades)
        after = partial(metrics.performance_metrics, equity, trades)
        scenario = "performance_metrics_10000"
    result = dict(baseline=BASELINE, round=args.round, scenario=scenario,
                  python=platform.python_version(), numpy=np.__version__, pandas=pd.__version__,
                  platform=platform.platform(), runs=7, **compare(before, after))
    if args.round == 1:
        result["integrated_scanner_prepare_2000"] = scanner_comparison()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
