"""Refresh configured D1 data and scan Turtle, SMA, and MACD signals."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from typing import Any, Mapping, Sequence

from rich.console import Console
from rich.table import Table

from inv_trend_application import DEFAULT_BOOTSTRAP_DAYS, DailyMarketScanService
from inv_trend_observability import write_daily_report
from inv_trend_core.signals import CORRECTED_STRATEGY_VERSION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", help="Configured D1 symbol; repeat as needed")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--output-dir", default="outputs/daily_market_scan")
    parser.add_argument("--database", help="SQLite SignalStore path")
    parser.add_argument("--signal-log-dir", default="logs/signals")
    parser.add_argument("--bootstrap-days", type=int, default=DEFAULT_BOOTSTRAP_DAYS)
    parser.add_argument(
        "--provider-timeout", type=int, default=10,
        help="Network timeout in seconds for each provider HTTP attempt (default: 10)",
    )
    parser.add_argument(
        "--provider-retries", type=int, default=1,
        help="Provider/data-service attempts per request (default: 1)",
    )
    parser.add_argument(
        "--scan-only", action="store_true",
        help="Do not contact providers; scan the current local dataset after freshness checks",
    )
    parser.add_argument(
        "--research-mode",
        action="store_true",
        help="Include non-CURATED detected events in the snapshot only; never formally alert them",
    )
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--strategy-version", choices=(CORRECTED_STRATEGY_VERSION,),
        default=CORRECTED_STRATEGY_VERSION,
    )
    parser.add_argument(
        "--backfill-signals", action="store_true",
        help="Research replay of all complete bars; newly found events are not notified",
    )
    parser.add_argument("--chart-bars", type=int, default=180)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument(
        "--open-report", action="store_true",
        help="Open the generated local HTML report after completion",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bootstrap_days < 1:
        parser.error("--bootstrap-days must be positive")
    if args.provider_timeout < 1:
        parser.error("--provider-timeout must be positive")
    if args.provider_retries < 1:
        parser.error("--provider-retries must be positive")
    if args.chart_bars < 1:
        parser.error("--chart-bars must be positive")
    if args.open_report and args.no_html:
        parser.error("--open-report cannot be combined with --no-html")
    service = DailyMarketScanService(
        data_root=args.data_root,
        output_dir=args.output_dir,
        database_path=args.database,
        signal_log_dir=args.signal_log_dir,
        provider_timeout=args.provider_timeout,
        provider_retries=args.provider_retries,
        refresh_data=not args.scan_only,
    )
    try:
        result = service.run(
            symbols=args.symbol,
            bootstrap_days=args.bootstrap_days,
            research_mode=args.research_mode,
            strategy_version=args.strategy_version,
            backfill_signals=args.backfill_signals,
            chart_bars=args.chart_bars,
        )
    except KeyboardInterrupt:
        print("Daily scan interrupted by user (Ctrl+C).", file=sys.stderr)
        return 130
    console = Console(no_color=args.no_color or not sys.stdout.isatty())
    if args.no_color:
        _render_plain(result.snapshot)
    else:
        _render(console, result.snapshot)
    console.print(f"Snapshot: {result.snapshot_path}")
    console.print(f"SignalStore: {result.database_path}")
    console.print(f"Signal logs: {result.signal_log_root}")
    html_path = None
    if not args.no_html:
        html_path = write_daily_report(
            result.snapshot, result.snapshot_path.with_suffix(".html")
        )
        console.print(f"HTML report: {html_path}")
        if args.open_report:
            webbrowser.open(html_path.resolve().as_uri())
    if result.failed_symbols:
        console.print(f"Failed/blocked/stale: {', '.join(result.failed_symbols)}", style="red")
    return result.exit_code


def _render(console: Console, snapshot: Mapping[str, Any]) -> None:
    table = Table(title=f"Daily D1 scan {snapshot['report_date']}")
    for heading in ("Symbol", "Run", "Latest", "Version", "Turtle 20/55", "SMA10/20", "MACD", "Signals"):
        table.add_column(heading)
    for row in snapshot["symbols"]:
        scan = row.get("scan", {})
        indicators = scan.get("indicators", {}) if isinstance(scan, Mapping) else {}
        t20, t55 = indicators.get("turtle_20", {}), indicators.get("turtle_55", {})
        sma, macd = indicators.get("sma_10_20", {}), indicators.get("macd_12_26_9", {})
        table.add_row(
            str(row["symbol"]),
            str(row.get("run_status", "failed")),
            str(row.get("data", {}).get("latest_complete_bar", "-"))[:10],
            str(row.get("data", {}).get("dataset_version", "-"))[:12],
            f"{t20.get('event') or '-'} / {t55.get('event') or '-'}",
            f"{_number(sma.get('sma_10'))}/{_number(sma.get('sma_20'))} {sma.get('event') or '-'}",
            f"{_number(macd.get('dif'))}/{_number(macd.get('dea'))} {macd.get('event') or '-'}",
            f"{row.get('signals_new', 0)} new",
        )
    console.print(table)
    summary = snapshot["summary"]
    console.print(
        "Summary: " + " ".join(f"{key}={value}" for key, value in summary.items())
    )


def _render_plain(snapshot: Mapping[str, Any]) -> None:
    print(f"Daily D1 scan {snapshot['report_date']}")
    for row in snapshot["symbols"]:
        scan = row.get("scan", {})
        indicators = scan.get("indicators", {}) if isinstance(scan, Mapping) else {}
        t20, t55 = indicators.get("turtle_20", {}), indicators.get("turtle_55", {})
        sma, macd = indicators.get("sma_10_20", {}), indicators.get("macd_12_26_9", {})
        print(
            f"{row['symbol']} status={row.get('run_status', 'failed')} "
            f"latest={row.get('data', {}).get('latest_complete_bar', '-')} "
            f"version={row.get('data', {}).get('dataset_version', '-')} "
            f"turtle20={t20.get('event') or '-'} turtle55={t55.get('event') or '-'} "
            f"sma10={_number(sma.get('sma_10'))} sma20={_number(sma.get('sma_20'))} "
            f"ma_cross={sma.get('event') or '-'} dif={_number(macd.get('dif'))} "
            f"dea={_number(macd.get('dea'))} macd_cross={macd.get('event') or '-'} "
            f"new_signals={row.get('signals_new', 0)}"
        )
    print("Summary: " + " ".join(f"{key}={value}" for key, value in snapshot["summary"].items()))


def _number(value: object) -> str:
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return "-"


if __name__ == "__main__":
    raise SystemExit(main())
