from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Dict, List

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.institutional_report import build_institutional_metrics, metrics_to_dict
from config.loader import load_settings
from data.aggregator import aggregate_daily_frames


TF_RULES = {
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1D",
}


def _parse_utc_datetime(text: str) -> pd.Timestamp:
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    ts = pd.Timestamp(t)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run offline backtest from Binance Vision monthly 15m zip archives")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--cache-dir", default="data_cache/binance_vision/futures_um/monthly/15m")
    p.add_argument("--period-years", nargs="*", type=float, default=[1, 2, 3, 5])
    p.add_argument("--end", default="2026-03-01T00:00:00Z")
    p.add_argument("--warmup-days", type=float, default=45)
    p.add_argument("--initial-equity", type=float, default=10000.0)
    p.add_argument("--output-dir", default="artifacts/backtest_binance_archive_current")
    return p.parse_args()


def _read_zip_15m(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])
        with zf.open(names[0]) as f:
            raw = pd.read_csv(f, header=None)

    if raw.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])

    cols = {0: "open_ts_ms", 1: "open", 2: "high", 3: "low", 4: "close", 5: "volume"}
    raw = raw.rename(columns=cols)
    need = ["open_ts_ms", "open", "high", "low", "close", "volume"]
    df = raw[need].copy()
    df["open_ts_ms"] = pd.to_numeric(df["open_ts_ms"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=need)
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])

    df["open_ts"] = pd.to_datetime(df["open_ts_ms"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("open_ts").sort_index()
    out = df[["open", "high", "low", "close", "volume"]].astype(float)
    out["close_ts"] = out.index + pd.Timedelta(minutes=15)
    return out


def _load_symbol_15m(cache_dir: Path, symbol: str, fetch_start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    files = sorted(cache_dir.glob(f"{symbol}-15m-*.zip"))
    if not files:
        raise RuntimeError(f"No archive files found for {symbol} in {cache_dir}")

    chunks: List[pd.DataFrame] = []
    for fp in files:
        chunk = _read_zip_15m(fp)
        if chunk.empty:
            continue
        # quick range skip
        if chunk.index.max() < fetch_start or chunk.index.min() >= end:
            continue
        chunks.append(chunk)

    if not chunks:
        raise RuntimeError(f"No usable 15m bars for {symbol} in requested range")

    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[(df.index >= fetch_start) & (df.index < end)]
    if df.empty:
        raise RuntimeError(f"No bars after range filter for {symbol}")
    return df


def _resample_from_15m(df15: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = TF_RULES[tf]
    if tf == "15m":
        return df15.copy()
    out = (
        df15.resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
    )
    if out.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])
    out["close_ts"] = out.index + pd.tseries.frequencies.to_offset(rule)
    return out


def _build_dataset(settings, cache_dir: Path, fetch_start: pd.Timestamp, end: pd.Timestamp) -> Dict[str, Dict[str, pd.DataFrame]]:
    dataset: Dict[str, Dict[str, pd.DataFrame]] = {}
    for symbol in settings.strategy.symbols:
        fr15 = _load_symbol_15m(cache_dir=cache_dir, symbol=symbol, fetch_start=fetch_start, end=end)
        first_open = fr15.index.min()
        if first_open > fetch_start:
            raise RuntimeError(
                f"Insufficient archive history for {symbol}: first_open={first_open.isoformat()} > requested_start={fetch_start.isoformat()}"
            )

        frames = {
            "15m": fr15,
            "30m": _resample_from_15m(fr15, "30m"),
            "1h": _resample_from_15m(fr15, "1h"),
            "2h": _resample_from_15m(fr15, "2h"),
            "4h": _resample_from_15m(fr15, "4h"),
            "1d": _resample_from_15m(fr15, "1d"),
        }
        frames = aggregate_daily_frames(frames)
        dataset[symbol] = frames
    return dataset


def _save_outputs(out_dir: Path, equity: pd.Series, fills: list, metrics: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    fills_df = pd.DataFrame([f.__dict__ for f in fills])
    fills_path = out_dir / "fills.csv"
    if not fills_df.empty:
        fills_df.to_csv(fills_path, index=False)
    else:
        pd.DataFrame(columns=["timestamp", "symbol", "side", "action", "price", "quantity", "notional", "fee", "slippage_cost", "reason"]).to_csv(fills_path, index=False)
    eq_path = out_dir / "equity.csv"
    equity.to_csv(eq_path, header=True)
    (out_dir / "institutional_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    args = _args()
    settings = load_settings(args.config)
    if args.symbols:
        settings.strategy.symbols = args.symbols

    cache_dir = Path(args.cache_dir)
    end_ts = _parse_utc_datetime(args.end)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for y in args.period_years:
        start_ts = end_ts - pd.DateOffset(months=int(float(y)*12))
        fetch_start = start_ts - pd.Timedelta(days=args.warmup_days)
        tag = f"{y}y"
        print(f"[RUN] {tag}: {start_ts.isoformat()} -> {end_ts.isoformat()} (warmup from {fetch_start.isoformat()})")

        dataset = _build_dataset(settings=settings, cache_dir=cache_dir, fetch_start=fetch_start, end=end_ts)
        engine = BacktestEngine(
            settings=settings,
            dataset=dataset,
            initial_equity=args.initial_equity,
            trade_start_ts=start_ts,
        )
        result = engine.run()

        fills_df = pd.DataFrame([f.__dict__ for f in result.fills])
        metrics = build_institutional_metrics(
            equity=result.equity_curve,
            fills=fills_df,
            initial_equity=args.initial_equity,
        )
        metrics_dict = metrics_to_dict(metrics)

        out_dir = out_root / tag
        _save_outputs(out_dir=out_dir, equity=result.equity_curve, fills=result.fills, metrics=metrics_dict)

        rows.append(
            {
                "period": tag,
                "trade_start": str(start_ts),
                "trade_end": str(end_ts),
                "final_equity": metrics_dict["final_equity"],
                "total_return": metrics_dict["total_return"],
                "max_drawdown": metrics_dict["max_drawdown"],
                "sharpe_rf0": metrics_dict["sharpe_rf0"],
                "sortino_rf0": metrics_dict["sortino_rf0"],
                "trade_count": metrics_dict["trade_count"],
                "win_rate": metrics_dict["win_rate"],
                "profit_factor": metrics_dict["profit_factor"],
                "avg_net_pnl_per_trade": metrics_dict["avg_net_pnl_per_trade"],
                "fills_path": str((out_dir / "fills.csv")),
                "equity_path": str((out_dir / "equity.csv")),
                "institutional_metrics_path": str((out_dir / "institutional_metrics.json")),
            }
        )

    summary_path = out_root / "summary.csv"
    pd.DataFrame(rows).to_csv(summary_path, index=False)
    print(f"[DONE] summary: {summary_path}")


if __name__ == "__main__":
    main()
