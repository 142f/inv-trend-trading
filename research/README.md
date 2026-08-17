# research

Research scripts and experiment suites live here. This area is for exploration,
not for stable package APIs.

## Layout

```text
research/
├── d1_suite/            D1 multi-asset backtest and pruning experiments.
├── metal_tech_core/     Metal + technology core universe analysis.
├── strategy_variants/   Strategy comparison scripts.
└── xau_short_policy/    XAU short-policy experiments.
```

## Usage

Use scripts here to reproduce analysis or generate new outputs. Reusable logic
should be moved into `inv_trend.core` or `inv_trend.application` once it
stabilizes. `inv_trend.adapters.multi_asset` is a transitional compatibility
area, not the default destination for new reusable logic.
