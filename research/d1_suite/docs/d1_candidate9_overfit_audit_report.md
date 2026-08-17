# D1 Candidate 9 Overfit Audit Report

Date: 2026-04-21

## Candidate Under Audit

Original 9-symbol candidate:

```text
XAU long only
XAG long only
BTC long/short
ETH short only
NVDA long only
AMD long only
MU long only
TSM long only
AVGO long only
```

Common sample:

`2017-08-17 -> 2026-04-20`

This audit tries to break the candidate with:

- single-asset removals
- cluster removals
- time segments and rolling windows
- top-trade concentration checks
- cost stress
- cluster-cap stress
- narrow rule perturbations
- benchmark comparisons

Output directory:

`outputs/d1_candidate9_audit`

## Base Result

| Experiment | Final equity | CAGR | Max DD | Sharpe-like | MAR | Trades | Top1 contrib | Top3 contrib |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| base_9 | 1497084.87 | 78.15% | -50.14% | 1.383 | 1.559 | 217 | 46.38% | 78.23% |

The headline result is very strong, but top-trade concentration is high.

Approximate concentration removal:

| Adjustment | Adjusted final equity | Adjusted CAGR |
|---|---:|---:|
| remove top1 trade | 807448.75 | 65.91% |
| remove top3 trades | 333813.08 | 49.85% |
| remove all XAG trades | 865593.63 | 67.25% |
| remove BTC long trades | 1181492.51 | 73.36% |
| remove MU trades | 1291556.27 | 75.15% |

These adjusted results are trade-PnL approximations, not full path reruns. They are useful as concentration diagnostics, not as exact alternate backtests.

## Single-Asset Removal

| Experiment | Removed | Final equity | CAGR | Max DD | Sharpe-like | MAR | Interpretation |
|---|---|---:|---:|---:|---:|---:|---|
| minus_xau | XAU | 586981.86 | 59.92% | -52.60% | 1.162 | 1.139 | XAU matters more than raw contribution suggests |
| minus_xag | XAG | 702680.48 | 63.27% | -42.04% | 1.274 | 1.505 | XAG boosts return but also concentration/drawdown |
| minus_btc | BTC | 163009.31 | 37.96% | -49.72% | 1.130 | 0.763 | BTC is indispensable |
| minus_eth_short | ETH short | 1625439.24 | 79.85% | -46.98% | 1.417 | 1.700 | ETH short is not needed; removing it improves the portfolio |
| minus_nvda | NVDA | 682229.16 | 62.72% | -56.51% | 1.281 | 1.110 | NVDA is important |
| minus_amd | AMD | 660021.70 | 62.10% | -50.55% | 1.255 | 1.229 | AMD is useful |
| minus_mu | MU | 1063502.93 | 71.27% | -49.76% | 1.335 | 1.432 | MU helps, but not as irreplaceable as expected |
| minus_tsm | TSM | 923000.53 | 68.49% | -44.47% | 1.305 | 1.540 | TSM improves return, but removal keeps MAR near base |
| minus_avgo | AVGO | 1197496.40 | 73.62% | -52.56% | 1.358 | 1.401 | AVGO helps, but is less critical |

Important reversal:

`minus_eth_short` is better than `base_9`. The audit does not support keeping ETH short in the main candidate.

## Cluster Removal

| Experiment | Removed | Final equity | CAGR | Max DD | Sharpe-like | MAR | Interpretation |
|---|---|---:|---:|---:|---:|---:|---|
| minus_metals | XAU + XAG | 597097.34 | 60.24% | -40.07% | 1.237 | 1.503 | Metals are major return contributors, but not required for MAR survival |
| minus_btc_family_tail | ETH short | 1625439.24 | 79.85% | -46.98% | 1.417 | 1.700 | Same as minus ETH, best tested result |
| minus_crypto | BTC + ETH | 146164.33 | 30.77% | -50.26% | 1.037 | 0.612 | Crypto is indispensable |
| minus_semis | NVDA/AMD/MU/TSM/AVGO | 286418.37 | 47.23% | -57.00% | 1.054 | 0.828 | Semiconductor sleeve is indispensable |

