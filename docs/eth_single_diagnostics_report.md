# ETH Single-Symbol Turtle Diagnostics

Date: 2026-04-21

## Purpose

The question was why ETH looks like a strong long/short trend instrument visually, but had weak contribution in the multi-asset Turtle backtests.

This diagnostic isolates ETH and runs:

- D1 `20/55` Turtle
- H4 `120/330` Turtle, daily-equivalent
- H4 `60/165` Turtle, half-length
- long/short, long-only, and short-only
- portfolio caps and relaxed single-symbol caps

Data:

`data_external_xau_btc_xag_eth/processed/external/H4/ETHUSDT_BINANCE.csv`

Output:

`outputs/eth_single_diagnostics`

## Data Quality

| Timeframe | Bars | Start | End | Duplicate | Bad OHLC | Abnormal gaps |
|---|---:|---|---|---:|---:|---:|
| H4 | 18992 | 2017-08-17 04:00 UTC | 2026-04-20 00:00 UTC | 0 | 0 | 8 |
| D1 | 3169 | 2017-08-17 00:00 UTC | 2026-04-20 00:00 UTC | 0 | 0 | 0 |

Data continuity is not the main problem.

Important caveat: this is Binance spot kline data. Short testing uses spot price path plus fixed cost assumptions, not real perpetual futures funding and exchange-specific short execution.

## Main Standalone Results

Portfolio cap means the same ETH caps used inside the multi-asset system: crypto cluster 1.5x and ETH symbol max leverage 1.5x.

| Run | Final equity | CAGR | Max DD | Sharpe-like | MAR | Trades | Long PnL | Short PnL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| D1 20/55 long-only | 86861.36 | 28.30% | -48.12% | 0.816 | 0.588 | 27 | 76861.36 | 0.00 |
| D1 20/55 long/short | 106422.98 | 31.34% | -49.56% | 0.851 | 0.632 | 46 | 88883.64 | 7539.34 |
| D1 20/55 short-only | 13595.84 | 3.61% | -19.76% | 0.331 | 0.182 | 20 | 0.00 | 3595.84 |
| H4 120/330 long-only | 16172.69 | 5.70% | -68.02% | 0.340 | 0.084 | 71 | 6172.69 | 0.00 |
| H4 120/330 long/short | 29002.15 | 13.07% | -69.27% | 0.500 | 0.189 | 110 | 11538.51 | 7463.64 |
| H4 120/330 short-only | 12798.88 | 2.89% | -41.99% | 0.239 | 0.069 | 44 | 0.00 | 2798.88 |
| H4 60/165 long-only | 48866.21 | 20.08% | -66.27% | 0.659 | 0.303 | 93 | 38866.21 | 0.00 |
| H4 60/165 long/short | 74487.13 | 26.06% | -70.55% | 0.720 | 0.369 | 162 | 54057.11 | 10430.02 |
| H4 60/165 short-only | 24467.72 | 10.87% | -56.34% | 0.503 | 0.193 | 69 | 0.00 | 14467.72 |

Relaxed single-symbol 3x caps:

| Run | Final equity | CAGR | Max DD | Sharpe-like | MAR | Trades | Long PnL | Short PnL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| H4 120/330 relaxed long/short | 59112.92 | 22.74% | -72.78% | 0.644 | 0.312 | 125 | 28805.73 | 20307.19 |
| H4 60/165 relaxed long/short | 139110.33 | 35.48% | -67.37% | 0.796 | 0.527 | 171 | 79667.02 | 49443.31 |

## Key Finding

ETH is not a bad trend asset by itself.

The best clean standalone result under the portfolio cap is:

`D1 20/55 long/short`

Final equity is `106422.98`, CAGR `31.34%`, but max drawdown is still `-49.56%` and MAR is only `0.632`.

That means ETH trends, but the current Turtle implementation extracts that trend with poor drawdown efficiency compared with BTC / metals / semiconductors.

## Why H4 120/330 Looks Bad

H4 `120/330` is theoretically equivalent to D1 `20/55`, but in practice it is much noisier because:

1. Signals can trigger every 4 hours instead of once per day.
2. ETH often spikes above/below a breakout level intraday and reverses quickly.
3. The strategy executes next bar open and uses 2N stops; fast H4 reversals create many stopped trades.
4. H4 entries in range regimes often buy the upper edge or short the lower edge.

Result:

| Version | Final equity | Max DD | MAR | Trades |
|---|---:|---:|---:|---:|
| D1 20/55 long/short | 106422.98 | -49.56% | 0.632 | 46 |
| H4 120/330 long/short | 29002.15 | -69.27% | 0.189 | 110 |

So the problem is not that `120/330 H4` is mathematically wrong. The problem is that ETH is more sensitive to intraday false breakouts and stop churn.

## Why The Visual Chart Looks Better Than The Backtest

The chart visually shows broad ETH trends and channels. But the current Turtle rule is not an EMA/channel discretionary strategy.

Current rule:

