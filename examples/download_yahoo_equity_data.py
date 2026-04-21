"""Download Yahoo Finance adjusted daily OHLC data for an equity universe."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yfinance as yf

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
    raw_root = data_dir / "raw" / "yahoo"
    processed_root = data_dir / "processed" / "yahoo" / "D1"
    metadata_root = data_dir / "metadata" / "yahoo"
    processed_root.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)

    quality_rows: list[dict] = []
    log_rows: list[dict] = []
    spec_rows: list[dict] = []
    for symbol in args.symbols:
        try:
            raw = fetch_yahoo(symbol, args.start, args.end)
            processed = normalize_yahoo(raw, symbol)
        except Exception as exc:
            log_rows.append(
                {
                    "symbol": symbol,
                    "source": "yahoo",
                    "timeframe": "D1",
                    "raw_path": "",
                    "processed_path": "",
                    "bar_count": 0,
                    "start": "",
                    "end": "",
                    "status": "failed",
                    "error": str(exc),
                }
            )
            print(f"Skipped {symbol}: {exc}")
            continue
        if processed.empty:
            log_rows.append(
                {
                    "symbol": symbol,
                    "source": "yahoo",
                    "timeframe": "D1",
                    "raw_path": "",
                    "processed_path": "",
                    "bar_count": 0,
                    "start": "",
                    "end": "",
                    "status": "failed",
                    "error": "no valid OHLC rows after normalization",
                }
            )
            print(f"Skipped {symbol}: no valid OHLC rows after normalization")
            continue
        raw_symbol_dir = raw_root / symbol
        raw_symbol_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_symbol_dir / "D1.csv"
        processed_path = processed_root / f"{symbol}.csv"
        raw.to_csv(raw_path)
        processed.to_csv(processed_path, index=False)

        quality_rows.append(data_quality(processed, symbol=symbol, timeframe="D1", point=0.01))
        log_rows.append(
            {
                "symbol": symbol,
                "source": "yahoo",
                "timeframe": "D1",
                "raw_path": str(raw_path),
                "processed_path": str(processed_path),
                "bar_count": len(processed),
                "start": processed["time"].iloc[0] if not processed.empty else "",
                "end": processed["time"].iloc[-1] if not processed.empty else "",
                "status": "ok",
                "error": "",
            }
        )
        spec_rows.append(
            {
                "name": symbol,
                "description": f"Yahoo Finance adjusted daily {symbol}",
                "source": "yahoo",
                "trade_contract_size": 1.0,
                "volume_min": 1.0,
                "volume_step": 1.0,
                "point": 0.01,
                "digits": 2,
                "cost_bps": 1.0,
                "slippage_bps": 5.0,
            }
        )

    pd.DataFrame(quality_rows).to_csv(metadata_root / "data_quality_report.csv", index=False)
    pd.DataFrame(log_rows).to_csv(metadata_root / "download_log.csv", index=False)
    pd.DataFrame(spec_rows).to_csv(metadata_root / "symbol_specs.csv", index=False)

    print("Saved processed files:")
    for row in log_rows:
        print(f"{row['symbol']}: {row['processed_path']} ({row['bar_count']} bars)")
    print(f"Metadata: {metadata_root.resolve()}")


def fetch_yahoo(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(
        symbol,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError(f"no Yahoo data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    return df


def normalize_yahoo(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    out = df.reset_index().rename(
        columns={
            "Date": "time",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    out["time"] = pd.to_datetime(out["time"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
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
    out["source"] = "yahoo"
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


if __name__ == "__main__":
    main()
