# D1 Equity Overlay 3x Backtest Report

Date: 2026-04-21

## Scope

This test adds the requested US equities and ETFs to the existing external XAU/XAG/BTC/ETH universe.

Requested equity mapping:

| User input | Test symbol |
|---|---|
| 消费 | XLY |
| AMZNo | AMZN |
| Others | NVDA, MU, AMD, TSM, SNDK, AVGO, QQQ, SPY, ORCL, MSFT, PLTR, NFLX, META, AAPL, TSLA, GOOGL |

Data sources:

| Asset group | Source | Bar |
|---|---|---|
| XAU/XAG | Dukascopy H4, resampled to D1 | D1 |
| BTC/ETH | Binance H4, resampled to D1 | D1 |
| Equities/ETFs | Nasdaq historical daily endpoint | D1 |

Yahoo Finance was attempted first but returned `YFRateLimitError` for all requested tickers. Stooq was also checked, but the CSV endpoint now requires API key/captcha, so Nasdaq was used as the fallback source.

Important data boundary: the Nasdaq endpoint returned mostly `2016-04-20` through `2026-04-20`, not full IPO history. `PLTR` starts `2020-09-30`; `SNDK` starts `2025-02-13`, so `SNDK` is not the old SanDisk full history and should not be used for model selection.

## Strategy Settings

All equity-overlay tests use D1 Turtle settings equivalent to the previous `120/330 H4` baseline:

| Parameter | Value |
|---|---:|
| N | 20 D1 bars |
| fast entry | 20 D1 high/low breakout |
| slow entry | 55 D1 high/low breakout |
| fast exit | 10 D1 reverse breakout |
| slow exit | 20 D1 reverse breakout |
| stop | 2N |
| add step | 0.5N |
| trigger | close breakout, next-bar open execution |
| max total leverage | 3.0x |
| max direction leverage | 2.0x |

Policy:

| Group | Direction |
|---|---|
| XAU/XAG | long only |
| BTC/ETH | long and short |
| Equities/ETFs main test | long only |
| Equities/ETFs diagnostic test | long and short |

## Data Check

| Symbol group | Symbols | Available period |
|---|---|---|
| 2016 equity set | NVDA, MU, AMD, TSM, AVGO, QQQ, SPY, XLY, ORCL, MSFT, NFLX, META, AAPL, TSLA, GOOGL, AMZN | 2016-04-20 to 2026-04-20 |
| PLTR | PLTR | 2020-09-30 to 2026-04-20 |
| SNDK | SNDK | 2025-02-13 to 2026-04-20 |

Nasdaq equity data quality:

| Check | Result |
|---|---:|
| duplicate rows | 0 |
| bad OHLC rows | 0 |
| bars for main 2016 symbols | 2514 each |
| bars for PLTR | 1393 |
| bars for SNDK | 296 |

The equity `abnormal_gap_count` values are mostly exchange holidays and long weekends under the generic data-quality heuristic.

## Main Results

| Run | Symbols | Sample | Final equity | Total return | CAGR | Max DD | Vol | Sharpe-like | MAR | Trades |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Core 4 D1, common BTC | 4 | 2017-08-17 to 2026-04-20 | 339572.50 | 3295.73% | 50.14% | -68.74% | 49.05% | 1.073 | 0.729 | 141 |
| Equities only, no PLTR/SNDK | 16 | 2016-04-20 to 2026-04-20 | 147759.14 | 1377.59% | 30.91% | -33.44% | 29.37% | 1.065 | 0.924 | 405 |
| Core 4 + equities, no PLTR/SNDK | 20 | 2017-08-17 to 2026-04-20 | 386067.67 | 3760.68% | 52.38% | -51.05% | 49.41% | 1.104 | 1.026 | 408 |
| Core 4 + all equities, staggered full | 22 | 2005-01-02 to 2026-04-20 | 1530592.38 | 15205.92% | 26.65% | -50.75% | 34.24% | 0.863 | 0.525 | 523 |
| Core 4 + all equities, equity long/short diagnostic | 22 | 2005-01-02 to 2026-04-20 | 569157.94 | 5591.58% | 20.90% | -53.55% | 35.27% | 0.714 | 0.390 | 808 |

