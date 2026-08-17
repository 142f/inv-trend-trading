# Local market-data lake

This directory is generated locally and is intentionally excluded from version control.

- `raw/` is immutable provider output.
- `normalized/` is canonical Parquet with audit fields.
- `curated/` contains immutable, quality-gated versions selected by a current pointer.
- `LEGACY_ONLY` and `RESEARCH_ONLY` data require an explicit Repository opt-in.

Use `market-data status`, `coverage`, `versions`, and `rollback` to inspect or manage published data. Do not manually edit Parquet files.

`DUKASCOPY_API_KEY` is optional in configuration but required by Dukascopy deployments that
enforce developer-key access. `ALPHAVANTAGE_API_KEY` enables the Yahoo fallback for US data.
