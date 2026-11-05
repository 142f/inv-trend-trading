"""Reproducible micro-benchmarks for the two remaining hot paths.

Covers:
1. ``state_transitions``  -- per-bar state re-evaluation on a prepared daily frame.
2. ``merge_current``       -- incoming-bar conflict detection against existing bars.

Run:
    .venv/Scripts/python.exe scripts/benchmark_daily_hot_paths.py [--repeat N]

The numbers are wall-clock on one machine; only the *same-machine* before/after
ratio is meaningful.
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from inv_trend.application.strategy_config import DailyChecksConfig  # noqa: E402
from inv_trend.core.strategy.daily.analysis import (  # noqa: E402
    prepare_daily_analysis,
    state_transitions,
)


def _bars(count: int, *, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2020-01-01", periods=count, freq="D", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.012, count)))
    open_ = np.concatenate(([close[0]], close[:-1]))
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.004,
            "low": np.minimum(open_, close) * 0.996,
            "close": close,
            "volume": rng.integers(500_000, 2_000_000, count),
            "is_complete": True,
        },
        index=index,
    )


def _time(call, repeat: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "repeat": repeat,
    }


def bench_state_transitions(*, repeat: int) -> dict[str, dict[str, float]]:
    config = DailyChecksConfig()
    results: dict[str, dict[str, float]] = {}
    for count in (200, 400, 600):
        bars = _bars(count)
        prepared = prepare_daily_analysis(
            bars, config, session_anchor=bars.index[0]
        )
        transitions = state_transitions(prepared)
        results[f"bars_{count}"] = {
            **_time(lambda p=prepared: state_transitions(p), repeat),
            "transitions": float(len(transitions)),
        }
    return results


def bench_merge_current(*, repeat: int) -> dict[str, dict[str, float]]:
    import inv_trend.data.pipeline.行情阶段_v1 as stage_module

    rows = 60_000
    bars = _bars(rows, seed=11).reset_index().rename(columns={"index": "timestamp"})
    bars["ingested_at"] = pd.Timestamp("2024-01-01", tz="UTC")
    adapter = object.__new__(stage_module.BarsCore)

    # Incremental update: the provider re-sends a short window.
    incremental_existing = bars.iloc[: rows - 400].reset_index(drop=True)
    incremental = bars.iloc[rows - 400 :].reset_index(drop=True).copy()
    # Backfill/rebuild: the provider re-sends the whole history, a few bars revised.
    existing = bars.copy()
    full = bars.copy()
    full.loc[::97, "close"] = full.loc[::97, "close"] * 1.01

    return {
        "rows_60000_incoming_400": _time(
            lambda: adapter.merge_current(
                "SPY", "D1", incremental, existing=incremental_existing
            ),
            repeat,
        ),
        "rows_60000_incoming_60000": _time(
            lambda: adapter.merge_current("SPY", "D1", full, existing=existing),
            repeat,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    payload = {
        "state_transitions": bench_state_transitions(repeat=args.repeat),
        "merge_current": bench_merge_current(repeat=args.repeat),
    }
    for section, values in payload.items():
        print(section)
        for name, metrics in values.items():
            print(f"  {name}: {metrics}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
