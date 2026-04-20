"""下载、清洗并保存 MT5 OHLC 数据，便于复现实验。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from turtle_multi_asset.mt5_data import fetch_mt5_ohlc, mt5_session


SPEC_FIELDS = [
    "name",
    "description",
    "path",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "trade_contract_size",
    "volume_min",
    "volume_max",
    "volume_step",
    "point",
    "digits",
    "trade_tick_size",
    "trade_tick_value",
    "margin_initial",
    "margin_maintenance",
    "swap_long",
    "swap_short",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframe", default="H4")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--terminal-path", default=None)
    parser.add_argument("--login", type=int, default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--server", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    raw_root = data_dir / "raw" / "mt5"
    processed_root = data_dir / "processed" / "mt5" / args.timeframe.upper()
    metadata_root = data_dir / "metadata" / "mt5"
    processed_root.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)

    quality_rows: list[dict] = []
    spec_rows: list[dict] = []
    log_rows: list[dict] = []

    with mt5_session(
        path=args.terminal_path,
        login=args.login,
        password=args.password,
        server=args.server,
    ):
        import MetaTrader5 as mt5

        for symbol in args.symbols:
            raw = fetch_mt5_ohlc(
                symbol=symbol,
                timeframe=args.timeframe,
                start=args.start,
                end=args.end,
                count=args.count,
            )
            processed = normalize_ohlc(raw, symbol=symbol, timeframe=args.timeframe)
            info = mt5.symbol_info(symbol)
            point = _symbol_point(info)

            raw_symbol_dir = raw_root / symbol
            raw_symbol_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_symbol_dir / f"{args.timeframe.upper()}.csv"
            processed_path = processed_root / f"{symbol}.csv"
            raw.to_csv(raw_path)
            processed.to_csv(processed_path, index=False)

            quality = data_quality(
                processed,
                symbol=symbol,
                timeframe=args.timeframe,
                point=point,
            )
            quality_rows.append(quality)
            log_rows.append(
                {
                    "symbol": symbol,
                    "timeframe": args.timeframe.upper(),
                    "raw_path": str(raw_path),
                    "processed_path": str(processed_path),
                    "bar_count": len(processed),
                    "start": processed["time"].iloc[0] if not processed.empty else "",
                    "end": processed["time"].iloc[-1] if not processed.empty else "",
                }
            )

            if info is not None:
                spec_rows.append(symbol_info_row(info))

    pd.DataFrame(quality_rows).to_csv(metadata_root / "data_quality_report.csv", index=False)
    pd.DataFrame(spec_rows).to_csv(metadata_root / "symbol_specs.csv", index=False)
    pd.DataFrame(log_rows).to_csv(metadata_root / "download_log.csv", index=False)

    print("Saved processed files:")
    for row in log_rows:
        print(f"{row['symbol']}: {row['processed_path']} ({row['bar_count']} bars)")
    print(f"Metadata: {metadata_root.resolve()}")


def normalize_ohlc(df: pd.DataFrame, symbol: str, timeframe: str) -> pd.DataFrame:
    out = df.reset_index().copy()
    if "time" not in out.columns:
        raise ValueError("expected time index or time column")
    out["time"] = pd.to_datetime(out["time"], utc=True)
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
    for column in ["volume", "spread"]:
        if column not in out.columns:
            out[column] = 0.0
    out["source"] = "mt5"
    out["symbol"] = symbol
    out["timeframe"] = timeframe.upper()
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


def data_quality(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    market_type: str | None = None,
    point: float | None = None,
) -> dict:
    duplicate_count = int(df.duplicated(subset=["time"]).sum()) if "time" in df.columns else 0
    bad_ohlc_count = int(
        (
            (df["high"] < df["low"])
            | (df["open"] > df["high"])
            | (df["open"] < df["low"])
            | (df["close"] > df["high"])
            | (df["close"] < df["low"])
        ).sum()
    )
    market_type = market_type or infer_market_type(symbol)
    ordered_times = pd.to_datetime(df["time"], utc=True).sort_values().reset_index(drop=True)
    gaps = ordered_times.diff().dropna()
    median_gap = gaps.median() if not gaps.empty else pd.NaT
    expected_gap = timeframe_to_timedelta(timeframe)
    gap_threshold = (
        expected_gap * 1.5
        if expected_gap is not None
        else median_gap * 3
        if pd.notna(median_gap)
        else None
    )
    large_gap_count = 0
    normal_session_gap_count = 0
    abnormal_gap_count = 0
    if gap_threshold is not None:
        for idx in range(1, len(ordered_times)):
            previous = pd.Timestamp(ordered_times.iloc[idx - 1])
            current = pd.Timestamp(ordered_times.iloc[idx])
            gap = current - previous
            if gap <= gap_threshold:
                continue
            large_gap_count += 1
            if market_type == "session" and is_normal_session_gap(previous, current):
                normal_session_gap_count += 1
            else:
                abnormal_gap_count += 1

    spread = pd.to_numeric(df["spread"], errors="coerce") if "spread" in df else pd.Series(dtype=float)
    close = pd.to_numeric(df["close"], errors="coerce") if "close" in df else pd.Series(dtype=float)
    spread_price = spread * float(point) if point is not None and point > 0 else spread
    spread_bps = (spread_price / close * 10_000).replace([float("inf"), float("-inf")], pd.NA).dropna()
    return {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "market_type": market_type,
        "bar_count": len(df),
        "start_time": df["time"].iloc[0] if not df.empty else "",
        "end_time": df["time"].iloc[-1] if not df.empty else "",
        "duplicate_count": duplicate_count,
        "bad_ohlc_count": bad_ohlc_count,
        "large_gap_count": large_gap_count,
        "normal_session_gap_count": normal_session_gap_count,
        "abnormal_gap_count": abnormal_gap_count,
        "expected_gap_minutes": (
            expected_gap / pd.Timedelta(minutes=1) if expected_gap is not None else 0.0
        ),
        "median_gap_hours": (
            median_gap / pd.Timedelta(hours=1) if pd.notna(median_gap) else 0.0
        ),
        "max_gap_hours": (
            gaps.max() / pd.Timedelta(hours=1) if not gaps.empty else 0.0
        ),
        "median_spread": float(df["spread"].median()) if "spread" in df and not df.empty else 0.0,
        "max_spread": float(df["spread"].max()) if "spread" in df and not df.empty else 0.0,
        "median_spread_bps": float(spread_bps.median()) if not spread_bps.empty else 0.0,
        "p95_spread_bps": float(spread_bps.quantile(0.95)) if not spread_bps.empty else 0.0,
        "max_spread_bps": float(spread_bps.max()) if not spread_bps.empty else 0.0,
    }


def symbol_info_row(info: object) -> dict:
    return {field: getattr(info, field, None) for field in SPEC_FIELDS}


def infer_market_type(symbol: str) -> str:
    upper = symbol.upper()
    if any(token in upper for token in ("BTC", "ETH", "SOL", "LTC", "XRP", "CRYPTO")):
        return "24x7"
    return "session"


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta | None:
    key = timeframe.upper()
    if key.startswith("M") and key[1:].isdigit():
        return pd.Timedelta(minutes=int(key[1:]))
    if key.startswith("H") and key[1:].isdigit():
        return pd.Timedelta(hours=int(key[1:]))
    if key.startswith("D") and key[1:].isdigit():
        return pd.Timedelta(days=int(key[1:]))
    if key.startswith("W") and key[1:].isdigit():
        return pd.Timedelta(weeks=int(key[1:]))
    return None


def is_normal_session_gap(previous: pd.Timestamp, current: pd.Timestamp) -> bool:
    start_day = previous.normalize()
    end_day = current.normalize()
    day = start_day
    while day <= end_day:
        if day.weekday() >= 5:
            return True
        day += pd.Timedelta(days=1)
    return False


def _symbol_point(info: object | None) -> float | None:
    if info is None:
        return None
    try:
        point = float(getattr(info, "point", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return point if point > 0 else None


if __name__ == "__main__":
    main()
