"""Check OKX connectivity and show a dry-run order payload."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from turtle_multi_asset.okx_client import OKXClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--inst-id", default="BTC-USDT-SWAP")
    parser.add_argument("--bar", default="1m")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--with-private", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = OKXClient.from_env(args.env_file)

    print("OKX mode:", "demo" if client.config.simulated else "live")
    ticker = client.ticker(args.inst_id)
    print(
        "Ticker:",
        {
            "instId": ticker.get("instId"),
            "last": ticker.get("last"),
            "bidPx": ticker.get("bidPx"),
            "askPx": ticker.get("askPx"),
            "ts": ticker.get("ts"),
        },
    )

    candles = client.candles(args.inst_id, bar=args.bar, limit=args.limit)
    print("Candles tail:")
    print(candles.tail(5).to_string(index=False))

    preview = client.place_market_order(
        inst_id=args.inst_id,
        side="buy",
        size="1",
        td_mode="cross",
        client_order_id="dryrun_demo_001",
    )
    print("Dry-run order preview:", preview)

    if args.with_private:
        print("Account balance:", client.balance())
        print("Positions:", client.positions(inst_id=args.inst_id))


if __name__ == "__main__":
    main()
