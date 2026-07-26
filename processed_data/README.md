# processed_data

Generated data artifacts from the data pipeline. This directory is organized by
pipeline stage so that data can be audited before it is used in a backtest.

## Layout

```text
processed_data/
├── raw_index/       Source discovery catalog.
├── cleaned/         Per-symbol cleaned OHLCV files.
├── merged/          Multi-symbol merged datasets.
├── backtest_ready/  Stable datasets consumed by backtests.
├── logs/            Quality, validation, and alignment logs.
├── metadata/        Manifests, field dictionaries, and universe decisions.
└── us_trend_alerts/ Local cache used by alert generation.
```

## Recommended Reading Order

1. `metadata/*_manifest.csv` to see what exists.
2. `metadata/*_selected_sources.csv` to see what was selected.
3. `logs/*quality*` or `logs/*validation*` to audit data quality.
4. `backtest_ready/` for the files actually consumed by backtests.

## Duplication Rule

Keep one canonical copy per stage. If a derived dataset can reference an
existing source-cleaned file, prefer metadata references instead of copying the
same CSV into the derived dataset folder.
