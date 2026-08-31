"""Refresh configured D1 data and scan parallel trend-strategy checks."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys
import webbrowser
from typing import Any, Mapping, Sequence

from rich.console import Console
from rich.table import Table

from inv_trend.application import DEFAULT_BOOTSTRAP_DAYS
from inv_trend.application.daily.legacy_runtime_adapter import (
    DeferredStateDailyRuntimeAdapter,
    PreCommitDailyRuntimeAdapter,
)
from inv_trend.application.daily.workflow import DailyWorkflow
from inv_trend.application.daily.workspace import DailyStagingWorkspace
from inv_trend.adapters.daily.stage_runtime_adapters import (
    CommitStageRuntimeAdapter,
    DeliveryStageRuntimeAdapter,
    PublishStageRuntimeAdapter,
)
from inv_trend.core.signals import CORRECTED_STRATEGY_VERSION


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
    parser.add_argument(
        "--with-backtest", action="store_true",
        help="After publish/delivery, append an independent strategy-backtest batch",
    )
    parser.add_argument("--backtest-plan", help="YAML plan required by --with-backtest")
    parser.add_argument(
        "--backtest-output-dir", default="outputs/strategy_backtest",
        help="Independent backtest artifact root",
    )
    parser.epilog = (
        "推荐的分阶段入口：turtle-daily run | data-update | strategy-screen | "
        "trend-decide | commit | publish | deliver。保留无子命令调用以兼容现有任务计划。"
    )
    return parser


_STAGE_COMMANDS = frozenset(
    {"run", "data-update", "strategy-screen", "trend-decide", "commit", "publish", "deliver"}
)
_PRE_COMMIT_STAGE_COMMANDS = frozenset({"data-update", "strategy-screen", "trend-decide"})
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the new stage commands while preserving the legacy invocation."""

    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "backtest":
        return _daily_backtest_main(values[1:])
    if values and values[0] in _STAGE_COMMANDS:
        return _stage_main(values)
    # Existing scheduled-task and script invocations begin with an option (or
    # nothing).  They now mean the explicit ``run`` stage while retaining the
    # exact public option surface and exit-code semantics.
    return _run_staged_main(values)


