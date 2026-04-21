"""Download and normalize external long-history XAU/BTC/XAG/ETH H4 data.

Sources:
- XAUUSD/XAGUSD: Dukascopy chart API through ``dukascopy-python``.
- BTCUSDT/ETHUSDT: Binance spot REST klines API.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from examples.download_mt5_data import data_quality


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data_external_xau_btc")
    parser.add_argument("--xau-start", default="2005-01-01")
    parser.add_argument("--xag-start", default="2015-01-01")
    parser.add_argument("--btc-start", default="2017-08-17")
    parser.add_argument("--eth-start", default="2017-08-17")
    parser.add_argument("--end", default="2026-04-20")
    parser.add_argument("--timeframe", default="H4")
    parser.add_argument(
        "--include",
        nargs="+",
        default=["xau", "btc"],
        choices=["xau", "btc", "xag", "eth"],
        help="Assets to download.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.timeframe.upper() != "H4":
        raise ValueError("this downloader currently writes H4 data only")

    data_dir = Path(args.data_dir)
    raw_root = data_dir / "raw"
    processed_root = data_dir / "processed" / "external" / "H4"
    metadata_root = data_dir / "metadata" / "external"
    processed_root.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)

    end = _to_utc(args.end)
    frames = {}
    include = set(args.include)
    if "xau" in include:
        xau = fetch_dukascopy("XAU/USD", _to_utc(args.xau_start), end)
        frames["XAUUSD_DUKAS"] = normalize_frame(xau, "XAUUSD_DUKAS", "dukascopy")
    if "xag" in include:
        xag = fetch_dukascopy("XAG/USD", _to_utc(args.xag_start), end)
        frames["XAGUSD_DUKAS"] = normalize_frame(xag, "XAGUSD_DUKAS", "dukascopy")
    if "btc" in include:
        btc = fetch_binance("BTCUSDT", _to_utc(args.btc_start), end)
        frames["BTCUSDT_BINANCE"] = normalize_frame(btc, "BTCUSDT_BINANCE", "binance")
    if "eth" in include:
        eth = fetch_binance("ETHUSDT", _to_utc(args.eth_start), end)
        frames["ETHUSDT_BINANCE"] = normalize_frame(eth, "ETHUSDT_BINANCE", "binance")

    log_rows: list[dict] = []
    quality_rows: list[dict] = []
    for symbol, frame in frames.items():
        source = str(frame["source"].iloc[0])
        raw_dir = raw_root / source / symbol
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "H4.csv"
        processed_path = processed_root / f"{symbol}.csv"
        frame.to_csv(raw_path, index=False)
        frame.to_csv(processed_path, index=False)

        point = 0.001 if symbol.startswith(("XAU", "XAG")) else 0.01
        quality_rows.append(
            data_quality(
                frame,
                symbol=symbol,
                timeframe="H4",
                point=point,
            )
        )
        log_rows.append(
            {
                "symbol": symbol,
                "source": source,
                "timeframe": "H4",
                "raw_path": str(raw_path),
                "processed_path": str(processed_path),
                "bar_count": len(frame),
                "start": frame["time"].iloc[0] if not frame.empty else "",
                "end": frame["time"].iloc[-1] if not frame.empty else "",
            }
        )

    pd.DataFrame(log_rows).to_csv(metadata_root / "download_log.csv", index=False)
    pd.DataFrame(quality_rows).to_csv(metadata_root / "data_quality_report.csv", index=False)
    pd.DataFrame(symbol_specs(include)).to_csv(metadata_root / "symbol_specs.csv", index=False)

    print("Saved processed files:")
    for row in log_rows:
        print(f"{row['symbol']}: {row['processed_path']} ({row['bar_count']} bars)")
    print(f"Metadata: {metadata_root.resolve()}")


def fetch_dukascopy(instrument: str, start: datetime, end: datetime) -> pd.DataFrame:
    import dukascopy_python as dp

    df = dp.fetch(
        instrument,
        dp.INTERVAL_HOUR_4,
        dp.OFFER_SIDE_BID,
        start,
        end,
        max_retries=3,
        debug=False,
    )
    return df.reset_index()


def fetch_binance(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step_ms = 4 * 60 * 60 * 1000
    cursor = start_ms
    rows: list[list] = []
    session = requests.Session()
    while cursor <= end_ms:
        params = {
            "symbol": symbol,
            "interval": "4h",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        for attempt in range(6):
            try:
                response = session.get(url, params=params, timeout=30)
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(1.5 * (attempt + 1))
        chunk = response.json()
        if not chunk:
            break
        rows.extend(chunk)
        next_cursor = int(chunk[-1][0]) + step_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.05)

    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "ignore",
    ]
    df = pd.DataFrame(rows, columns=columns)
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def normalize_frame(df: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
    out = df.rename(columns={"timestamp": "time"}).copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.drop_duplicates(subset=["time"]).sort_values("time")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out["open"] > 0) & (out["high"] > 0) & (out["low"] > 0) & (out["close"] > 0)]
    out = out[
        (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    ]
    out["spread"] = 0.0
    out["source"] = source
    out["symbol"] = symbol
    out["timeframe"] = "H4"
    return out[
        [
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "spread",
            "source",
            "symbol",
            "timeframe",
        ]
    ]


def symbol_specs(include: set[str]) -> list[dict]:
    specs = []
    if "xau" in include:
        specs.append(
            {
                "name": "XAUUSD_DUKAS",
                "description": "Dukascopy XAU/USD bid H4",
                "source": "dukascopy",
                "trade_contract_size": 1.0,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "point": 0.001,
                "digits": 3,
                "cost_bps": 1.0,
                "slippage_bps": 3.0,
            },
        )
    if "xag" in include:
        specs.append(
            {
                "name": "XAGUSD_DUKAS",
                "description": "Dukascopy XAG/USD bid H4",
                "source": "dukascopy",
                "trade_contract_size": 1.0,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "point": 0.001,
                "digits": 3,
                "cost_bps": 1.5,
                "slippage_bps": 4.0,
            },
        )
    if "btc" in include:
        specs.append(crypto_spec("BTCUSDT_BINANCE", "Binance spot BTCUSDT H4", 0.001))
    if "eth" in include:
        specs.append(crypto_spec("ETHUSDT_BINANCE", "Binance spot ETHUSDT H4", 0.001))
    return specs


def crypto_spec(name: str, description: str, min_qty: float) -> dict:
    return {
        "name": name,
        "description": description,
        "source": "binance",
        "trade_contract_size": 1.0,
        "volume_min": min_qty,
        "volume_step": min_qty,
        "point": 0.01,
        "digits": 2,
        "cost_bps": 3.0,
        "slippage_bps": 8.0,
    }


def _to_utc(value: str) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone.utc)
    return timestamp.tz_convert(timezone.utc).to_pydatetime()


if __name__ == "__main__":
    main()
