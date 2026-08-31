from __future__ import annotations

import numpy as np
import pandas as pd

from inv_trend.core.math_utils import rolling_percentile


def _midrank_percentile(series: pd.Series, lookback: int) -> pd.Series:
    return series.rolling(lookback, min_periods=lookback).apply(
        lambda values: float(
            ((values < values[-1]).sum() + 0.5 * (values == values[-1]).sum())
            / len(values)
        ),
        raw=True,
    )


def test_canonical_percentile_matches_independent_midrank_reference() -> None:
    rng = np.random.default_rng(20260821)
    # Rounded values deliberately introduce ties; those ties must receive
    # their probability midpoint instead of being promoted to the upper edge.
    values = np.round(rng.lognormal(mean=-3.0, sigma=0.4, size=600), 4)
    series = pd.Series(values)
    expected = _midrank_percentile(series, 120)
    actual = rolling_percentile(series, 120, min_periods=120)
    np.testing.assert_allclose(
        actual.to_numpy(), expected.to_numpy(), equal_nan=True, rtol=0, atol=0
    )
