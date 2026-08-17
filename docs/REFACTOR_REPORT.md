# Refactor report

> **Namespace update (2026-08-18):** the implementation described below now
> lives under `src/inv_trend/`: `historical_data → data`,
> `inv_trend_core → core`, `inv_trend_application → application`,
> `inv_trend_observability → observability`, `inv_trend_integrations → integrations`,
> `turtle_detector → adapters/detector`, and
> `turtle_multi_asset → adapters/multi_asset`. The five console command names
> are unchanged; the former top-level Python import paths are intentionally
> unavailable. `StrategyRunManifest` belongs to `application` after this move.

> This is the implementation-era report. For the dated independent audit of
> the worktree, including remaining CLI bypasses and acceptance fixes, see
> [`REFACTOR_ACCEPTANCE_AUDIT.md`](REFACTOR_ACCEPTANCE_AUDIT.md).
>
> **Current verification status (2026-08-18):** test sources were recovered
> from the protected ZIP, a static BTC D1 fixture was re-certified against Git
> `121f901`, and the full current suite passed. The original Golden pair was
> not recoverable, so the new fixture is explicitly labelled *re-certified*
> rather than represented as a frozen pre-refactor asset.

## Result

The refactor preserves the five command names and their parameters:
`turtle-data`, `turtle-detect`, `turtle-alert`, `turtle-daily`, and
`market-data`. The tested executable strategy version is now exclusively
`corrected-v2`; `legacy-v1` is not a runtime option. Historical legacy data is
still an explicit `market-data migrate-legacy` archival/import path and is not
read by normal scans or backtests.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `historical_data` | Canonical instrument identity, source adaptation, cleaning, quality, calendar, lake and lineage. `config/assets.yaml` is authoritative. |
| `inv_trend_core` | I/O-free OHLCV features, signal identity, causal math and shared performance statistics. |
| `inv_trend_application` | Daily scan, detector and backtest orchestration; immutable resolved strategy config and run manifests. |
| `inv_trend_observability` | JSON/HTML reporting and audit/run manifests. |
| `inv_trend_integrations` | Optional isolated MT5 market and OKX execution ports; no CLI imports them. |
| `research` | Experiment definitions and result analysis; migrated entry points invoke `BacktestService`. |

## Consolidation and removal

Removed after repository-wide reference search and targeted regression tests:

- `turtle_detector/indicators/atr.py` and `donchian.py`; their only algorithms
  were replaced by `inv_trend_core.math_utils`.
- `turtle_detector/daily_models.py`; daily event identity now comes from
  `inv_trend_core.signals`.
- The default `turtle_detector/config/assets.yaml`; default identity comes from
  `historical_data/config/assets.yaml`. The existing `--assets-config` option
  accepts an explicit legacy-shaped file through the CLI boundary only.
- `turtle_detector/daily_scan.py`; the implementation now resides in
  `inv_trend_application.daily_pipeline`, and the CLI calls observability to
  render HTML after the use case returns a snapshot.
- `turtle_multi_asset/integrations`; MT5 and OKX implementations were moved to
  `inv_trend_integrations`.
- `tests/trend_stability_report.json`; test output now goes to pytest's
  isolated temporary directory.

Retained deliberately:

- `turtle_detector` and `turtle_multi_asset` remain the concrete Turtle state,
  execution and accounting adapters. Removing these package names would be a
  large unrelated public-API migration; their indicator and metric algorithms
  now delegate to core rather than duplicate it.
- `historical_data.legacy` remains only for the explicit migration command.

## Reused components

- `FeatureRequest`, `PreparedBars`, `FeatureCache`, and content fingerprints:
  ATR, prior-bar Donchian, SMA, EMA and MACD are computed under one causal
  contract and may not cross data fingerprints.
- Core Wilder recursion, true range, rolling percentile, Donchian, SMA/EMA,
  MACD and shared equity statistics.
- `BacktestService`, `DetectorService`, `DailyMarketScanService`, immutable
  `ResolvedRunConfig`, and `StrategyRunManifest`.
- Core `SignalEvent` identities and optional integration ports.

## Performance audit

Measured on Python 3.12.6, deterministic in-memory 720-bar D1 fixture, one
warm-up plus seven measured runs, median time and traced peak memory. The
"before" column is the equivalent pre-refactor local implementation.