Conclusion:

- BTC/crypto and semiconductors are truly core.
- Metals are powerful but create concentration.
- ETH short is not a necessary part of the candidate.

## Time Segments

| Segment | Period | Final equity | CAGR | Max DD | Sharpe-like | MAR | Top1 contrib | Top3 contrib |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| segment_1 | 2017-08-17 -> 2019-12-31 | 57460.72 | 109.06% | -40.65% | 1.609 | 2.683 | 55.98% | 84.58% |
| segment_2 | 2020-01-02 -> 2022-12-30 | 56472.54 | 78.34% | -41.51% | 1.368 | 1.887 | 93.95% | 120.43% |
| segment_3 | 2023-01-03 -> 2026-04-20 | 40603.00 | 53.03% | -48.01% | 1.162 | 1.104 | 65.32% | 95.03% |

Interpretation:

- It worked in all three calendar segments.
- But quality deteriorated in 2023-2026: Sharpe-like and MAR drop below the preferred audit thresholds.
- Segment 2 is extremely top-trade dependent.

## Rolling Windows

| Window | Period | Final equity | CAGR | Max DD | Sharpe-like | MAR | Top3 contrib |
|---|---|---:|---:|---:|---:|---:|---:|
| roll_2017_2021 | 2017-08-17 -> 2021-12-31 | 348587.60 | 125.29% | -40.99% | 1.664 | 3.057 | 92.40% |
| roll_2019_2023 | 2019-01-02 -> 2023-12-29 | 112056.38 | 62.32% | -49.01% | 1.230 | 1.272 | 139.06% |
| roll_2021_2026 | 2021-01-04 -> 2026-04-20 | 17057.02 | 10.62% | -44.11% | 0.457 | 0.241 | 115.53% |

This is the largest warning in the audit.

The candidate is not equally strong if started in 2021. That does not invalidate the strategy, but it means the 2017 full-sample result benefits heavily from pre-2021 trend capture and compounding.

## Cost Stress

| Experiment | Final equity | CAGR | Max DD | Sharpe-like | MAR |
|---|---:|---:|---:|---:|---:|
| base_9 | 1497084.87 | 78.15% | -50.14% | 1.383 | 1.559 |
| cost_1p5x | 1401882.33 | 76.81% | -51.47% | 1.368 | 1.492 |
| cost_2x | 1311339.89 | 75.45% | -52.76% | 1.352 | 1.430 |

Cost stress passes.

Even with 2x costs, the result remains stronger than the previous 20-symbol reference:

| Version | Final equity | MAR |
|---|---:|---:|
| cost_2x | 1311339.89 | 1.430 |
| previous 20-symbol reference | 386067.67 | 1.026 |

The edge is not just a low-cost execution artifact.

## Cluster Cap Stress

| Experiment | Change | Final equity | CAGR | Max DD | Sharpe-like | MAR |
|---|---|---:|---:|---:|---:|---:|
| metals_cap_1p0 | Metals cap 1.0x | 434802.34 | 54.48% | -40.65% | 1.165 | 1.340 |
| crypto_cap_1p0 | Crypto cap 1.0x | 1058563.53 | 71.17% | -46.76% | 1.330 | 1.522 |
| semis_cap_1p0 | Semis cap 1.0x | 1403048.51 | 76.83% | -49.88% | 1.367 | 1.540 |
| all_clusters_tighter | Metals/crypto/semis tightened | 501515.28 | 57.05% | -40.56% | 1.234 | 1.406 |

Interpretation:

- `semis_cap_1p0` barely hurts. The semis sleeve is structurally useful, not just overlevered.
- `crypto_cap_1p0` still passes.
- `metals_cap_1p0` hurts final equity hard but improves drawdown.
- `all_clusters_tighter` still beats the old 20-symbol reference on MAR, but final equity falls sharply.

