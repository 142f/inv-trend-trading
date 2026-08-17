"""Scan configured instruments for completed D1 Turtle breakouts."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import sys
import time

from inv_trend.data import HistoricalDataService, load_bars
from inv_trend.data.providers import (
    HoldingsCsvProvider,
    OFFICIAL_QQQ_HOLDINGS_URL,
    OFFICIAL_SPY_HOLDINGS_URL,
)
from rich.console import Console

from inv_trend.adapters.detector.alerts import JsonLinesNotifier
from inv_trend.adapters.detector.alerts.breakout import scan_configured_d1
from inv_trend.adapters.detector.alerts.report import render_and_write_report
from inv_trend.adapters.detector.models import AssetConfig, Market
from inv_trend.adapters.detector.storage.signal_repository import JsonSignalRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", help="Configured symbol; repeat as needed")
    parser.add_argument("--assets-config", default=None)
    parser.add_argument("--universe", choices=["qqq-spy-top50"])
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--state", default="outputs/turtle_alerts/state.json")
    parser.add_argument("--alerts", default="outputs/turtle_alerts/alerts.jsonl")
    parser.add_argument("--log-dir", default="outputs/turtle_alerts")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--watch", action="store_true", help="Continuously scan at a fixed interval")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument(
        "--refresh-before-scan", action="store_true",
        help="Refresh the selected holdings universe through HistoricalDataService first",
    )
    parser.add_argument(
        "--refresh-official-holdings", action="store_true",
        help="Reload official QQQ/SPY top-50 holdings before each scan cycle",
    )
    parser.add_argument("--max-cycles", type=int, help="Stop after N cycles; useful for jobs/tests")
    parser.add_argument("--allow-research-data", action="store_true")
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("--interval-seconds must be at least 60")
    if args.max_cycles is not None and args.max_cycles < 1:
        parser.error("--max-cycles must be positive")
    if args.refresh_before_scan and args.universe != "qqq-spy-top50":
        parser.error("--refresh-before-scan requires --universe qqq-spy-top50")
    if args.refresh_official_holdings and args.universe != "qqq-spy-top50":
        parser.error("--refresh-official-holdings requires --universe qqq-spy-top50")

    cycle = 0
    while True:
        cycle += 1
        service = HistoricalDataService(args.data_root)
        if args.refresh_official_holdings:
            for fund, source in (
                ("QQQ", OFFICIAL_QQQ_HOLDINGS_URL),
                ("SPY", OFFICIAL_SPY_HOLDINGS_URL),
            ):
                service.update_holdings(HoldingsCsvProvider(source, fund=fund), top=50)

        from inv_trend.application import load_detector_asset_configs

        assets = load_detector_asset_configs(args.assets_config)
        metadata: dict[str, dict[str, str]] = {}
        if args.universe:
            holdings = service.load_holdings(top=50)
            for row in holdings.to_dict("records"):
                symbol = str(row["symbol"])
                metadata[symbol] = {
                    key: str(value) for key, value in row.items() if key != "symbol"
                }
                if symbol not in assets:
                    instrument = service.instrument(symbol)
                    assets[symbol] = AssetConfig(
                        symbol=symbol, instrument=instrument.instrument_id,
                        market=Market.US_EQUITY, data_source=instrument.primary_source,
                        price_type="equity", timeframes=("D1",), timezone=instrument.timezone,
                        session="regular", adjustment="back_adjusted",
                    )
        if args.symbol:
            wanted = {value.upper() for value in args.symbol}
            unknown = wanted - set(assets)
            if unknown:
                raise ValueError(f"symbols are not configured: {sorted(unknown)}")
            selected = [assets[symbol] for symbol in sorted(wanted)]
        else:
            selected = list(assets.values())

        if args.refresh_before_scan:
            now = datetime.now(timezone.utc)
            refresh = service.sync_holdings_universe(
                timeframe="D1",
                start=(now - timedelta(days=730)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
                end=now,
            )
            Console(no_color=args.no_color or not sys.stdout.isatty()).print(
                f"数据同步：成功 {len(refresh['succeeded'])}，失败 {len(refresh['failed'])}"
            )
        result = scan_configured_d1(
            JsonSignalRepository(args.state), JsonLinesNotifier(args.alerts),
            assets=selected,
            universe_metadata=metadata,
            loader=lambda symbol, timeframe: load_bars(
                symbol, timeframe, root=args.data_root, completed_only=True,
                allow_research=args.allow_research_data,
            ),
        )
        render_and_write_report(
            result, args.log_dir,
            no_color=args.no_color or not sys.stdout.isatty(),
            console=Console(no_color=args.no_color or not sys.stdout.isatty()),
        )
        if not args.watch or (args.max_cycles is not None and cycle >= args.max_cycles):
            break
        try:
            time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
