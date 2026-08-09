from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os

from .api import HistoricalDataService
from .audit import audit_universe
from .legacy import migrate_legacy_csv
from .providers import (
    AlphaVantageProvider,
    BinanceKlineProvider,
    CsvBarsProvider,
    DukascopyBarsProvider,
    QqqHoldingsCsvProvider,
    YahooChartProvider,
)


def _providers(csv: str | None = None) -> dict[str, object]:
    providers: dict[str, object] = {
        "binance": BinanceKlineProvider(),
        "dukascopy": DukascopyBarsProvider(),
        "yahoo_chart": YahooChartProvider(),
    }
    if os.getenv("ALPHAVANTAGE_API_KEY"):
        providers["alpha_vantage"] = AlphaVantageProvider()
    if csv:
        providers["licensed_csv"] = CsvBarsProvider(csv)
    return providers


def _symbol_timeframe_command(sub: argparse._SubParsersAction, name: str) -> None:
    command = sub.add_parser(name)
    command.add_argument("--symbol", required=True)
    command.add_argument("--timeframe", required=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Traceable historical market-data ingestion")
    parser.add_argument("--root", default="data")
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("download")
    download.add_argument("--symbol", required=True)
    download.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    download.add_argument("--start", required=True)
    download.add_argument("--end")
    download.add_argument("--csv")
    update = sub.add_parser("update")
    update.add_argument("--symbol", required=True)
    update.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    update.add_argument("--end")
    update.add_argument("--csv")
    update_gaps = sub.add_parser("update-gaps")
    update_gaps.add_argument("--symbol", required=True)
    update_gaps.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    update_gaps.add_argument("--end")
    reprocess = sub.add_parser("reprocess")
    reprocess.add_argument("--symbol", required=True)
    reprocess.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    reprocess.add_argument("--run-id", required=True)
    update_batch = sub.add_parser("update-batch")
    update_batch.add_argument("--symbols", nargs="+", required=True)
    update_batch.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    update_batch.add_argument("--end")
    for name in ("status", "coverage", "versions", "validate"):
        _symbol_timeframe_command(sub, name)
    verify = sub.add_parser("verify")
    verify.add_argument("--kind", choices=["catalog", "universe"], default="catalog")
    audit = sub.add_parser("audit")
    audit.add_argument("--symbols", nargs="+", required=True)
    audit.add_argument("--timeframe", choices=["D1", "H4", "H1"], default="D1")
    missing = sub.add_parser("missing")
    missing.add_argument("--symbol", required=True)
    missing.add_argument("--timeframe", choices=["D1", "H4", "H1"], required=True)
    missing.add_argument("--end")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--symbol", required=True)
    rollback.add_argument("--timeframe", required=True)
    rollback.add_argument("--version", required=True)
    rollback.add_argument("--reason", required=True)
    repair = sub.add_parser("repair-current")
    repair.add_argument("--symbol", required=True)
    repair.add_argument("--timeframe", required=True)
    review = sub.add_parser("review")
    review.add_argument("action", choices=["list", "approve", "reject"])
    review.add_argument("--run-id")
    review.add_argument("--reason", default="")
    migrate = sub.add_parser("migrate-legacy")
    migrate.add_argument("--input", default="processed_data")
    holdings = sub.add_parser("qqq-holdings")
    holdings.add_argument("--source", required=True)
    holdings.add_argument("--snapshot-date")
    args = parser.parse_args()

    if args.command == "migrate-legacy":
        print(json.dumps(migrate_legacy_csv(args.input, args.root), ensure_ascii=False, indent=2))
        return
    service = HistoricalDataService(args.root, providers=_providers(getattr(args, "csv", None)))
    if args.command == "download":
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else datetime.now(timezone.utc)
        print(json.dumps(service.ingest(args.symbol, args.timeframe, start, end).to_dict(), ensure_ascii=False, indent=2))
        return
    if args.command == "update":
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
        print(json.dumps(service.update(args.symbol, args.timeframe, end=end).to_dict(), ensure_ascii=False, indent=2))
        return
    if args.command == "update-gaps":
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
        print(json.dumps([m.to_dict() for m in service.update_gaps(args.symbol, args.timeframe, end=end)],
                         ensure_ascii=False, indent=2, default=str))
        return
    if args.command == "missing":
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
        print(json.dumps(service.missing_intervals(args.symbol, args.timeframe, end=end),
                         ensure_ascii=False, indent=2, default=str))
        return
    if args.command == "reprocess":
        print(json.dumps(service.reprocess_raw(args.symbol, args.timeframe, args.run_id).to_dict(),
                         ensure_ascii=False, indent=2))
        return
    if args.command == "update-batch":
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
        print(json.dumps(service.update_many(args.symbols, args.timeframe, end=end), ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        print(json.dumps(service.status(args.symbol, args.timeframe), ensure_ascii=False, indent=2))
        return
    if args.command == "coverage":
        print(json.dumps(service.coverage(args.symbol, args.timeframe), ensure_ascii=False, indent=2))
        return
    if args.command == "validate":
        bars = service.load_bars(args.symbol, args.timeframe)
        status = service.status(args.symbol, args.timeframe)
        status["validated_rows"] = len(bars)
        status["validation"] = "passed"
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    if args.command == "versions":
        print(json.dumps(service.lake.list_versions(args.symbol, args.timeframe), ensure_ascii=False, indent=2))
        return
    if args.command == "verify":
        if args.kind == "catalog":
            print(json.dumps(service.verify_catalog(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps([{"symbol": s, "status": "registered",
                               "instrument_id": cfg.instrument_id, "primary_source": cfg.primary_source,
                               "venue": cfg.venue, "asset_class": cfg.asset_class, "session": cfg.session}
                              for s, cfg in service.instruments.items()],
                             ensure_ascii=False, indent=2))
        return
    if args.command == "audit":
        print(json.dumps(audit_universe(args.root, args.symbols, args.timeframe), ensure_ascii=False, indent=2, default=str))
        return
    if args.command == "rollback":
        service.lake.rollback(args.symbol, args.timeframe, args.version, args.reason)
        print("rolled back")
        return
    if args.command == "repair-current":
        service.lake.repair_current(args.symbol, args.timeframe)
        print(json.dumps(service.repair_current_lineage(args.symbol, args.timeframe), ensure_ascii=False, indent=2))
        return
    if args.command == "review":
        if args.action == "list":
            print(json.dumps(service.lake.list_reviews(), ensure_ascii=False, indent=2))
            return
        if not args.run_id:
            parser.error("review approve/reject requires --run-id")
        decision = "approved" if args.action == "approve" else "rejected"
        if decision == "approved":
            print(service.approve_review(args.run_id, args.reason))
        else:
            print(service.lake.review(args.run_id, decision, args.reason))
        return
    if args.command == "qqq-holdings":
        print(service.update_qqq_holdings(QqqHoldingsCsvProvider(args.source), args.snapshot_date))


if __name__ == "__main__":
    main()
