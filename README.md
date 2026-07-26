# inv-trend-trading Workspace

This repository is organized around three working areas:

```text
outputs/         Human-facing run outputs and final reports.
processed_data/  Generated data pipeline artifacts for audit and backtests.
research/        Experiment scripts, notebooks, and research-specific outputs.
```

## Reading Order

1. Start with `outputs/` when you want the latest actionable result.
2. Open `processed_data/metadata/` when you need to understand what data was used.
3. Use `processed_data/logs/` to inspect data quality and validation decisions.
4. Use `processed_data/backtest_ready/` as the stable input layer for backtests.
5. Use `research/` only when you need to reproduce or extend experiments.

## Directory Roles

| Directory | Role | Keep manually edited files here? |
|---|---|---|
| `outputs/` | Final reports, alerts, and exported analysis results | No |
| `processed_data/raw_index/` | Catalog of discovered raw data sources | No |
| `processed_data/cleaned/` | Per-symbol cleaned OHLCV files | No |
| `processed_data/merged/` | Multi-asset merged datasets | No |
| `processed_data/backtest_ready/` | Backtest input datasets | No |
| `processed_data/logs/` | Validation and alignment logs | No |
| `processed_data/metadata/` | Manifests, selected symbols, field dictionaries | No |
| `research/` | Research scripts and experiment suites | Yes, for scripts only |
| `turtle_multi_asset/` | Reusable package code | Yes |
| `tests/` | Regression and behavior tests | Yes |

## Naming Convention

Generated data files follow this pattern:

```text
<dataset>_<source>_<symbols>_<start>_<end>_<timeframe>_<stage>.csv
```

Examples:

```text
data_2010_xau_btc_mt5_btcusdc_xauusdc_2018_2026_h4_backtest_ready.csv
metal_tech_core_d1_backtest_ready.csv
```

## Cleanup Policy

Do not keep duplicate copies of the same generated file unless they represent
different pipeline stages. Prefer one source copy plus metadata references.

Safe to remove when duplicated:

- Old generated datasets with identical hashes.
- Per-symbol copies inside a derived dataset when the same file already exists in its source dataset and metadata can point to the source.
- Empty generated directories.

Keep even if the contents currently match:

- `merged/` and `backtest_ready/` files, because they are separate pipeline stages.
- Shared assets used by different datasets, because each dataset records a different universe definition.