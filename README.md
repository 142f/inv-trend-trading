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

## Turtle D1 Alerts

Import authorized QQQ and SPY holdings snapshots, then synchronize their merged top-50 universe:

```powershell
market-data holdings --fund QQQ --source C:\licensed\qqq.csv --snapshot-date 2026-08-07 --top 50
market-data holdings --fund SPY --source C:\licensed\spy.csv --snapshot-date 2026-08-07 --top 50
market-data sync-universe --universe qqq-spy-top50 --timeframe D1
```

Run the read-only alert scan:

```powershell
turtle-alert --data-root data --universe qqq-spy-top50
```

Continuously refresh through the existing Repository pipeline and scan every five minutes
(only completed D1 bars can emit formal alerts):

```powershell
turtle-alert --data-root data --universe qqq-spy-top50 --watch --refresh-before-scan --interval-seconds 300
```

The command prints a colored status table and appends daily text and JSONL reports under
`outputs/turtle_alerts/`. Formal deduplicated signals remain in `alerts.jsonl`.

## Daily Market Scan

完整的中文参数、命令、退出码和故障排查说明见
[`DAILY_MARKET_SCAN_CLI.md`](DAILY_MARKET_SCAN_CLI.md)。

Run the daily data refresh and technical-signal scan from the repository root:

```powershell
.\scripts\run_daily_market_scan.ps1
```

The launcher finds the repository root itself, uses `.venv\Scripts\python.exe` when it
exists, and otherwise uses `python` available on `PATH`. Any command-line options are
passed through to the scanner, for example:

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --no-color
```

The equivalent installed entry point is `turtle-daily`. Signals are calculated from
complete-only bars. Formal `CURATED` events are transactionally deduplicated in
`outputs\daily_market_scan\signals.sqlite3` and then appended to
`logs\signals\YYYY-MM-DD.jsonl`; `RESEARCH_ONLY` and `LEGACY_ONLY` results remain visible
in the daily snapshot but never produce formal alerts. A strict calendar-aware freshness
gate requires yesterday's UTC bar for crypto, the latest completed weekday for 24x5
markets, and the latest completed NYSE session for US equities.

Useful options include `--symbol BTC`, `--database <path>`, `--bootstrap-days 400`, and
`--research-mode`. The command returns a non-zero exit code when any instrument is failed,
blocked, or stale, while still completing the remaining instruments and recording the run.

### Schedule at 09:00 Beijing time (Windows)

In an elevated PowerShell window, from the repository root, register the task below.
Windows Task Scheduler evaluates `09:00` in the computer's local time zone; ensure the
machine time zone is set to China Standard Time for a Beijing-time schedule.

```powershell
$repo = (Resolve-Path .).Path
$script = Join-Path $repo 'scripts\run_daily_market_scan.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script)
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'TurtleDailyMarketScan' -Action $action -Trigger $trigger -Settings $settings -Description 'Refresh D1 market data and scan Turtle, SMA, and MACD signals.' -Force
```

`-MultipleInstances IgnoreNew` is intentional: if a prior scan is still running, the
scheduled start is skipped rather than opening a second concurrent scan. To remove the
task later, run `Unregister-ScheduledTask -TaskName 'TurtleDailyMarketScan' -Confirm:$false`.