Primary model-selection result should be the 20-symbol common run excluding PLTR/SNDK:

`core4_plus_equities_common_2017_no_pltr_sndk`

Reason: it keeps a stable universe from the BTC/ETH start date and avoids letting `SNDK`'s short 2025 sample dominate the conclusion.

## Comparison To Previous H4 Baselines

| Version | Bar | Symbols | Policy | Sample | Final equity | CAGR | Max DD | Sharpe-like | MAR | Trades |
|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|
| Previous XAU/BTC | H4 | 2 | no XAU short | 2017-08-17 to 2026-04-20 | 92044.60 | 29.17% | -24.42% | 1.089 | 1.195 | 117 |
| Previous XAU/XAG/BTC/ETH | H4 | 4 | no metals short | 2017-08-17 to 2026-04-20 | 172299.08 | 38.86% | -40.20% | 1.067 | 0.967 | 253 |
| New core 4 | D1 | 4 | no metals short | 2017-08-17 to 2026-04-20 | 339572.50 | 50.14% | -68.74% | 1.073 | 0.729 | 141 |
| New core 4 + stocks | D1 | 20 | no metals short, equities long only | 2017-08-17 to 2026-04-20 | 386067.67 | 52.38% | -51.05% | 1.104 | 1.026 | 408 |

Interpretation:

- Adding equities improves the D1 core-4 result from `339572.50` to `386067.67`.
- More importantly, it reduces D1 core-4 max drawdown from `-68.74%` to `-51.05%`.
- Compared with the previous H4 4-asset baseline, the new D1 stock-overlay version has higher return but deeper drawdown.
- The best risk-adjusted result is still not simply "more assets is always better"; bar frequency and execution model matter.

## Symbol Contribution, Main 20-Symbol Common Run

Run: `core4_plus_equities_common_2017_no_pltr_sndk`

| Symbol / side | Trades | Net PnL |
|---|---:|---:|
| BTC long | 13 | 102035.90 |
| MU long | 15 | 92289.52 |
| XAG long | 14 | 42386.20 |
| XAU long | 15 | 33989.66 |
| NVDA long | 28 | 32969.43 |
| BTC short | 19 | 18132.36 |
| AMD long | 18 | 17280.67 |
| ETH short | 18 | 15334.09 |
| META long | 16 | 12182.00 |
| MSFT long | 19 | 11883.57 |
| QQQ long | 19 | 10736.58 |
| TSLA long | 21 | 10003.57 |
| AVGO long | 26 | 8512.02 |
| AMZN long | 14 | 3766.07 |
| AAPL long | 16 | 1951.08 |
| GOOGL long | 19 | 738.50 |
| XLY long | 13 | -710.35 |
| SPY long | 19 | -1012.56 |
| TSM long | 19 | -6227.04 |
| ORCL long | 23 | -6426.53 |
| NFLX long | 26 | -8535.78 |
| ETH long | 18 | -15211.31 |

The strongest equity additions are `MU`, `NVDA`, `AMD`, `META`, `MSFT`, `QQQ`, `TSLA`, and `AVGO`.

Weak or negative contributors in this run are `NFLX`, `ORCL`, `TSM`, `SPY`, `XLY`, and `ETH long`.

## Exit Breakdown

Main 20-symbol common run:

| Exit type | Trades | Net PnL | Win rate | Avg PnL |
|---|---:|---:|---:|---:|
| trend_exit | 129 | 545434.13 | 71.32% | 4228.17 |
| stop | 271 | -168752.26 | 11.44% | -622.70 |
| end_of_test | 8 | -614.20 | 50.00% | -76.77 |