Cluster concentration risk is real, especially metals.

## Rule Perturbation

| Experiment | Final equity | CAGR | Max DD | Sharpe-like | MAR | Interpretation |
|---|---:|---:|---:|---:|---:|---|
| entry_18_50 | 928342.15 | 68.60% | -52.29% | 1.305 | 1.312 | Pass, weaker |
| entry_22_60 | 878795.62 | 67.54% | -46.39% | 1.314 | 1.456 | Pass |
| exit_9_18 | 997744.60 | 70.01% | -49.80% | 1.296 | 1.406 | Pass |
| exit_12_24 | 1173692.46 | 73.22% | -51.53% | 1.335 | 1.421 | Pass |
| stop_1p8n | 334788.43 | 49.90% | -46.48% | 1.106 | 1.074 | Fail / warning |
| stop_2p2n | 1153419.43 | 72.88% | -46.30% | 1.311 | 1.574 | Pass |

The strategy is not a single-parameter spike for entry/exit windows, but it is sensitive to tighter stops. `1.8N` materially degrades the result.

## Benchmark Comparison

| Benchmark | Final equity | CAGR | Max DD | Sharpe-like | MAR | Trades |
|---|---:|---:|---:|---:|---:|---:|
| core4 | 339572.50 | 50.14% | -68.74% | 1.073 | 0.729 | 141 |
| base_9 | 1497084.87 | 78.15% | -50.14% | 1.383 | 1.559 | 217 |
| compact A-grade | 743881.50 | 64.35% | -49.21% | 1.277 | 1.308 | 220 |
| previous 20-symbol | 386067.67 | 52.38% | -51.05% | 1.104 | 1.026 | 408 |
| minus ETH short / revised 8 | 1625439.24 | 79.85% | -46.98% | 1.417 | 1.700 | 198 |

The revised 8-symbol set is the best tested version:

```text
XAU long only
XAG long only
BTC long/short
NVDA long only
AMD long only
MU long only
TSM long only
AVGO long only
```

## Pass / Warning Summary

| Audit area | Result |
|---|---|
| Single-asset robustness | Mixed. BTC is indispensable; XAU/NVDA/AMD matter; ETH short fails as a keeper |
| Cluster robustness | Semis and crypto are indispensable; metals boost return but concentrate risk |
| Time segmentation | Pass with warning. All segments profitable, but 2021-2026 rolling window is weak |
| Top-trade concentration | Warning. Top1 = 46.38%, top3 = 78.23% of net trade PnL |
| Cost stress | Pass |
| Cluster cap stress | Pass with warning. Tight caps still work, but metals cap heavily reduces final equity |
| Rule perturbation | Pass with warning. Entry/exit stable, stop tightening is fragile |
| Benchmark comparison | Pass. 9-symbol and revised 8-symbol versions beat 20-symbol reference |

## Final Decision

Do not promote the original 9-symbol candidate unchanged.

Promote a revised 8-symbol candidate:

```text
XAU long only
XAG long only
BTC long/short
NVDA long only
AMD long only
MU long only
TSM long only
AVGO long only
```

Rationale:

- Removing ETH short improves final equity, CAGR, drawdown, Sharpe-like, MAR, and reduces trades.
- The core advantage survives cost stress and most parameter perturbations.
- The 8/9-symbol pruned structure remains materially better than the 20-symbol reference.

But classify it as:

`strong candidate, not yet production baseline`

Reasons:

- top-trade concentration is high
- 2021-2026 rolling window is weak
- metals and BTC are still major drivers
- tighter metal cap sharply lowers final equity

## Recommended Next Test

The next test should not add more symbols. It should run the revised 8-symbol candidate against:

1. walk-forward yearly re-optimization avoided, fixed rules only
2. start-date sensitivity by year
3. lower per-symbol unit risk
4. metal leverage cap variants
5. top-trade clipped equity curves

The main question is now risk sizing, not symbol selection.
