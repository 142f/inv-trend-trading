"""Run a small synthetic multi-asset Turtle backtest.

This demo uses generated data only. Replace ``data`` with real, adjusted OHLC
data before drawing any trading conclusion.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from turtle_multi_asset import AssetSpec, TurtleBacktester, TurtleRules


def make_ohlc(seed: int, drift: float, periods: int = 260) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-01", periods=periods)
    noise = rng.normal(loc=drift, scale=0.018, size=periods)
    close = 100 * np.exp(np.cumsum(noise))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, periods))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.015, periods))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.015, periods))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close},
        index=index,
    )


def main() -> None:
    data = {
        "SPY": make_ohlc(1, drift=0.0010),
        "QQQ": make_ohlc(2, drift=0.0012),
        "XAU": make_ohlc(3, drift=0.0008),
        "BTC": make_ohlc(4, drift=0.0016),
    }
    specs = {
        "SPY": AssetSpec("SPY", "etf", "us_index", can_short=True, qty_step=1),
        "QQQ": AssetSpec("QQQ", "etf", "us_growth", can_short=True, qty_step=1),
        "XAU": AssetSpec("XAU", "metal", "precious_metals", can_short=True, qty_step=0.01),
        "BTC": AssetSpec(
            "BTC",
            "crypto",
            "crypto",
            can_short=True,
            qty_step=0.001,
            max_units=2,
            unit_1n_risk_pct=0.003,
            max_symbol_1n_risk_pct=0.01,
        ),
    }
    rules = TurtleRules(
        trigger_mode="close",
        max_total_1n_risk_pct=0.08,
        max_direction_1n_risk_pct=0.06,
        cluster_1n_risk_pct={
            "us_index": 0.025,
            "us_growth": 0.025,
            "precious_metals": 0.025,
            "crypto": 0.015,
        },
    )
    result = TurtleBacktester(data, specs, rules, initial_equity=100_000).run()
    print("Metrics")
    for key, value in result.metrics.items():
        print(f"{key}: {value:.4f}")
    print("\nLast equity")
    print(result.equity_curve.tail())
    print("\nOrders")
    print(result.orders.tail(10))


if __name__ == "__main__":
    main()
