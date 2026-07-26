"""Run one configured detector scan from a canonical CSV file."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .alerts.notifier import CompositeNotifier, ConsoleNotifier, JsonLinesNotifier
from .config.loader import load_asset_configs, load_strategy_config
from .engine.scanner import TurtleScanner
from .storage.signal_repository import JsonSignalRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--assets-config", default=None)
    parser.add_argument("--strategy-config", default=None)
    parser.add_argument("--state", default="outputs/turtle_detector/state.json")
    parser.add_argument("--alerts", default="outputs/turtle_detector/signals.jsonl")
    args = parser.parse_args()

    assets = load_asset_configs(args.assets_config)
    symbol = args.symbol.upper()
    if symbol not in assets:
        raise ValueError(f"symbol is not configured: {symbol}")
    bars = pd.read_csv(Path(args.bars))
    scanner = TurtleScanner(
        load_strategy_config(args.strategy_config),
        JsonSignalRepository(args.state),
        CompositeNotifier([ConsoleNotifier(), JsonLinesNotifier(args.alerts)]),
    )
    scanner.scan_and_store(bars, assets[symbol], args.timeframe.upper())


if __name__ == "__main__":
    main()
