# D1 Pruned Universe 3x Backtest Report

Date: 2026-04-21

## Purpose

This run applies the requested reduction logic:

- keep the main risk engines
- remove weak / redundant names
- compare category-level additions instead of judging by individual ticker names only

All tests use the same D1 Turtle setting as the previous equity-overlay test:

| Setting | Value |
|---|---:|
| Entry | 20D / 55D breakout |
| Exit | 10D / 20D reverse breakout |
| N | 20D |
| Stop | 2N |
| Add step | 0.5N |
| Total leverage cap | 3.0x |
| Direction leverage cap | 2.0x |
| Metals | long only |
| Equities | long only |
| BTC | long / short |

Sample for all model-selection runs:

`2017-08-17 -> 2026-04-20`

This is the common sample after BTC/ETH become available and excludes the short-history `PLTR` / `SNDK` issue.

## Tested Universes

| Run | Symbols | Count |
|---|---|---:|
| `core4_macro_crypto` | XAU, XAG, BTC, ETH | 4 |
| `core3_no_eth` | XAU, XAG, BTC | 3 |
| `core4_eth_short_only` | XAU, XAG, BTC, ETH short-only | 4 |
| `core4_plus_semis` | Core 4 + NVDA, AMD, MU, TSM, AVGO | 9 |
| `core4_plus_semis_eth_short_only` | Core 4 + semis, ETH short-only | 9 |
| `core4_plus_semis_platform` | Core 4 + semis + MSFT, META, GOOGL, AMZN | 13 |
| `pruned_final_eth_short_only` | Same as above, ETH short-only | 13 |
| `compact_a_grade` | XAU, XAG, BTC, NVDA, AMD, MU, MSFT, META | 8 |
| `add_consumer_etf_weak_bucket` | Group 3 + SPY, QQQ, XLY, AAPL, TSLA, NFLX, ORCL | 20 |
| `previous_full20_no_pltr_sndk` | Previous 20-symbol reference without PLTR/SNDK | 20 |

## Main Result Table

| Run | Symbols | Final equity | CAGR | Max DD | Vol | Sharpe-like | MAR | Trades |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Core 4 macro/crypto | 4 | 339572.50 | 50.14% | -68.74% | 49.05% | 1.073 | 0.729 | 141 |
| Core 3 no ETH | 3 | 289903.74 | 47.43% | -51.92% | 46.17% | 1.071 | 0.913 | 95 |
| Core 4 ETH short-only | 4 | 286418.37 | 47.23% | -57.00% | 47.28% | 1.054 | 0.828 | 113 |
| Core 4 + semis | 9 | 1330074.29 | 75.74% | -58.50% | 57.10% | 1.277 | 1.295 | 241 |
| Core 4 + semis, ETH short-only | 9 | 1497084.87 | 78.15% | -50.14% | 51.38% | 1.383 | 1.559 | 217 |
| Core 4 + semis + platform | 13 | 779748.44 | 65.25% | -50.05% | 51.37% | 1.238 | 1.304 | 314 |
| Pruned 13, ETH short-only | 13 | 750056.43 | 64.51% | -49.98% | 48.48% | 1.272 | 1.291 | 301 |
| Compact A-grade | 8 | 743881.50 | 64.35% | -49.21% | 48.06% | 1.277 | 1.308 | 220 |
| Add weak / ETF / consumer bucket | 20 | 386067.67 | 52.38% | -51.05% | 49.41% | 1.104 | 1.026 | 408 |
| Previous 20-symbol reference | 20 | 386067.67 | 52.38% | -51.05% | 49.41% | 1.104 | 1.026 | 408 |

## Key Finding

The best tested portfolio is:

`core4_plus_semis_eth_short_only`

Symbols:

```text
XAUUSD_DUKAS
XAGUSD_DUKAS
BTCUSDT_BINANCE
ETHUSDT_BINANCE short-only
NVDA
AMD
MU
TSM
AVGO
```

Compared with the previous 20-symbol common reference:

| Metric | Previous 20-symbol | Best pruned 9-symbol | Change |
|---|---:|---:|---:|
| Final equity | 386067.67 | 1497084.87 | +287.78% |
| CAGR | 52.38% | 78.15% | +25.77 pp |
| Max DD | -51.05% | -50.14% | +0.91 pp better |
| Volatility | 49.41% | 51.38% | +1.97 pp |
| Sharpe-like | 1.104 | 1.383 | +0.280 |
| MAR | 1.026 | 1.559 | +0.532 |
| Trades | 408 | 217 | -191 |

So the reduction improved both return and risk-adjusted return while cutting the number of trades almost in half.

## Why This Works

The previous 20-symbol set was not truly 20 independent assets. It was a concentrated bet on:

- metals trend
- crypto trend
- AI / semiconductor cycle
- US growth / platform tech
- overlapping ETF beta
- consumer-growth beta

The pruned 9-symbol set keeps the strongest engines and removes the weaker / duplicative sleeves.

## Best 9-Symbol Contribution

Run: `core4_plus_semis_eth_short_only`