This is normal Turtle behavior: most stopped trades lose, and the system depends on fewer large trend exits.

## Largest Trades

Main 20-symbol common run:

| Trade | PnL |
|---|---:|
| BTC long, 2020-10-22 to 2021-05-18 | 114366.21 |
| MU long, 2025-09-08 to 2026-03-27 | 90983.30 |
| XAG long, 2025-11-30 to 2026-02-05 | 44168.12 |
| BTC short, 2022-05-06 to 2022-07-20 | 30656.45 |
| XAU long, 2025-01-22 to 2025-05-01 | 27113.79 |
| XAG long, 2025-06-06 to 2025-10-27 | 25023.83 |
| BTC long, 2023-10-24 to 2024-01-22 | 22033.58 |
| NVDA long, 2023-02-24 to 2023-08-10 | 20071.11 |

Largest losses:

| Trade | PnL |
|---|---:|
| NFLX long, 2026-04-13 to 2026-04-17 | -9857.03 |
| BTC long, 2025-05-09 to 2025-06-05 | -9606.21 |
| BTC short, 2024-07-05 to 2024-07-14 | -8939.88 |
| ETH long, 2024-05-22 to 2024-06-11 | -7809.12 |
| ETH long, 2022-08-11 to 2022-08-19 | -7250.41 |

## Drawdown

| Run | Peak | Trough | Max DD | Recovery |
|---|---|---|---:|---|
| Core 4 D1 common | 2021-04-15 | 2024-02-13 | -68.74% | 2026-01-28 |
| Core 4 + equities common | 2026-01-28 | 2026-04-20 | -51.05% | unrecovered at test end |
| Equities only common | 2020-02-19 | 2020-05-28 | -33.44% | 2020-08-17 |

The stock overlay improves the D1 core-4 drawdown, but the combined book is currently in an unrecovered drawdown at the end of the test window.

## Leverage Usage

Approximate mark-to-market leverage during the main 20-symbol common run:

| Metric | Max |
|---|---:|
| Gross leverage | 2.829x |
| Long leverage | 2.318x |
| Short leverage | 1.404x |
| Crypto cluster | 1.519x |
| Precious metals cluster | 1.498x |
| Broad equity cluster | 0.966x |
| Consumer cluster | 0.857x |
| Semiconductor cluster | 0.812x |

The strategy did not materially exceed the 3x gross target, but mark-to-market moves can push cluster usage slightly above entry-time caps.

## SNDK Warning

The staggered full 22-symbol run includes `SNDK`, and `SNDK` contributes `385690.32` net PnL from only 7 trades. Its largest single trade is `345049.36`.

That result should not be treated as robust because:

- Nasdaq only returned `296` bars for `SNDK`.
- The period starts `2025-02-13`.
- It is not a long SanDisk history.
- It dominates the full staggered result.

For model selection, exclude `SNDK`. For a live watchlist, it can be kept as a future-trading candidate after more history accumulates.

## Conclusion

Adding the requested stocks improves the D1 multi-asset system, especially in the common 2017 sample excluding short-history `PLTR/SNDK`:

- final equity improves from `339572.50` to `386067.67` versus D1 core 4
- max drawdown improves from `-68.74%` to `-51.05%`
- Sharpe-like improves from `1.073` to `1.104`
- MAR improves from `0.729` to `1.026`

But compared with the previous H4 4-asset baseline, the D1 stock-overlay version is more aggressive:

- higher return
- more trades
- deeper drawdown
- currently unrecovered drawdown at the end of the sample

Recommended next baseline:

`core4_plus_equities_common_2017_no_pltr_sndk`

Recommended next experiment:

1. Keep equities long-only.
2. Exclude `SNDK` from model selection.
3. Keep `PLTR` in a separate late-start report, not in the main common sample.
4. Test whether weak contributors `NFLX`, `ORCL`, `TSM`, `SPY`, `XLY`, and `ETH long` should be reduced, filtered, or removed.
