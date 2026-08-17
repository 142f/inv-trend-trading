"""Run one configured detector scan from a canonical CSV file."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from inv_trend.adapters.detector.alerts.notifier import (
    CompositeNotifier,
    ConsoleNotifier,
    JsonLinesNotifier,
)
from inv_trend.adapters.detector.config.loader import load_strategy_config
from inv_trend.adapters.detector.storage.signal_repository import JsonSignalRepository
from inv_trend.application import DetectorService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", help="Legacy/test CSV input. Repository is used when omitted.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--assets-config", default=None)
    parser.add_argument("--strategy-config", default=None)
    parser.add_argument("--state", default="outputs/turtle_detector/state.json")
    parser.add_argument("--alerts", default="outputs/turtle_detector/signals.jsonl")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--allow-research-data", action="store_true")
    args = parser.parse_args()

    from inv_trend.application import load_detector_asset_configs

    assets = load_detector_asset_configs(args.assets_config)
    symbol = args.symbol.upper()
    if symbol not in assets:
        raise ValueError(f"symbol is not configured: {symbol}")
    if args.bars:
        bars = pd.read_csv(Path(args.bars))
    else:
        from inv_trend.data import load_bars
        bars = load_bars(
            symbol, args.timeframe.upper(), root=args.data_root,
            allow_research=args.allow_research_data,
        )
    # Keep every existing CLI argument.  State changes are now committed by
    # the application service; CLI presentation remains intentionally thin.
    repository = JsonSignalRepository(args.state)
    result = DetectorService(load_strategy_config(args.strategy_config), repository).scan(
        bars, assets[symbol], args.timeframe.upper()
    )
    if result.committed and result.decision.execution_transition:
        CompositeNotifier([ConsoleNotifier(), JsonLinesNotifier(args.alerts)]).notify(
            result.decision.signal
        )


if __name__ == "__main__":
    main()
