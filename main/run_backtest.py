from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.report import build_report
from config.loader import load_settings
from data.aggregator import aggregate_daily_frames
from data.exchange_client import ExchangeClient
from data.kline_fetcher import fetch_symbol_frames
from data.synthetic import build_synthetic_dataset
from data.validators import validate_frames
from main.logging_setup import setup_logger


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run crypto strategy backtest")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--exchange-id", default=None, help="e.g. binanceusdm or okx")
    p.add_argument("--default-type", default=None, help="e.g. future or swap")
    p.add_argument("--limit", type=int, default=2000)
    p.add_argument("--initial-equity", type=float, default=10000.0)
    p.add_argument("--use-synthetic", action="store_true")
    p.add_argument("--output-dir", default="artifacts/backtest")
    return p.parse_args()


def _build_dataset_from_exchange(settings, limit: int) -> Dict[str, Dict[str, pd.DataFrame]]:
    client = ExchangeClient(settings=settings, authenticated=False)
    dataset = {}
    for symbol in settings.strategy.symbols:
        frames = fetch_symbol_frames(
            client=client,
            symbol=symbol,
            raw_timeframes=settings.strategy.raw_timeframes,
            limit=limit,
        )
        frames = aggregate_daily_frames(frames)
        dataset[symbol] = frames
    return dataset


def main():
    args = _parse_args()
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
    settings.data.fetch_limit = args.limit

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("runtime", out_dir / "runtime.log", level=settings.runtime.log_level)

    if args.use_synthetic:
        dataset = build_synthetic_dataset(settings.strategy.symbols)
        logger.info("using synthetic dataset")
    else:
        try:
            dataset = _build_dataset_from_exchange(settings, limit=settings.data.fetch_limit)
            logger.info("using exchange dataset")
        except Exception as exc:
            logger.error(
                "exchange_data_fetch_failed",
                extra={"extra_data": {"error": str(exc)}},
            )
            print(f"Exchange data fetch failed: {exc}")
            print("Tip: run with --use-synthetic to verify local pipeline.")
            return

    required = ["15m", "30m", "1h", "2h", "4h", "1d", "2d", "5d", "7d"]
    for symbol, frames in dataset.items():
        q = validate_frames(frames, required_timeframes=required, max_gap_multiple=settings.data.max_gap_multiple)
        if not q.can_open_new_positions:
            logger.warning(
                f"data quality issues for {symbol}",
                extra={"extra_data": {"symbol": symbol, "issues": q.issues}},
            )

    engine = BacktestEngine(settings=settings, dataset=dataset, initial_equity=args.initial_equity)
    result = engine.run()
    report = build_report(result.equity_curve, result.fills, args.initial_equity)

    fills_df = pd.DataFrame([f.__dict__ for f in result.fills])
    fills_path = out_dir / "fills.csv"
    if not fills_df.empty:
        fills_df.to_csv(fills_path, index=False)
    eq_path = out_dir / "equity.csv"
    result.equity_curve.to_csv(eq_path, header=True)

    summary = {
        "final_equity": report.final_equity,
        "total_return": report.total_return,
        "max_drawdown": report.max_drawdown,
        "trade_count": report.trade_count,
        "win_rate": report.win_rate,
        "avg_pnl_per_trade": report.avg_pnl_per_trade,
        "fills_csv": str(fills_path),
        "equity_csv": str(eq_path),
    }
    logger.info("backtest_summary", extra={"extra_data": summary})
    print("Backtest Summary")
    for k, v in summary.items():
        print(f"{k}: {v}")
    if result.alerts:
        print("\nAlerts:")
        for a in result.alerts[:20]:
            print(f"- {a}")


if __name__ == "__main__":
    main()