| Path | Before | After | Time | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| Feature preparation | 0.06333 s / 219,658 B | 0.03066 s / 246,137 B | 2.07× faster | 0.89× (12% higher) |
| Detector replay | 0.87402 s / 1,148,639 B | 0.54673 s / 542,833 B | 1.60× faster | 2.12× lower |
| Multi-asset timeline | 0.13461 s / 913,539 B | 0.07872 s / 577,891 B | 1.71× faster | 1.58× lower |

Feature preparation uses a vectorized seeded Wilder recurrence for complete
market-quality series and preserves the former NaN-propagation fallback.
Detector replay prepares once and passes a single causal row to state updates.
The backtester now advances an event-driven as-of timeline rather than
prebuilding date-by-symbol search maps. The benchmark is reproducible with
`python scripts/benchmark_refactor.py`.

## Namespace migration validation (2026-08-18)

- Full test suite: `pytest -q -p no:cacheprovider --basetemp tf` → **239 passed**
  in 86.12 s. A short workspace temporary path is required on this Windows
  host because long Parquet temporary names exceed the traditional path limit.
- Golden Master: fixture `tests/fixtures/golden_d1.csv`, 420 BTC D1 bars,
  data version `028585a633484f05948afa5f`, LF encoding, SHA-256
  `bd19374be941eb5d356ebde2ac078ed6cd87120f33e04c7b078d2b54b8cab536`.
  Git `121f9016f8490baf42f5c2212aa46d97b9247290` matched common shared
  features, Detector events/trades/equity/metrics and multi-asset
  orders/trades/equity/metrics at `1e-10` relative tolerance. `generated_at`
  and random `intent_id` are excluded from identity; current-only daily SMA/
  MACD results are retained in the fixture but not attributed to the baseline.
- Packaging and static gates: editable install and a wheel containing all five
  YAML resources succeeded; five CLI `--help` commands succeeded; `ruff check .`,
  controlled `compileall`, and `git diff --check` succeeded.
- Recovery provenance is recorded in `tests/RECOVERY_MANIFEST.json`: 25 test
  source files came from ZIP SHA-256
  `554738bb5c8d81a21f7fdb466f6a9cb2ad90cb49fed4f980f7f294b2203f4f7a`.
- The restored fail-closed catalog test exposed a real gap: a catalog backend
  marker previously allowed a second catalog file to coexist. `DataLake` now
  rejects that conflict before opening either catalog.

Latest local benchmark (Python 3.12.6, 720 bars, one warm-up + seven medians):

| Path | Before | After | Time speedup | Peak-memory ratio |
| --- | ---: | ---: | ---: | ---: |
| Feature preparation | 0.06635 s | 0.03231 s | 2.05× | 0.89× |
| Detector replay | 1.30860 s | 0.82842 s | 1.58× | 2.12× lower |
| Multi-asset timeline | 0.20253 s | 0.11179 s | 1.81× | 1.58× lower |

## Historical regression and quality gates (2026-08-13; not current verification)

- `pytest -q --basetemp .tmp/pytest-final-refactor-20260813`: **238 passed** in
  61.13 s.
- Golden Master: **passed**. The fixed `golden_d1.csv` SHA-256 is checked,
  together with shared feature values, daily scan result, detector events,
  trades/equity, and multi-asset orders/trades/equity. Event/order structure is
  exact; numeric values allow relative error `1e-10`. Random internal
  reservation IDs are intentionally excluded from result identity.
- `ruff check .`: **passed**.
- `python -m compileall -q ...`: **passed**.
- `git diff --check`: **passed**.
- Help-level CLI smoke checks passed for all five preserved commands.

## Remaining exceptions

- No executable duplicate ATR/Donchian/Wilder implementation, direct research
  backtester construction, or old `turtle_multi_asset.integrations` import was
  found by the final reference search.
- No cycle was found by the architecture AST tests: core has no application,
  data, detector, or multi-asset dependency; historical data has no strategy or
  application dependency; application has no CLI/presentation dependency.
- The explicit `--assets-config` parser is a narrow legacy input converter,
  retained solely for parameter compatibility. It is not a second default
  configuration authority.
- `historical_data.legacy` is retained as a read-only/archive migration
  exception, as described above. No `legacy-v1` strategy implementation or
  runtime selector remains.
- Detector and portfolio backtest state machines remain separate adapters
  because their execution/accounting semantics intentionally differ. They now
  share prepared features, causal primitives and metrics, but a fully merged
  Turtle transition kernel is a remaining consolidation opportunity and is not
  claimed as complete here.
