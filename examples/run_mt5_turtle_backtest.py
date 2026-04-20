"""使用 MetaTrader 5 券商历史数据运行多品种海龟回测。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from turtle_multi_asset import TurtleBacktester, classic_bar_rules
from turtle_multi_asset.mt5_data import (
    build_mt5_asset_specs,
    fetch_mt5_ohlc_many,
    list_mt5_symbols,
    mt5_session,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD"],
        help="MT5 broker symbols. Names must match Market Watch exactly.",
    )
    parser.add_argument("--timeframe", default="D1", help="MT5 timeframe, e.g. D1, H4, H1")
    parser.add_argument("--start", default=None, help="UTC start, e.g. 2022-01-01")
    parser.add_argument("--end", default=None, help="UTC end, e.g. 2026-04-20")
    parser.add_argument("--count", type=int, default=1200, help="Bars to fetch when start/end omitted")
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--terminal-path", default=None, help="Optional terminal64.exe path")
    parser.add_argument("--login", type=int, default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--server", default=None)
    parser.add_argument("--out-dir", default="outputs/mt5_turtle")
    parser.add_argument(
        "--list-symbols",
        default=None,
        help="List broker symbols matching this MT5 pattern, e.g. '*XAU*' or '*BTC*', then exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        with mt5_session(
            path=args.terminal_path,
            login=args.login,
            password=args.password,
            server=args.server,
        ):
            if args.list_symbols is not None:
                for name in list_mt5_symbols(args.list_symbols):
                    print(name)
                return
            data = fetch_mt5_ohlc_many(
                symbols=args.symbols,
                timeframe=args.timeframe,
                start=args.start,
                end=args.end,
                count=args.count,
            )
            specs = build_mt5_asset_specs(args.symbols)
    except RuntimeError as exc:
        print(f"MT5 data error: {exc}", file=sys.stderr)
        print(
            "Check that MT5 is running, logged in, connected, and that symbol names "
            "match your broker exactly. Use --list-symbols '*XAU*' or '*BTC*' to inspect names.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    rules = classic_bar_rules()
    result = TurtleBacktester(
        data=data,
        specs=specs,
        rules=rules,
        initial_equity=args.equity,
    ).run()

    result.equity_curve.to_csv(out_dir / "equity_curve.csv")
    result.orders.to_csv(out_dir / "orders.csv", index=False)
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.trade_details.to_csv(out_dir / "trade_details.csv", index=False)
    (out_dir / "metrics.json").write_text(
        json.dumps(result.metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Metrics")
    for key, value in result.metrics.items():
        print(f"{key}: {value:.6f}")
    print(f"\nWrote outputs to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
