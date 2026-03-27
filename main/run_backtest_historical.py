from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.report import build_report
from config.loader import load_settings
from data.aggregator import aggregate_daily_frames
from data.exchange_client import ExchangeClient
from data.historical_fetcher import fetch_symbol_frames_range
from data.synthetic import build_synthetic_dataset
from data.validators import validate_frames
from main.logging_setup import setup_logger


def _parse_utc_datetime(text: str) -> datetime:
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _args():
    p = argparse.ArgumentParser(description="Run historical-range backtest")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--exchange-id", default=None, help="e.g. okx or binanceusdm")
    p.add_argument("--default-type", default=None, help="e.g. swap/future")
    p.add_argument("--start", required=True, help="ISO datetime, e.g. 2024-01-01T00:00:00Z")
    p.add_argument("--end", required=True, help="ISO datetime, e.g. 2025-01-01T00:00:00Z")
    p.add_argument("--warmup-days", type=int, default=1300, help="Indicator warmup days before --start")
    p.add_argument("--limit-per-call", type=int, default=500)
    p.add_argument("--max-pages", type=int, default=300)
    p.add_argument("--initial-equity", type=float, default=10000.0)
    p.add_argument("--use-synthetic", action="store_true")
    p.add_argument("--output-dir", default="artifacts/backtest_historical")
    return p.parse_args()


def _build_dataset_historical(
    settings,
    fetch_start: datetime,
    end: datetime,
    limit_per_call: int,
    max_pages: int,
) -> Dict[str, Dict[str, pd.DataFrame]]:
    client = ExchangeClient(settings=settings, authenticated=False)
    dataset = {}
    for symbol in settings.strategy.symbols:
        frames = fetch_symbol_frames_range(
            client=client,
            symbol=symbol,
            raw_timeframes=settings.strategy.raw_timeframes,
            start=fetch_start,
            end=end,
            limit_per_call=limit_per_call,
            max_pages=max_pages,
            strict_complete=True,
        )

        # Guard against partial-history responses from exchanges with short retention.
        for tf in settings.strategy.raw_timeframes:
            tf_df = frames.get(tf)
            if tf_df is None or tf_df.empty:
                raise RuntimeError(f"No historical bars for {symbol} {tf} in requested range")
            first_open = tf_df.index.min()
            if first_open.tzinfo is None:
                first_open = first_open.tz_localize("UTC")
            else:
                first_open = first_open.tz_convert("UTC")
            fetch_start_ts = pd.Timestamp(fetch_start)
            fetch_start_ts = fetch_start_ts.tz_localize("UTC") if fetch_start_ts.tzinfo is None else fetch_start_ts.tz_convert("UTC")
            if first_open > fetch_start_ts:
                raise RuntimeError(
                    f"Insufficient history for {symbol} {tf}: first_open={first_open.isoformat()} > requested_start={fetch_start_ts.isoformat()}"
                )

        frames = aggregate_daily_frames(frames)
        dataset[symbol] = frames
    return dataset


def main():
    args = _args()
    start = _parse_utc_datetime(args.start)
    end = _parse_utc_datetime(args.end)
    if start >= end:
        raise ValueError("start must be earlier than end")
    if args.warmup_days < 0:
        raise ValueError("warmup-days must be >= 0")
    fetch_start = start - pd.Timedelta(days=args.warmup_days)

    settings = load_settings(args.config)
    if args.symbols:
        settings.strategy.symbols = args.symbols
    if args.exchange_id:
        settings.exchange.exchange_id = args.exchange_id
        if args.exchange_id == "okx":
            # common default for OKX perpetual
            settings.exchange.default_type = "swap"
            settings.exchange.api_key_env = "OKX_API_KEY"
            settings.exchange.api_secret_env = "OKX_API_SECRET"
            settings.exchange.api_passphrase_env = "OKX_API_PASSPHRASE"
    if args.default_type:
        settings.exchange.default_type = args.default_type

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger("backtest_historical", out_dir / "runtime.log", level=settings.runtime.log_level)

    if args.use_synthetic:
        dataset = build_synthetic_dataset(settings.strategy.symbols)
        logger.info("using synthetic dataset for historical backtest")
    else:
        try:
            dataset = _build_dataset_historical(
                settings=settings,
                fetch_start=fetch_start,
                end=end,
                limit_per_call=args.limit_per_call,
                max_pages=args.max_pages,
            )
        except Exception as exc:
            logger.error("historical_data_fetch_failed", extra={"extra_data": {"error": str(exc)}})
            print(f"Historical data fetch failed: {exc}")
            print("Tip: add --use-synthetic to verify local pipeline.")
            return

    required = ["15m", "30m", "1h", "2h", "4h", "1d", "2d", "5d", "7d"]
    for symbol, frames in dataset.items():
        q = validate_frames(frames, required_timeframes=required, max_gap_multiple=settings.data.max_gap_multiple)
        if not q.can_open_new_positions:
            logger.warning(
                f"data quality issues for {symbol}",
                extra={"extra_data": {"symbol": symbol, "issues": q.issues}},
            )

    engine = BacktestEngine(
        settings=settings,
        dataset=dataset,
        initial_equity=args.initial_equity,
        trade_start_ts=pd.Timestamp(start),
    )
    result = engine.run()
    report = build_report(result.equity_curve, result.fills, args.initial_equity)

    fills_df = pd.DataFrame([f.__dict__ for f in result.fills])
    fills_path = out_dir / "fills.csv"
    if not fills_df.empty:
        fills_df.to_csv(fills_path, index=False)
    eq_path = out_dir / "equity.csv"
    result.equity_curve.to_csv(eq_path, header=True)

    summary = {
        "exchange_id": settings.exchange.exchange_id,
        "default_type": settings.exchange.default_type,
        "symbols": settings.strategy.symbols,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "final_equity": report.final_equity,
        "total_return": report.total_return,
        "max_drawdown": report.max_drawdown,
        "trade_count": report.trade_count,
        "win_rate": report.win_rate,
        "avg_pnl_per_trade": report.avg_pnl_per_trade,
        "fills_csv": str(fills_path),
        "equity_csv": str(eq_path),
    }
    logger.info("historical_backtest_summary", extra={"extra_data": summary})
    print("Historical Backtest Summary")
    for k, v in summary.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
