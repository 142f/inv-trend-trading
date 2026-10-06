"""Immutable ETF membership snapshots and their point-in-time selection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .models import HoldingsSnapshot, InstrumentConfig, SurvivorshipBiasError, path_text
from .providers import HoldingsCsvProvider, QqqHoldingsCsvProvider


def update_holdings(
    lake, provider: HoldingsCsvProvider, snapshot_date: str | None = None, *, top: int = 50
) -> HoldingsSnapshot:
    frame = provider.fetch(snapshot_date, top=top)
    if len(frame) < top:
        raise ValueError(f"holdings snapshot requires at least {top} unique rows, got {len(frame)}")
    dates = frame["snapshot_date"].unique()
    if len(dates) != 1:
        raise ValueError("one holdings file must represent exactly one snapshot date")
    fund = str(frame["fund"].iloc[0]).upper()
    relative = (
        Path("reference")
        / "universes"
        / "etf_holdings"
        / f"fund={fund}"
        / f"snapshot_date={dates[0]}"
        / f"top{top}.parquet"
    )
    path = lake.write_frame(frame, relative)
    return HoldingsSnapshot(fund, str(dates[0]), top, path_text(path, lake.root), len(frame))


def load_holdings(
    lake,
    funds: tuple[str, ...] = ("QQQ", "SPY"),
    as_of: str | datetime | None = None,
    *,
    top: int = 50,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for requested in funds:
        fund = requested.upper()
        base = lake.root / "reference" / "universes" / "etf_holdings" / f"fund={fund}"
        paths = list(base.glob(f"snapshot_date=*/top{top}.parquet"))
        if fund == "QQQ" and not paths and top == 20:
            paths = list(
                (lake.root / "reference" / "universes" / "qqq_holdings").glob(
                    "snapshot_date=*/top20.parquet"
                )
            )
        snapshots = sorted((p.parent.name.split("=", 1)[1], p) for p in paths)
        target = (
            snapshots[-1]
            if snapshots and as_of is None
            else next(
                (
                    item
                    for item in reversed(snapshots)
                    if item[0] <= pd.Timestamp(as_of).date().isoformat()
                ),
                None,
            )
        )
        if target is None:
            raise SurvivorshipBiasError(f"no {fund} holdings snapshot on or before requested date")
        frame = pd.read_parquet(target[1]).head(top).copy()
        if "fund" not in frame:
            frame["fund"] = fund
        if "rank" not in frame:
            frame["rank"] = range(1, len(frame) + 1)
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    rows = []
    for symbol, group in combined.groupby("symbol", sort=False):
        rows.append(
            {
                "symbol": symbol,
                "funds": ",".join(group["fund"].astype(str)),
                "ranks": ",".join(f"{f}:{int(r)}" for f, r in zip(group["fund"], group["rank"])),
                "weights": ",".join(
                    f"{f}:{float(w):.6g}" for f, w in zip(group["fund"], group["weight"])
                ),
                "snapshot_dates": ",".join(
                    f"{f}:{d}" for f, d in zip(group["fund"], group["snapshot_date"])
                ),
            }
        )
    result = pd.DataFrame(rows)
    result.attrs["funds"] = tuple(f.upper() for f in funds)
    return result


def update_qqq_holdings(
    lake, provider: QqqHoldingsCsvProvider, snapshot_date: str | None = None
) -> Path:
    frame = provider.fetch(snapshot_date, top=20)
    dates = frame["snapshot_date"].unique()
    path = lake.write_frame(
        frame,
        Path("reference")
        / "universes"
        / "qqq_holdings"
        / f"snapshot_date={dates[0]}"
        / "top20.parquet",
    )
    return path


def load_qqq_holdings(lake, as_of: str | datetime | None = None) -> pd.DataFrame:
    paths = list(
        (lake.root / "reference" / "universes" / "qqq_holdings").glob(
            "snapshot_date=*/top20.parquet"
        )
    )
    if not paths:
        raise FileNotFoundError("no QQQ holdings snapshots available")
    snapshots = sorted((p.parent.name.split("=", 1)[1], p) for p in paths)
    target = (
        snapshots[-1]
        if as_of is None
        else next(
            (
                item
                for item in reversed(snapshots)
                if item[0] <= pd.Timestamp(as_of).date().isoformat()
            ),
            None,
        )
    )
    if target is None:
        raise SurvivorshipBiasError("no holdings snapshot on or before requested date")
    frame = pd.read_parquet(target[1])
    frame.attrs.update({"snapshot_date": target[0], "survivorship_bias_safe": True})
    return frame


def holding_instrument(symbol: str) -> InstrumentConfig:
    symbol = symbol.upper()
    return InstrumentConfig(
        symbol=symbol,
        source_symbol=symbol.replace(".", "-"),
        instrument_id=f"{symbol}.US.EQUITY",
        asset_class="equity",
        market="xnas",
        venue="xnas",
        instrument_type="equity",
        quote_currency="USD",
        currency="USD",
        timezone="America/New_York",
        session_timezone="America/New_York",
        session="regular",
        primary_source="yahoo_chart",
        fallback_sources=("alpha_vantage",),
        earliest_valid_date="1970-01-01",
        adjustment_policy="provider_adjusted_close",
        adjustment_method="provider_adjusted_close",
        bar_close_rule="provider_native",
        license="Yahoo Finance terms",
        revision_overlap_bars=10,
    )
