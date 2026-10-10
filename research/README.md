# research

Research scripts and experiment suites live here. This area is for exploration,
not for stable package APIs.

## Layout

```text
research/
└── d1_suite/            D1 multi-asset backtest and pruning experiments.
```

## Usage

Use scripts here to reproduce analysis or generate new outputs. Reusable logic
should be moved into `inv_trend.core` or `inv_trend.application` once it
stabilizes. `inv_trend.adapters.multi_asset` is a transitional compatibility
area, not the default destination for new reusable logic.