| Symbol / side | Trades | Net PnL |
|---|---:|---:|
| XAG long | 23 | 631491.25 |
| BTC long | 27 | 315592.37 |
| MU long | 17 | 205528.61 |
| AVGO long | 29 | 86220.63 |
| NVDA long | 27 | 71197.51 |
| TSM long | 19 | 67322.61 |
| XAU long | 15 | 56648.86 |
| AMD long | 22 | 34501.30 |
| ETH short | 18 | 12211.95 |
| BTC short | 20 | 6369.79 |

This is much cleaner than the 20-symbol set:

- no ETF overlap
- no `NFLX / ORCL / XLY / SPY` drag
- no short-history `PLTR / SNDK`
- no ETH long drag
- main winners are the intended risk engines

## ETH Result

ETH is not useless, but ETH long is the problematic leg.

| Run | Final equity | Max DD | MAR |
|---|---:|---:|---:|
| Core 4, ETH long/short | 339572.50 | -68.74% | 0.729 |
| Core 3, no ETH | 289903.74 | -51.92% | 0.913 |
| Core 4, ETH short-only | 286418.37 | -57.00% | 0.828 |
| Core 4 + semis | 1330074.29 | -58.50% | 1.295 |
| Core 4 + semis, ETH short-only | 1497084.87 | -50.14% | 1.559 |

Conclusion:

- In core-only, ETH does not clearly improve risk-adjusted performance.
- With the semiconductor sleeve included, removing ETH long and keeping ETH short-only improves the result materially.

## Platform Stock Result

Adding `MSFT / META / GOOGL / AMZN` did not improve the portfolio under this 3x risk budget.

| Run | Final equity | CAGR | Max DD | MAR |
|---|---:|---:|---:|---:|
| Core 4 + semis, ETH short-only | 1497084.87 | 78.15% | -50.14% | 1.559 |
| Pruned 13, ETH short-only | 750056.43 | 64.51% | -49.98% | 1.291 |
| Compact A-grade | 743881.50 | 64.35% | -49.21% | 1.308 |

This does not mean `MSFT / META / GOOGL / AMZN` are bad assets. It means that under the current Turtle allocation and leverage budget, they dilute the stronger semis/metals/crypto trades.

## Weak Bucket Result

Adding the ETF / consumer / weak names back in collapses the result toward the old 20-symbol portfolio:

| Run | Final equity | CAGR | Max DD | Sharpe-like | MAR | Trades |
|---|---:|---:|---:|---:|---:|---:|
| Core 4 + semis, ETH short-only | 1497084.87 | 78.15% | -50.14% | 1.383 | 1.559 | 217 |
| Add weak / ETF / consumer bucket | 386067.67 | 52.38% | -51.05% | 1.104 | 1.026 | 408 |

The weak bucket adds `191` extra trades and sharply reduces final equity and MAR.

## Exit Breakdown

Best 9-symbol run:

| Exit type | Trades | Net PnL | Avg PnL | Win rate |
|---|---:|---:|---:|---:|
| trend_exit | 63 | 1940636.05 | 30803.75 | 82.54% |
| stop | 150 | -465556.16 | -3103.71 | 5.33% |
| end_of_test | 4 | 12004.98 | 3001.25 | 50.00% |

Previous 20-symbol run:

| Exit type | Trades | Net PnL | Avg PnL | Win rate |
|---|---:|---:|---:|---:|
| trend_exit | 129 | 545434.13 | 4228.17 | 71.32% |
| stop | 271 | -168752.26 | -622.70 | 11.44% |
| end_of_test | 8 | -614.20 | -76.77 | 50.00% |

The pruned version has fewer but much larger trend winners. That is exactly what this Turtle style wants.

## Final Keep / Remove List

Recommended main universe:

| Action | Symbols |
|---|---|
| Keep | XAU, XAG |
| Keep | BTC |
| Keep short-only | ETH |
| Keep | NVDA, AMD, MU, TSM, AVGO |

Remove from main model-selection universe:

| Reason | Symbols |
|---|---|
| Weak / redundant ETF beta | SPY, QQQ, XLY |
| Weak or noisy in this model | NFLX, ORCL |
| Consumer-growth bucket did not help | AAPL, TSLA |
| Platform bucket diluted stronger trades | MSFT, META, GOOGL, AMZN |
| Short-history, not fair sample | PLTR, SNDK |

Alternative conservative universe:

```text
XAU / XAG / BTC / NVDA / AMD / MU / MSFT / META
```

This `compact_a_grade` set has lower final equity than the best 9-symbol set, but slightly shallower drawdown:

| Run | Final equity | Max DD | MAR |
|---|---:|---:|---:|
| Best 9-symbol | 1497084.87 | -50.14% | 1.559 |
| Compact A-grade | 743881.50 | -49.21% | 1.308 |

## Conclusion

The reduction worked.

The best current D1 3x candidate is:

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

This version beats the previous 20-symbol reference by a large margin:

- higher final equity
- higher CAGR
- slightly better max drawdown
- better Sharpe-like
- better MAR
- fewer trades

The main lesson is clear: the portfolio does not need more stock names. It needs fewer overlapping growth/ETF exposures and more capacity for the actual winning sleeves: metals, BTC trend, and semiconductors.
