"""Test OKX demo swap trading with the configured max leverage."""

from __future__ import annotations

import argparse
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
    parser.add_argument("--inst-id", default=None)
    parser.add_argument("--size", default="0.01")
    parser.add_argument("--leverage", type=int, default=None)
    parser.add_argument("--td-mode", default=None)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = OKXClient.from_env(args.env_file)
    inst_id = args.inst_id or client.config.default_swap_inst_id
    leverage = args.leverage or client.config.default_swap_leverage
    td_mode = args.td_mode or client.config.default_swap_td_mode
    suffix = str(int(time.time()))[-8:]

    print("OKX mode:", "demo" if client.config.simulated else "live")
    print("Trading enabled:", client.config.enable_trading)
    print("Swap config:", {"inst_id": inst_id, "td_mode": td_mode, "leverage": leverage, "size": args.size})

    if not args.submit:
        preview = client.place_market_order(
            inst_id=inst_id,
            side="buy",
            size=args.size,
            td_mode=td_mode,
            client_order_id=f"swpPrev{suffix}",
        )
        print("Dry-run open preview:", preview)
        return

    print("Set position mode:", client.account.set_position_mode("net_mode"))
    print("Set leverage:", client.set_leverage(inst_id=inst_id, lever=leverage, margin_mode=td_mode))
    print("Current leverage:", client.account.get_leverage(mgnMode=td_mode, instId=inst_id))

    open_order = client.place_market_order(
        inst_id=inst_id,
        side="buy",
        size=args.size,
        td_mode=td_mode,
        client_order_id=f"swpOpen{suffix}",
        dry_run=False,
    )
    print("Open order:", open_order)
    print("Positions after open:", client.positions(inst_type="SWAP", inst_id=inst_id))

    if not args.keep_open:
        close_order = client.place_market_order(
            inst_id=inst_id,
            side="sell",
            size=args.size,
            td_mode=td_mode,
            client_order_id=f"swpCls{suffix}",
            reduce_only=True,
            dry_run=False,
        )
        print("Close order:", close_order)
        print("Positions after close:", client.positions(inst_type="SWAP", inst_id=inst_id))


if __name__ == "__main__":
    main()
