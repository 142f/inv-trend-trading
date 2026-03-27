from __future__ import annotations

import argparse
from pathlib import Path

from backtest.engine import BacktestEngine
from config.loader import load_settings
from data.aggregator import aggregate_daily_frames
from data.exchange_client import ExchangeClient
from data.kline_fetcher import fetch_symbol_frames
from main.logging_setup import setup_logger


def _args():
    p = argparse.ArgumentParser(description="Paper mode (exchange data, local simulated execution)")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--exchange-id", default=None, help="e.g. binanceusdm or okx")
    p.add_argument("--default-type", default=None, help="e.g. future or swap")
    p.add_argument("--limit", type=int, default=1200)
    p.add_argument("--initial-equity", type=float, default=10000.0)
    p.add_argument("--output-dir", default="artifacts/paper")
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
    logger = setup_logger("paper", out_dir / "paper.log", level=settings.runtime.log_level)

    try:
        client = ExchangeClient(settings=settings, authenticated=False)
        dataset = {}
        for symbol in settings.strategy.symbols:
            frames = fetch_symbol_frames(client, symbol, settings.strategy.raw_timeframes, args.limit)
            frames = aggregate_daily_frames(frames)
            dataset[symbol] = frames
    except Exception as exc:
        logger.error("paper_data_fetch_failed", extra={"extra_data": {"error": str(exc)}})
        print(f"Paper data fetch failed: {exc}")
        return

    engine = BacktestEngine(settings=settings, dataset=dataset, initial_equity=args.initial_equity)
    result = engine.run()
    logger.info("paper_cycle_done", extra={"extra_data": {"fills": len(result.fills)}})
    print(f"Paper run complete. fills={len(result.fills)} last_equity={result.equity_curve.iloc[-1] if not result.equity_curve.empty else args.initial_equity}")


if __name__ == "__main__":
    main()
