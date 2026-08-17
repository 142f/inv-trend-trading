# Backtest Drawdown Comparison Notes

## What Changed In The Refactor Result

The conservative refactor did not change the generated backtest result. The before/after H4 XAU/BTC outputs are identical at the result-file level: `metrics.json`, `orders.csv`, and `trades.csv` have identical hashes. The fixed sample final equity remains `16021.954802095695`, with `trade_count = 72` and `max_drawdown = -17.8232%`.

This means the refactor only changed code structure in the record-building path; it did not change signal timing, order fills, cash accounting, trade rows, or metrics.

## How To Read The Logs And Output Files

- `metrics.json`: portfolio-level summary for one run. Important fields are total return, CAGR, max drawdown, volatility, Sharpe-like, MAR, and trade count.
- `equity_curve.csv`: time series of strategy equity. All period drawdown tables in this report are computed from this file.
- `orders.csv`: every filled order row. Identical row count/hash before and after means execution output did not drift.
- `trades.csv`: completed round-trip trades. Use this for trade count, win/loss diagnostics, and symbol-side PnL.
- `trade_details.csv`: unit-level trade details, useful when pyramiding creates multiple units inside one completed trade.
- `summary.csv`: experiment-level aggregation produced by audit scripts. It is useful for comparing models and asset-sleeve removals.

Drawdown here means portfolio equity peak-to-trough loss inside the selected time window. It is not buy-and-hold drawdown for each asset.

## Model-Level Result Summary

| Model | Asset group | Period | Final equity | CAGR | Max DD | MAR | Trades |
|---|---|---|---:|---:|---:|---:|---:|
| Refactor baseline XAU/BTC H4 | metals+crypto | 2022-01-02 -> 2026-04-20 | 16,021.95 | 11.61% | -17.82% | 0.651 | 72.00 |
| Refactor after XAU/BTC H4 | metals+crypto | 2022-01-02 -> 2026-04-20 | 16,021.95 | 11.61% | -17.82% | 0.651 | 72.00 |
| Core4 metals+crypto | metals+crypto | 2017-08-17 -> 2026-04-20 | 339,572.50 | 50.14% | -68.74% | 0.729 | 141.00 |
| Candidate 9 metals+crypto+semis | metals+crypto+tech | 2017-08-17 -> 2026-04-20 | 1,497,084.87 | 78.15% | -50.14% | 1.559 | 217.00 |
| Revised 8 no ETH short | metals+BTC+tech | 2017-08-17 -> 2026-04-20 | 1,625,439.31 | 79.85% | -46.98% | 1.700 | 198.00 |
| Previous 20 broad universe | metals+crypto+tech broad | 2017-08-17 -> 2026-04-20 | 386,067.67 | 52.38% | -51.05% | 1.026 | 408.00 |
| XAU/BTC/QQQ/SPY | metals+crypto+ETF | 2017-08-17 -> 2026-04-20 | 141,153.11 | 35.69% | -44.69% | 0.799 | 131.00 |
| Top20 crypto D1 | crypto | 2017-08-17 -> 2026-04-22 | 791,498.77 | 26.92% | -39.20% | 0.687 | 267.00 |
| Four asset no metals/ETH short | metals+crypto | 2005-01-02 -> 2026-04-20 | 280,352.82 | 16.95% | -36.91% | 0.459 | 271.00 |


## Period Drawdown Comparison

