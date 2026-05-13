"""Download Nasdaq historical daily OHLC data for an equity universe."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import requests

from examples.download_mt5_data import data_quality


DEFAULT_SYMBOLS = [
    "NVDA",
    "MU",
    "AMD",
    "TSM",
    "SNDK",
    "AVGO",
    "QQQ",
    "SPY",
    "XLY",
    "ORCL",
    "MSFT",
    "PLTR",
    "NFLX",
    "META",
    "AAPL",
    "TSLA",
    "GOOGL",
    "AMZN",
]

ETF_SYMBOLS = {"QQQ", "SPY", "XLY"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--data-dir", default="data_external_equities")
    parser.add_argument("--start", default="1990-01-01")
    parser.add_argument("--end", default="2026-04-21")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    raw_root = data_dir / "raw" / "nasdaq"
    processed_root = data_dir / "processed" / "nasdaq" / "D1"
    metadata_root = data_dir / "metadata" / "nasdaq"
    processed_root.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)

    quality_rows: list[dict] = []
    log_rows: list[dict] = []
    spec_rows: list[dict] = []
    for symbol in args.symbols:
        try:
            raw_json = fetch_nasdaq(symbol, args.start, args.end)
            processed = normalize_nasdaq(raw_json, symbol)
        except Exception as exc:
            log_rows.append(failed_log_row(symbol, exc))
            print(f"Skipped {symbol}: {exc}")
            continue
        if processed.empty:
            log_rows.append(failed_log_row(symbol, "no valid OHLC rows after normalization"))
            print(f"Skipped {symbol}: no valid OHLC rows after normalization")
            continue

        raw_symbol_dir = raw_root / symbol
        raw_symbol_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_symbol_dir / "D1.json"
        processed_path = processed_root / f"{symbol}.csv"
        raw_path.write_text(json.dumps(raw_json, indent=2), encoding="utf-8")
        processed.to_csv(processed_path, index=False)

        quality_rows.append(data_quality(processed, symbol=symbol, timeframe="D1", point=0.01))
        log_rows.append(
            {
                "symbol": symbol,
                "source": "nasdaq",
                "timeframe": "D1",
                "raw_path": str(raw_path),
                "processed_path": str(processed_path),
                "bar_count": len(processed),
                "start": processed["time"].iloc[0],
                "end": processed["time"].iloc[-1],
                "status": "ok",
                "error": "",
            }
        )
        spec_rows.append(
            {
                "name": symbol,
                "description": f"Nasdaq historical daily {symbol}",
                "source": "nasdaq",
                "trade_contract_size": 1.0,
                "volume_min": 1.0,
                "volume_step": 1.0,
                "point": 0.01,
                "digits": 2,
                "cost_bps": 1.0,
                "slippage_bps": 5.0,
            }
        )

    merge_metadata(metadata_root / "data_quality_report.csv", quality_rows, "symbol")
    merge_metadata(metadata_root / "download_log.csv", log_rows, "symbol")
    merge_metadata(metadata_root / "symbol_specs.csv", spec_rows, "name")

    print("Saved processed files:")
    for row in log_rows:
        print(f"{row['symbol']}: {row['processed_path']} ({row['bar_count']} bars, {row['status']})")
    print(f"Metadata: {metadata_root.resolve()}")


def fetch_nasdaq(symbol: str, start: str, end: str) -> dict:
    asset_class = "etf" if symbol in ETF_SYMBOLS else "stocks"
    url = f"https://api.nasdaq.com/api/quote/{symbol}/historical"
    params = {
        "assetclass": asset_class,
        "fromdate": start,
        "todate": end,
        "limit": "9999",
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": f"https://www.nasdaq.com/market-activity/{asset_class}/{symbol.lower()}/historical",
        "User-Agent": "Mozilla/5.0",
    }
    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", {}).get("tradesTable", {}).get("rows", [])
    if not rows:
        raise RuntimeError("Nasdaq returned no rows")
    return payload


def normalize_nasdaq(payload: dict, symbol: str) -> pd.DataFrame:
    rows = payload.get("data", {}).get("tradesTable", {}).get("rows", [])
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    out = pd.DataFrame(
        {
            "time": pd.to_datetime(df["date"], format="%m/%d/%Y", utc=True, errors="coerce"),
            "open": df["open"].map(parse_number),
            "high": df["high"].map(parse_number),
            "low": df["low"].map(parse_number),
            "close": df["close"].map(parse_number),
            "volume": df["volume"].map(parse_number),
        }
    )
    out = out.dropna(subset=["time", "open", "high", "low", "close"])
    out = out[(out["open"] > 0) & (out["high"] > 0) & (out["low"] > 0) & (out["close"] > 0)]
    out = out[
        (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    ]
    out = out.drop_duplicates(subset=["time"]).sort_values("time")
    out["spread"] = 0.0
    out["source"] = "nasdaq"
    out["symbol"] = symbol
    out["timeframe"] = "D1"
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


def parse_number(value: object) -> float:
    text = str(value).strip()
    if text in {"", "N/A", "nan", "None"}:
        return 0.0
    text = text.replace("$", "").replace(",", "")
    return float(text)


def failed_log_row(symbol: str, exc: object) -> dict:
    return {
        "symbol": symbol,
        "source": "nasdaq",
        "timeframe": "D1",
        "raw_path": "",
        "processed_path": "",
        "bar_count": 0,
        "start": "",
        "end": "",
        "status": "failed",
        "error": str(exc),
    }


def merge_metadata(path: Path, rows: list[dict], key: str) -> None:
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path)
        if not new.empty:
            combined = pd.concat([old, new], ignore_index=True, sort=False)
        else:
            combined = old
    else:
        combined = new
    if not combined.empty and key in combined.columns:
        if "status" in combined.columns:
            combined["_ok_rank"] = combined["status"].eq("ok").astype(int)
            combined = combined.sort_values([key, "_ok_rank"])
            combined = combined.drop(columns=["_ok_rank"])
        combined = combined.drop_duplicates(subset=[key], keep="last")
    combined.to_csv(path, index=False)


if __name__ == "__main__":
    main()
