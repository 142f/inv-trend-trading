from __future__ import annotations

import argparse
from pathlib import Path

from backtest.engine import BacktestEngine
from config.loader import load_settings
from data.aggregator import aggregate_daily_frames
from data.exchange_client import ExchangeClient
from data.kline_fetcher import fetch_symbol_frames
from execution.order_router import LiveOrderRouter
from main.logging_setup import setup_logger


def _args():
    p = argparse.ArgumentParser(description="Live mode (dry-run default)")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--exchange-id", default=None, help="e.g. binanceusdm or okx")
    p.add_argument("--default-type", default=None, help="e.g. future or swap")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--initial-equity", type=float, default=10000.0)
    p.add_argument("--execute", action="store_true", help="Actually send orders for last open signals only")
    p.add_argument("--output-dir", default="artifacts/live")
    return p.parse_args()


def main():
    args = _args()
    settings = load_settings(args.config)
    if args.symbols:
        settings.strategy.symbols = args.symbols
    if args.exchange_id:
        settings.exchange.exchange_id = args.exchange_id
        if args.exchange_id == "okx":
            settings.exchange.default_type = "swap"
            settings.exchange.api_key_env = "OKX_API_KEY"
            settings.exchange.api_secret_env = "OKX_API_SECRET"
            settings.exchange.api_passphrase_env = "OKX_API_PASSPHRASE"
    if args.default_type:
        settings.exchange.default_type = args.default_type

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("live", out_dir / "live.log", level=settings.runtime.log_level)

    try:
        public_client = ExchangeClient(settings=settings, authenticated=False)
        dataset = {}
        for symbol in settings.strategy.symbols:
            frames = fetch_symbol_frames(public_client, symbol, settings.strategy.raw_timeframes, args.limit)
            frames = aggregate_daily_frames(frames)
            dataset[symbol] = frames
    except Exception as exc:
        logger.error("live_data_fetch_failed", extra={"extra_data": {"error": str(exc)}})
        print(f"Live data fetch failed: {exc}")
        return

    engine = BacktestEngine(settings=settings, dataset=dataset, initial_equity=args.initial_equity)
    result = engine.run()
    open_fills = [f for f in result.fills if f.action == "open"]
    print(f"Live dry-run complete. generated_open_signals={len(open_fills)}")

    if not args.execute:
        logger.info("live_dry_run_done", extra={"extra_data": {"open_signals": len(open_fills)}})
        return

    auth_client = ExchangeClient(settings=settings, authenticated=True)
    router = LiveOrderRouter(auth_client)
    if not open_fills:
        print("No open signals to execute.")
        return

    # Safety: only send orders generated on latest timestamp
    latest_ts = max(f.timestamp for f in open_fills)
    to_send = [f for f in open_fills if f.timestamp == latest_ts]
    for fill in to_send:
        order = router.market_open(symbol=fill.symbol, side=fill.side, quantity=fill.quantity)
        logger.info(
            "live_order_sent",
            extra={"extra_data": {"symbol": fill.symbol, "side": fill.side, "qty": fill.quantity, "order_id": order.get("id")}},
        )
        print(f"sent order symbol={fill.symbol} side={fill.side} qty={fill.quantity}")


if __name__ == "__main__":
    main()