def _stage_main(argv: Sequence[str]) -> int:
    command, *values = argv
    if command == "run":
        return _run_staged_main(values)
    parser = _stage_parser(command)
    args = parser.parse_args(values)
    if args.provider_timeout < 1:
        parser.error("--provider-timeout must be positive")
    if args.provider_retries < 1:
        parser.error("--provider-retries must be positive")
    if command == "data-update":
        if args.bootstrap_days < 1:
            parser.error("--bootstrap-days must be positive")
        if args.chart_bars < 1:
            parser.error("--chart-bars must be positive")
    workflow = _stage_workflow(
        command,
        data_root=args.data_root,
        output_dir=args.output_dir,
        database_path=args.database,
        signal_log_dir=args.signal_log_dir,
        provider_timeout=args.provider_timeout,
        provider_retries=args.provider_retries,
        refresh_data=not getattr(args, "scan_only", False),
    )
    try:
        if command == "data-update":
            result = workflow.data_update(
                run_id=args.run_id,
                report_date=args.report_date,
                symbols=args.symbol,
                bootstrap_days=args.bootstrap_days,
                research_mode=args.research_mode,
                strategy_version=args.strategy_version,
                backfill_signals=args.backfill_signals,
                chart_bars=args.chart_bars,
            )
        elif command == "strategy-screen":
            result = workflow.strategy_screen(run_id=args.run_id, report_date=args.report_date)
        elif command == "trend-decide":
            result = workflow.trend_decide(run_id=args.run_id, report_date=args.report_date)
        elif command == "commit":
            result = workflow.commit(run_id=args.run_id, report_date=args.report_date)
        elif command == "publish":
            publication = workflow.publish(
                run_id=args.run_id, report_date=args.report_date, render_html=not args.no_html
            )
            result = {
                "stage": "publish",
                "run_id": args.run_id,
                "report_date": args.report_date,
                "run_directory": str(publication.run_directory),
                "compatibility_json": str(publication.compatibility_json),
                "compatibility_html": (
                    None if publication.compatibility_html is None else str(publication.compatibility_html)
                ),
            }
        else:  # deliver
            result = workflow.deliver(run_id=args.run_id, report_date=args.report_date)
    except KeyboardInterrupt:
        print("Daily scan interrupted by user (Ctrl+C).", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"daily stage {command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    gate_failures = _stage_gate_failures(command, args, result)
    if gate_failures and isinstance(result, Mapping):
        result = {**result, "gate_failures": gate_failures}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    summary = result.get("summary", {}) if isinstance(result, Mapping) else {}
    if isinstance(summary, Mapping) and summary.get("error_count", 0):
        return 1
    return 1 if gate_failures else 0


def _run_staged_main(argv: Sequence[str]) -> int:
    """Run the new stage chain with the complete historical daily argument set."""

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
    if args.with_backtest and not args.backtest_plan:
        parser.error("--with-backtest requires --backtest-plan")
    workflow = DailyWorkflow(
        runtime=DeferredStateDailyRuntimeAdapter(
            data_root=args.data_root,
            output_dir=args.output_dir,
            database_path=args.database,
            signal_log_dir=args.signal_log_dir,
            provider_timeout=args.provider_timeout,
            provider_retries=args.provider_retries,
            refresh_data=not args.scan_only,
        )
    )
    try:
        result = workflow.run(
            symbols=args.symbol,
            bootstrap_days=args.bootstrap_days,
            research_mode=args.research_mode,
            strategy_version=args.strategy_version,
            backfill_signals=args.backfill_signals,
            chart_bars=args.chart_bars,
            render_html=not args.no_html,
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
    html_path = getattr(
        getattr(result, "artifact_publication", None), "compatibility_html", None
    )
    if not args.no_html and html_path is not None:
        console.print(f"HTML report: {html_path}")
        if args.open_report:
            webbrowser.open(html_path.resolve().as_uri())
    if result.failed_symbols:
        console.print(f"Failed/blocked/stale: {', '.join(result.failed_symbols)}", style="red")
    backtest_failed = False
    if args.with_backtest:
        try:
            from inv_trend.cli.backtest import load_plan, run_backtest_stage

            publication = getattr(result, "artifact_publication", None)
            source_run = getattr(publication, "run_directory", None)
            if source_run is None:
                raise RuntimeError("daily publication did not expose an immutable run directory")
            backtest = run_backtest_stage(
                source_run=source_run,
                plan=load_plan(args.backtest_plan),
                data_root=args.data_root,
                output_dir=args.backtest_output_dir,
            )
            console.print(f"Backtest report: {backtest['artifacts']['report']}")
        except Exception as exc:
            backtest_failed = True
            console.print(
                f"Backtest sidecar failed after daily delivery: {type(exc).__name__}: {exc}",
                style="red",
            )
    return 1 if backtest_failed else result.exit_code


def _daily_backtest_main(argv: Sequence[str]) -> int:
    """Expose the independent backtest stage under the daily command family."""

    parser = argparse.ArgumentParser(
        prog="turtle-daily backtest",
        description="Run a read-only strategy-backtest from one immutable daily batch.",
    )
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--output-dir", default="outputs/strategy_backtest")
    parser.add_argument("--run-id", type=_run_id_argument)
    args = parser.parse_args(argv)
    try:
        from inv_trend.cli.backtest import load_plan, run_backtest_stage

        result = run_backtest_stage(
            source_run=args.source_run,
            plan=load_plan(args.plan),
            data_root=args.data_root,
            output_dir=args.output_dir,
            run_id=args.run_id,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"daily backtest stage failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def _stage_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"turtle-daily {command}",
        description="Execute one auditable D1 daily-workflow stage.",
    )
    parser.add_argument("--output-dir", default="outputs/daily_market_scan")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--database", help="SQLite SignalStore path")
    parser.add_argument("--signal-log-dir", default="logs/signals")
    parser.add_argument("--provider-timeout", type=int, default=10)
    parser.add_argument("--provider-retries", type=int, default=1)
    parser.add_argument("--run-id", required=True, type=_run_id_argument)
    parser.add_argument(
        "--report-date",
        required=True,
        type=_report_date_argument,
        help="YYYY-MM-DD artifact date",
    )
    if command == "data-update":
        parser.add_argument("--symbol", action="append", help="Configured D1 symbol; repeat as needed")
        parser.add_argument("--bootstrap-days", type=int, default=DEFAULT_BOOTSTRAP_DAYS)
        parser.add_argument("--research-mode", action="store_true")
        parser.add_argument("--strategy-version", choices=(CORRECTED_STRATEGY_VERSION,), default=CORRECTED_STRATEGY_VERSION)
        parser.add_argument("--backfill-signals", action="store_true")
        parser.add_argument("--chart-bars", type=int, default=180)
        parser.add_argument("--scan-only", action="store_true")
    elif command == "publish":
        parser.add_argument("--no-html", action="store_true")
    return parser


def _stage_workflow(command: str, **kwargs: Any) -> DailyWorkflow:
    """Build a stage-specific runtime without changing the public CLI flags.

    Evidence stages can safely read an already committed cursor/anchor, but
    they must not create or migrate the SQLite store.  ``run`` deliberately
    keeps the historical construction path.  Resumed commit, publication,
    and delivery stages get narrow adapters that never reload mutable assets,
    strategy YAML, or market-data services.
    """

    if command in _PRE_COMMIT_STAGE_COMMANDS:
        return DailyWorkflow(runtime=PreCommitDailyRuntimeAdapter(**kwargs))
    if command == "commit":
        return DailyWorkflow(runtime=CommitStageRuntimeAdapter(**kwargs))
    if command == "publish":
        return DailyWorkflow(runtime=PublishStageRuntimeAdapter(**kwargs))
    if command == "deliver":
        return DailyWorkflow(runtime=DeliveryStageRuntimeAdapter(**kwargs))
    return DailyWorkflow(**kwargs)


def _run_id_argument(value: str) -> str:
    """Accept one portable, non-traversing artifact directory segment."""

    if not _SAFE_RUN_ID.fullmatch(value) or value in {".", ".."}:
        raise argparse.ArgumentTypeError(
            "must be a non-empty ASCII path segment containing only A-Z, a-z, 0-9, ., _, or -"
        )
    return value


def _report_date_argument(value: str) -> str:
    """Accept only canonical ISO calendar dates at the CLI boundary."""

    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD")
    return value


def _stage_gate_failures(
    command: str,
    args: argparse.Namespace,
    result: Any,
) -> list[dict[str, Any]]:
    """Return formal-stage failures that must produce a non-zero status.

    A normal trend ``WAIT`` (for example, no fresh Turtle breakout) is a
    valid decision and must not look like a failed process.  In contrast,
    formal data-readiness and unavailable-screening gates stop the official
    chain and need an explicit non-zero result.  Research observations remain
    successful because their staged payloads carry ``observation_only``.
    """

    if command == "deliver":
        delivery = _mapping(result).get("delivery")
        errors = _as_nonnegative_int(_mapping(delivery).get("errors"))
        return [] if errors == 0 else [{"kind": "delivery", "errors": errors}]
    if command not in _PRE_COMMIT_STAGE_COMMANDS:
        return []

    try:
        workspace = DailyStagingWorkspace(Path(args.output_dir), args.report_date, args.run_id)
        context = workspace.read_context()
        symbols = context.get("symbols")
        if not isinstance(symbols, list):
            return []
        failures: list[dict[str, Any]] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol)
            data = workspace.read_stage(symbol, "data_update")
            formal = not bool(data.get("observation_only", False))
            data_ready = (
                str(data.get("data_readiness") or "").upper() == "READY"
                and not list(data.get("blocking_reasons") or ())
            )
            if formal and not data_ready:
                failures.append(
                    {
                        "symbol": symbol,
                        "kind": "data_readiness",
                        "reasons": [str(item) for item in data.get("blocking_reasons", ())],
                    }
                )
                # Strategy-screen and trend-decide are expected to preserve
                # this same hard gate; one concise reason is sufficient.
                continue
            if command == "data-update":
                continue
            screening = workspace.read_stage(symbol, "strategy_screening")
            if (
                not bool(screening.get("observation_only", False))
                and str(screening.get("screening_status") or "") != "READY"
            ):
                failures.append(
                    {
                        "symbol": symbol,
                        "kind": "strategy_screening",
                        "reason": screening.get("reason") or screening.get("screening_status"),
                    }
                )
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        # A workflow implementation supplied by an embedding/test may return
        # a summary without materializing a staging workspace.  Its own exit
        # contract remains authoritative in that compatibility case.
        return []
    return failures


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _render(console: Console, snapshot: Mapping[str, Any]) -> None:
    table = Table(title=f"Daily D1 scan {snapshot['report_date']}")
    for heading in ("Symbol", "Run", "Latest", "Version", "Turtle 20/55", "SMA10/20", "MACD", "Grade", "Signals"):
        table.add_column(heading)
    for row in snapshot["symbols"]:
        scan = row.get("scan", {})
        indicators = scan.get("indicators", {}) if isinstance(scan, Mapping) else {}
        t20, t55 = indicators.get("turtle_20", {}), indicators.get("turtle_55", {})
        sma, macd = indicators.get("sma_10_20", {}), indicators.get("macd_12_26_9", {})
        rating = scan.get("strategy_checks", {}).get("rating", {})
        table.add_row(
            str(row["symbol"]),
            str(row.get("run_status", "failed")),
            str(row.get("data", {}).get("latest_complete_bar", "-"))[:10],
            str(row.get("data", {}).get("dataset_version", "-"))[:12],
            f"{t20.get('event') or '-'} / {t55.get('event') or '-'}",
            f"{_number(sma.get('sma_10'))}/{_number(sma.get('sma_20'))} {sma.get('event') or '-'}",
            f"{_number(macd.get('dif'))}/{_number(macd.get('dea'))} {macd.get('event') or '-'}",
            f"{rating.get('grade') or '-'} {rating.get('direction') or '-'} ({_number(rating.get('score'))})",
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
        rating = scan.get("strategy_checks", {}).get("rating", {})
        print(
            f"{row['symbol']} status={row.get('run_status', 'failed')} "
            f"latest={row.get('data', {}).get('latest_complete_bar', '-')} "
            f"version={row.get('data', {}).get('dataset_version', '-')} "
            f"turtle20={t20.get('event') or '-'} turtle55={t55.get('event') or '-'} "
            f"sma10={_number(sma.get('sma_10'))} sma20={_number(sma.get('sma_20'))} "
            f"ma_cross={sma.get('event') or '-'} dif={_number(macd.get('dif'))} "
            f"dea={_number(macd.get('dea'))} macd_cross={macd.get('event') or '-'} "
            f"grade={rating.get('grade') or '-'} rating_direction={rating.get('direction') or '-'} "
            f"rating_score={_number(rating.get('score'))} "
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
