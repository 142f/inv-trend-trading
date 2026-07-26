from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json

from .api import HistoricalDataService
from .providers import BinanceKlineProvider, CsvBarsProvider, QqqHoldingsCsvProvider


def main() -> None:
    parser = argparse.ArgumentParser(description="Traceable historical data ingestion")
    parser.add_argument("--root", default="data")
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download")
    download.add_argument("--symbol", required=True)
    download.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    download.add_argument("--start", required=True)
    download.add_argument("--end")
    download.add_argument("--csv")
    holdings = sub.add_parser("qqq-holdings")
    holdings.add_argument("--source", required=True)
    holdings.add_argument("--snapshot-date")
    args = parser.parse_args()
    providers = {"binance": BinanceKlineProvider()}
    if getattr(args, "csv", None):
        providers["licensed_csv"] = CsvBarsProvider(args.csv)
    service = HistoricalDataService(args.root, providers=providers)
    if args.command == "download":
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        end = (
            datetime.fromisoformat(args.end.replace("Z", "+00:00"))
            if args.end else datetime.now(timezone.utc)
        )
        print(json.dumps(
            service.ingest(args.symbol, args.timeframe, start, end).to_dict(),
            ensure_ascii=False, indent=2,
        ))
    else:
        print(service.update_qqq_holdings(
            QqqHoldingsCsvProvider(args.source), args.snapshot_date
        ))


if __name__ == "__main__":
    main()
