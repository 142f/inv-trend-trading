"""Place two OKX demo spot limit orders: one resting order and one fillable order."""

from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from turtle_multi_asset.okx_client import OKXClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--inst-id", default="BTC-USDT")
    parser.add_argument("--size", default="0.001")
    parser.add_argument("--resting-price", default="10000")
    parser.add_argument("--marketable-buffer", default="1.01")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--cancel-resting", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = OKXClient.from_env(args.env_file)
    ticker = client.ticker(args.inst_id)
    ask = Decimal(str(ticker["askPx"]))
    marketable_price = (ask * Decimal(args.marketable_buffer)).quantize(Decimal("0.1"), rounding=ROUND_DOWN)

    suffix = str(int(time.time()))[-8:]
    resting_cl_id = f"rest{suffix}"
    fill_cl_id = f"fill{suffix}"

    print("OKX mode:", "demo" if client.config.simulated else "live")
    print("Trading enabled:", client.config.enable_trading)
    print("Ticker:", {"instId": args.inst_id, "bidPx": ticker.get("bidPx"), "askPx": ticker.get("askPx")})

    resting_order = client.place_limit_order(
        inst_id=args.inst_id,
        side="buy",
        size=args.size,
        price=args.resting_price,
        td_mode="cash",
        client_order_id=resting_cl_id,
        dry_run=not args.submit,
    )
    print("Resting order:", resting_order)

    fillable_order = client.place_limit_order(
        inst_id=args.inst_id,
        side="buy",
        size=args.size,
        price=str(marketable_price),
        td_mode="cash",
        client_order_id=fill_cl_id,
        dry_run=not args.submit,
    )
    print("Fillable order:", fillable_order)

    if not args.submit:
        return

    time.sleep(2)
    print("Resting order detail:", client.trade.get_order(args.inst_id, clOrdId=resting_cl_id))
    print("Fillable order detail:", client.trade.get_order(args.inst_id, clOrdId=fill_cl_id))
    print("Pending orders:", client.trade.get_order_list(instId=args.inst_id))
    print("Balance:", client.balance("BTC,USDT"))

    if args.cancel_resting:
        print("Cancel resting:", client.cancel_order(args.inst_id, client_order_id=resting_cl_id))


if __name__ == "__main__":
    main()
