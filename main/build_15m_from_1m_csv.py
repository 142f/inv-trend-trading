from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def _args():
    p = argparse.ArgumentParser(description="Aggregate local 1m OHLCV CSV into 15m bars")
    p.add_argument("--input", required=True, help="Input 1m csv path")
    p.add_argument("--output", required=True, help="Output 15m csv path")
    p.add_argument("--timestamp-col", default="open_ts", help="Timestamp column name in input")
    p.add_argument("--timezone", default="UTC", help="Timezone for timestamp parsing")
    return p.parse_args()


def main():
    args = _args()
    in_path = Path(args.input)
    out_path = Path(args.output)

    df = pd.read_csv(in_path)
    required = {"open", "high", "low", "close", "volume", args.timestamp_col}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    ts = pd.to_datetime(df[args.timestamp_col], utc=True)
    if args.timezone and args.timezone.upper() != "UTC":
        ts = ts.dt.tz_convert(args.timezone).dt.tz_convert("UTC")

    df = df.copy()
    df[args.timestamp_col] = ts
    df = df.sort_values(args.timestamp_col).drop_duplicates(subset=[args.timestamp_col])
    df = df.set_index(args.timestamp_col)

    agg = (
        df.resample("15min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
    )

    agg = agg.reset_index().rename(columns={args.timestamp_col: "open_ts"})
    agg["close_ts"] = agg["open_ts"] + pd.Timedelta(minutes=15)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    agg.to_csv(out_path, index=False)

    print("Build 15m from 1m done")
    print(f"rows: {len(agg)}")
    print(f"first_open_ts: {agg['open_ts'].iloc[0] if len(agg) else 'NA'}")
    print(f"last_open_ts: {agg['open_ts'].iloc[-1] if len(agg) else 'NA'}")
    print(f"output: {out_path}")


if __name__ == "__main__":
    main()