| Model | Period | Return | CAGR | Max DD | DD peak -> trough | Recovery |
|---|---|---:|---:|---:|---|---|
| Candidate 9 metals+crypto+semis | all | 14870.85% | 78.15% | -50.14% | 2021-04-13 -> 2024-01-04 | 2024-12-15 |
| Candidate 9 metals+crypto+semis | last_1y | 206.70% | 206.94% | -46.56% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Candidate 9 metals+crypto+semis | last_3y | 381.79% | 68.96% | -46.56% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Candidate 9 metals+crypto+semis | last_5y | 243.21% | 27.99% | -46.56% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Candidate 9 metals+crypto+semis | 2017_2019 | 474.90% | 109.11% | -40.65% | 2017-12-16 -> 2018-03-01 | 2018-11-20 |
| Candidate 9 metals+crypto+semis | 2020_2022 | 490.02% | 80.77% | -42.62% | 2021-04-13 -> 2022-04-26 | not recovered in window |
| Candidate 9 metals+crypto+semis | 2023_2026 | 341.36% | 56.84% | -46.56% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Candidate 9 metals+crypto+semis | roll_2021_2026 | 542.17% | 42.05% | -50.14% | 2021-04-13 -> 2024-01-04 | 2024-12-15 |
| Revised 8 no ETH short | all | 16154.39% | 79.85% | -46.98% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Revised 8 no ETH short | last_1y | 220.29% | 220.55% | -46.98% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Revised 8 no ETH short | last_3y | 437.87% | 75.28% | -46.98% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Revised 8 no ETH short | last_5y | 304.95% | 32.30% | -46.98% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Revised 8 no ETH short | 2017_2019 | 417.42% | 100.02% | -42.88% | 2017-12-16 -> 2018-07-25 | 2019-01-13 |
| Revised 8 no ETH short | 2020_2022 | 537.54% | 85.50% | -42.61% | 2021-04-13 -> 2022-04-26 | not recovered in window |
| Revised 8 no ETH short | 2023_2026 | 392.74% | 62.16% | -46.98% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Revised 8 no ETH short | roll_2021_2026 | 657.66% | 46.56% | -46.98% | 2026-01-28 -> 2026-03-30 | not recovered in window |
| Previous 20 broad universe | all | 3760.68% | 52.38% | -51.05% | 2026-01-28 -> 2026-04-20 | not recovered in window |
| Previous 20 broad universe | last_1y | 53.75% | 53.79% | -51.05% | 2026-01-28 -> 2026-04-20 | not recovered in window |
| Previous 20 broad universe | last_3y | 150.11% | 35.77% | -51.05% | 2026-01-28 -> 2026-04-20 | not recovered in window |
| Previous 20 broad universe | last_5y | 81.67% | 12.69% | -51.05% | 2026-01-28 -> 2026-04-20 | not recovered in window |
| Previous 20 broad universe | 2017_2019 | 114.88% | 38.07% | -44.85% | 2017-12-18 -> 2018-05-07 | 2018-12-05 |
| Previous 20 broad universe | 2020_2022 | 697.80% | 99.91% | -39.29% | 2021-05-08 -> 2022-12-06 | not recovered in window |
| Previous 20 broad universe | 2023_2026 | 125.20% | 27.90% | -51.05% | 2026-01-28 -> 2026-04-20 | not recovered in window |
| Previous 20 broad universe | roll_2021_2026 | 241.18% | 26.07% | -51.05% | 2026-01-28 -> 2026-04-20 | not recovered in window |
| Top20 crypto D1 | all | 691.50% | 26.92% | -39.20% | 2025-11-16 -> 2026-01-27 | not recovered in window |
| Top20 crypto D1 | last_1y | 81.92% | 81.99% | -39.20% | 2025-11-16 -> 2026-01-27 | not recovered in window |
| Top20 crypto D1 | last_3y | 142.97% | 34.46% | -39.20% | 2025-11-16 -> 2026-01-27 | not recovered in window |
| Top20 crypto D1 | last_5y | 131.05% | 18.25% | -39.20% | 2025-11-16 -> 2026-01-27 | not recovered in window |
| Top20 crypto D1 | 2017_2019 | 43.68% | 16.52% | -26.74% | 2018-01-13 -> 2018-11-14 | 2019-06-22 |
| Top20 crypto D1 | 2020_2022 | 137.91% | 33.52% | -27.75% | 2021-05-09 -> 2021-08-03 | not recovered in window |
| Top20 crypto D1 | 2023_2026 | 132.01% | 29.01% | -39.20% | 2025-11-16 -> 2026-01-27 | not recovered in window |
| Top20 crypto D1 | roll_2021_2026 | 265.97% | 27.72% | -39.20% | 2025-11-16 -> 2026-01-27 | not recovered in window |
| Four asset no metals/ETH short | all | 2703.53% | 16.95% | -36.91% | 2021-02-21 -> 2023-10-22 | 2024-12-05 |
| Four asset no metals/ETH short | last_1y | 55.94% | 55.99% | -29.52% | 2026-01-29 -> 2026-04-18 | not recovered in window |
| Four asset no metals/ETH short | last_3y | 133.58% | 32.71% | -29.52% | 2026-01-29 -> 2026-04-18 | not recovered in window |
| Four asset no metals/ETH short | last_5y | 91.97% | 13.94% | -29.56% | 2021-05-12 -> 2023-10-22 | 2024-03-06 |
| Four asset no metals/ETH short | 2017_2019 | 79.31% | 21.55% | -34.12% | 2018-01-13 -> 2018-11-14 | 2019-06-22 |
| Four asset no metals/ETH short | 2020_2022 | 310.28% | 60.14% | -31.85% | 2021-02-21 -> 2022-12-28 | not recovered in window |
| Four asset no metals/ETH short | 2023_2026 | 139.14% | 30.25% | -29.52% | 2026-01-29 -> 2026-04-18 | not recovered in window |
| Four asset no metals/ETH short | roll_2021_2026 | 251.82% | 26.80% | -36.91% | 2021-02-21 -> 2023-10-22 | 2024-12-05 |


## Calendar-Year Drawdown Snapshot

| Year | Candidate 9 DD | Revised 8 DD | Top20 crypto DD |
|---:|---:|---:|---:|
| 2017 | -28.57% | -28.52% | -7.65% |
| 2018 | -36.06% | -38.47% | -26.74% |
| 2019 | -32.02% | -32.87% | -17.69% |
| 2020 | -31.23% | -29.65% | -17.34% |
| 2021 | -40.99% | -40.98% | -27.75% |
| 2022 | -23.18% | -17.50% | -9.90% |
| 2023 | -31.29% | -27.21% | -18.95% |
| 2024 | -19.24% | -17.09% | -21.90% |
| 2025 | -23.44% | -23.81% | -36.40% |
| 2026 | -46.56% | -46.98% | -4.75% |


## Asset Sleeve Interpretation

- Crypto is the most indispensable sleeve in the D1 candidate audit. Removing BTC+ETH (`minus_crypto`) drops the strategy from roughly `1.50M` final equity to about `146K`, while max drawdown remains around `-50%`; that means crypto is a return engine, not just a risk reducer.
- Semiconductors are also structurally important. Removing NVDA/AMD/MU/TSM/AVGO (`minus_semis`) leaves about `286K` final equity and worsens max drawdown to roughly `-57%`.
- Metals contribute strongly but carry concentration risk. Tightening the metals cap reduces final equity sharply but improves drawdown, so metals are a high-contribution/high-concentration sleeve.
- ETH short is not supported by the audit. The revised 8-symbol model without ETH short improves final equity, CAGR, max drawdown, Sharpe-like, and MAR versus base 9.
- The weakest robustness signal is start-date sensitivity: `roll_2021_2026` is much weaker than full-sample results, so the full history benefits from earlier large trends and compounding.

## Files Generated

- `outputs/drawdown_comparison/model_summary.csv`
- `outputs/drawdown_comparison/model_period_drawdowns.csv`
- `outputs/drawdown_comparison/sleeve_contribution_snapshot.csv`
