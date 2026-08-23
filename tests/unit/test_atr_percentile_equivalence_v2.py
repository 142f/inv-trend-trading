from __future__ import annotations

import numpy as np
import pandas as pd


def _legacy_percentile(series: pd.Series, lookback: int) -> pd.Series:
    return series.rolling(lookback, min_periods=lookback).apply(
        lambda values: float((values <= values[-1]).mean()), raw=True
    )


def _optimized_percentile(series: pd.Series, lookback: int) -> pd.Series:
    return series.rolling(lookback, min_periods=lookback).rank(method="max", pct=True)


def test_rolling_rank_matches_legacy_percentile_including_ties() -> None:
    rng = np.random.default_rng(20260821)
    # Rounded values deliberately introduce ties; equivalence must still hold.
    values = np.round(rng.lognormal(mean=-3.0, sigma=0.4, size=600), 4)
    series = pd.Series(values)
    old = _legacy_percentile(series, 120)
    new = _optimized_percentile(series, 120)
    np.testing.assert_allclose(new.to_numpy(), old.to_numpy(), equal_nan=True, rtol=0, atol=0)
