"""Command line entry points for standardized data builds."""

from __future__ import annotations

import argparse
from dataclasses import replace

from ..data.builder import DEFAULT_DATASET_DIRS, build_unified_processed_data
from ..data.core_dataset import build_metal_tech_core_dataset
from ..config import load_config
from ..us_trend_alerts import TrendAlertConfig, run_us_trend_alerts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build the unified processed_data directory.")
    build.add_argument("--output-dir", default="processed_data")
    build.add_argument("--dataset-dirs", nargs="+", default=DEFAULT_DATASET_DIRS)
    core = subparsers.add_parser("build-metal-tech-core", help="Build the metal + tech core dataset and backtest report.")
    core.add_argument("--processed-dir", default="processed_data")
    core.add_argument("--output-dir", default="processed_data")
    core.add_argument("--reports-dir", default="outputs")
    core.add_argument(
        "--config",
        default=None,
        help="Optional YAML backtest configuration (defaults to config/defaults.yaml).",
    )
    core.add_argument(
        "--strategy-version", choices=("corrected-v2",),
        default=None,
    )
    core.add_argument("--no-html", action="store_true")
    alerts = subparsers.add_parser("us-trend-alerts", help="Scan SPY/QQQ top holdings for 20/55 day breakouts.")
    alerts.add_argument("--etfs", nargs="+", default=["SPY", "QQQ"])
    alerts.add_argument("--top-n", type=int, default=100)
    alerts.add_argument("--lookback-days", type=int, default=180)
    alerts.add_argument("--cache-dir", default="processed_data/us_trend_alerts/cache")
    alerts.add_argument("--output-dir", default="outputs/us_trend_alerts")
    alerts.add_argument("--local-bars-dir", default=None)
    alerts.add_argument("--data-root", default="data")
    alerts.add_argument("--force-refresh", action="store_true")
    alerts.add_argument("--verbose", action="store_true")

    args = parser.parse_args()
    if args.command == "build":
        summary = build_unified_processed_data(
            dataset_dirs=args.dataset_dirs,
            output_dir=args.output_dir,
        )
        print("Unified data build summary")
        for key, value in summary.items():
            print(f"{key}: {value}")
    if args.command == "build-metal-tech-core":
        config = load_config(args.config)
        config = replace(
            config,
            strategy_version=args.strategy_version or config.strategy_version,
            html_report=not args.no_html,
        )
        summary = build_metal_tech_core_dataset(
            processed_dir=args.processed_dir,
            output_dir=args.output_dir,
            reports_dir=args.reports_dir,
            backtest_config=config,
        )
        print("Metal + tech core build summary")
        for key, value in summary.items():
            print(f"{key}: {value}")
    if args.command == "us-trend-alerts":
        import logging

        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        run_us_trend_alerts(
            TrendAlertConfig(
                etfs=tuple(str(x).upper() for x in args.etfs),
                top_n=args.top_n,
                lookback_days=args.lookback_days,
                cache_dir=args.cache_dir,
                output_dir=args.output_dir,
                local_bars_dir=args.local_bars_dir,
                data_root=args.data_root,
                force_refresh=args.force_refresh,
            )
        )


if __name__ == "__main__":
    main()
