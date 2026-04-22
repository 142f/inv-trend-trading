"""Submit a small guarded OKX demo order and optionally cancel it."""

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
    parser.add_argument("--inst-id", default="BTC-USDT")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--size", default="0.001")
    parser.add_argument("--td-mode", default="cash")
    parser.add_argument("--order-type", choices=["market", "limit"], default="limit")
    parser.add_argument("--price", default="10000")
    parser.add_argument("--pos-side", default="")
    parser.add_argument("--client-order-id", default="demoTest001")
    parser.add_argument("--submit", action="store_true", help="Actually submit the demo order.")
    parser.add_argument("--keep-open", action="store_true", help="Do not cancel submitted limit orders.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = OKXClient.from_env(args.env_file)
    print("OKX mode:", "demo" if client.config.simulated else "live")
    print("Trading enabled:", client.config.enable_trading)

    if args.order_type == "market":
        order = client.place_market_order(
            inst_id=args.inst_id,
            side=args.side,
            size=args.size,
            td_mode=args.td_mode,
            client_order_id=args.client_order_id,
            position_side=args.pos_side,
            dry_run=not args.submit,
        )
    else:
        order = client.place_limit_order(
            inst_id=args.inst_id,
            side=args.side,
            size=args.size,
            price=args.price,
            td_mode=args.td_mode,
            client_order_id=args.client_order_id,
            position_side=args.pos_side,
            dry_run=not args.submit,
        )
    print("Order response:", order)

    if args.submit:
        if args.order_type == "limit" and not args.keep_open:
            cancel = client.cancel_order(args.inst_id, client_order_id=args.client_order_id)
            print("Cancel response:", cancel)
        print("Positions:", client.positions(inst_id=args.inst_id))


if __name__ == "__main__":
    main()