- enter long only after close breaks prior 20/55 high
- enter short only after close breaks prior 20/55 low
- execute next bar open
- exit on reverse 10/20 breakout or 2N stop
- add every 0.5N

In a broad rising range or wedge, a human may see:

- buy near rising support
- avoid shorting lower boundary
- take trend context from EMA/channel slope

The Turtle rule instead often:

- buys after price already reaches upper resistance
- shorts after price already reaches lower support
- gets stopped by ETH's fast reversals

This explains why ETH can look like a great trend product visually while this specific breakout implementation has mediocre MAR.

## Top Standalone D1 ETH Trades

Best D1 long/short trades:

| Entry | Exit | Side | PnL | Exit |
|---|---|---|---:|---|
| 2023-10-24 | 2024-03-20 | long | 45749.58 | trend_exit |
| 2020-12-28 | 2021-05-20 | long | 30078.02 | trend_exit |
| 2025-07-11 | 2025-09-23 | long | 24389.07 | trend_exit |
| 2025-02-03 | 2025-05-09 | short | 7400.51 | trend_exit |
| 2022-05-10 | 2022-07-17 | short | 7041.20 | trend_exit |

Worst D1 trades:

| Entry | Exit | Side | PnL | Exit |
|---|---|---|---:|---|
| 2025-11-05 | 2025-12-09 | short | -3344.42 | stop |
| 2024-05-22 | 2024-06-11 | long | -3259.46 | stop |
| 2024-11-28 | 2024-12-09 | long | -3073.46 | stop |
| 2025-05-10 | 2025-05-18 | long | -2998.70 | stop |
| 2024-08-05 | 2024-08-14 | short | -2355.45 | stop |

The winners exist. The issue is that the equity curve pays for them with deep drawdowns and many stop-outs.

## Annual Behavior

D1 20/55 long/short:

| Year | Return | Max DD |
|---:|---:|---:|
| 2017 | 62.44% | -16.94% |
| 2018 | 14.22% | -49.56% |
| 2019 | -4.58% | -14.01% |
| 2020 | 11.27% | -34.09% |
| 2021 | 132.42% | -42.85% |
| 2022 | 1.90% | -14.70% |
| 2023 | 28.49% | -20.51% |
| 2024 | 30.14% | -32.54% |
| 2025 | 28.61% | -16.48% |
| 2026 | 0.03% | -13.65% |

ETH has strong years, but the strategy also eats very large drawdowns in 2018, 2020, and 2021.

## Why ETH Was Worse In The Multi-Asset Portfolio

Standalone D1 ETH long/short:

| Side | Net PnL |
|---|---:|
| long | 88883.64 |
| short | 7539.34 |

But in the previous 20-symbol D1 portfolio:

| Side | Net PnL |
|---|---:|
| ETH long | -15211.31 |
| ETH short | 15334.09 |

Reason:

The multi-asset allocator does not take every ETH trade. ETH competes with BTC, metals, and equities for:

- total 1N risk budget
- long direction risk budget
- crypto cluster budget
- leverage budget
- signal ranking

As a result, the accepted ETH long trades inside the portfolio are not the same as standalone ETH trades. Some of ETH's best standalone long trades are skipped, resized, or cut differently because other assets already consume the risk budget.

Example:

Standalone ETH D1 captures:

| Trade | Standalone PnL |
|---|---:|
| 2023-10-24 -> 2024-03-20 long | 45749.58 |
| 2020-12-28 -> 2021-05-20 long | 30078.02 |
| 2025-07-11 -> 2025-09-23 long | 24389.07 |

In the 20-symbol portfolio, ETH long trades are a different subset and include many short-lived stopped trades. This is why ETH can be positive standalone and negative in the portfolio.

## Practical Interpretation

ETH is tradable, but not with the same confidence as BTC under this exact Turtle implementation.

Current findings:

1. ETH standalone D1 works, but MAR is weak.
2. ETH H4 120/330 is a poor fit because it creates too many false breakout/stop cycles.
3. ETH H4 60/165 improves return, but drawdown remains very high.
4. ETH short-only is not good standalone.
5. In a multi-asset book, ETH long is crowded out by BTC and other higher-quality signals.
6. ETH's visual trendiness is real, but the current Donchian breakout rule does not use EMA/channel context.

## Recommendation

Do not conclude "ETH is bad."

Conclude:

`ETH is not well captured by the current shared Turtle rule, especially on H4 120/330 and inside the multi-asset allocator.`

Recommended next ETH-specific tests:

1. Keep ETH as a separate sleeve, not in the same crypto cluster budget as BTC.
2. Test D1-only ETH signals rather than H4 `120/330`.
3. Test H4 `60/165` but with lower leverage and a trend filter.
4. Add an EMA / channel regime filter before long entries.
5. Test stop logic `2.5N` or volatility trailing exit instead of fixed `2N`.
6. If testing shorts seriously, switch from spot klines to perpetual futures data with funding included.

The current result is not a data-quality failure. It is mostly a strategy-fit and portfolio-allocation issue.
