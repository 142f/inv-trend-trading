from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import time
from typing import Mapping

import pandas as pd

from .config import load_instruments
from .models import DownloadRequest, Manifest, SurvivorshipBiasError, path_text, utc_now
from .processing import RULE_VERSION, assess_quality, normalize_bars
from .providers import BarsProvider, QqqHoldingsCsvProvider
from .storage import DataLake, sha256_file

_DEFAULT_ROOT = Path("data")


class HistoricalDataService:
    def __init__(
        self,
        root: str | Path = _DEFAULT_ROOT,
        instruments: Mapping[str, object] | None = None,
        providers: Mapping[str, BarsProvider] | None = None,
    ) -> None:
        self.lake = DataLake(root)
        self.instruments = dict(instruments or load_instruments())
        self.providers = dict(providers or {})

    def ingest(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        retries: int = 3,
    ) -> Manifest:
        instrument = self.instruments[symbol]
        start = _aware_utc(start)
        end = _aware_utc(end)
        if instrument.earliest_valid_date:
            start = max(start, pd.Timestamp(instrument.earliest_valid_date, tz="UTC").to_pydatetime())
        if start > end:
            raise ValueError("requested range ends before verified instrument history")
        names = (instrument.primary_source,) + instrument.fallback_sources
        result = None
        errors: list[str] = []
        for name in names:
            provider = self.providers.get(name)
            if provider is None:
                errors.append(f"{name}: provider not registered")
                continue
            for attempt in range(retries):
                try:
                    result = provider.fetch(DownloadRequest(instrument, timeframe, start, end))
                    break
                except Exception as exc:  # provider/network boundary
                    errors.append(f"{name} attempt {attempt + 1}: {exc}")
                    if attempt + 1 < retries:
                        time.sleep(min(2**attempt, 4))
            if result is not None:
                break
        if result is None:
            raise RuntimeError("all configured providers failed: " + " | ".join(errors))

        raw = result.frame.copy()
        raw_bytes = pd.util.hash_pandas_object(raw, index=True).values.tobytes()
        run_id = hashlib.sha256(
            result.source.encode() + symbol.encode() + timeframe.encode() + raw_bytes
        ).hexdigest()[:24]
        raw_path = self.lake.write_raw(raw, instrument.asset_class, symbol, run_id)
        normalized = normalize_bars(raw, instrument, timeframe, result.source)
        quality = assess_quality(normalized, instrument, timeframe)
        normalized_paths = self.lake.write_normalized(
            normalized.clean, instrument.asset_class, symbol, timeframe
        )
        if not normalized.quarantine.empty:
            self.lake.write_frame(
                normalized.quarantine,
                Path("quarantine") / symbol / timeframe / f"{run_id}.parquet",
            )
        quality_path = self.lake.write_json(
            quality.to_dict(), Path("quality_reports") / symbol / timeframe / f"{run_id}.json"
        )
        all_paths = [raw_path, *normalized_paths, quality_path]
        frame = normalized.clean
        manifest = Manifest(
            run_id, result.source, symbol, result.actual_symbol, start.isoformat(), end.isoformat(),
            frame["timestamp"].min().isoformat() if len(frame) else None,
            frame["timestamp"].max().isoformat() if len(frame) else None,
            timeframe, len(frame), [path_text(p) for p in all_paths],
            {path_text(p): sha256_file(p) for p in all_paths}, utc_now().isoformat(),
            result.license, _missing_intervals(quality), len(normalized.audit), RULE_VERSION,
            quality.backtest_suitable, {**result.metadata, "provider_failures": errors},
        )
        manifest_path = self.lake.write_json(
            manifest.to_dict(), Path("manifests") / symbol / timeframe / f"{run_id}.json"
        )
        self.lake.catalog(run_id, symbol, timeframe, result.source, manifest_path)
        return manifest

    def update(
        self,
        symbol: str,
        timeframe: str,
        *,
        end: datetime | None = None,
        overlap_bars: int = 5,
    ) -> Manifest:
        instrument = self.instruments[symbol]
        try:
            existing = self.lake.read_bars(symbol, timeframe)
            last = existing["timestamp"].max().to_pydatetime()
            delta = {"D1": timedelta(days=1), "H4": timedelta(hours=4), "H1": timedelta(hours=1)}[
                timeframe
            ]
            start = last - overlap_bars * delta
        except FileNotFoundError:
            if not instrument.earliest_valid_date:
                raise ValueError("cold download requires a configured earliest_valid_date")
            start = pd.Timestamp(instrument.earliest_valid_date, tz="UTC").to_pydatetime()
        return self.ingest(symbol, timeframe, start, end or utc_now())

    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        frame = self.lake.read_bars(symbol, timeframe)
        frame = frame.loc[frame["is_complete"].astype(bool)].copy()
        if start is not None:
            frame = frame.loc[frame["timestamp"] >= pd.Timestamp(_aware_utc(start))]
        if end is not None:
            frame = frame.loc[frame["timestamp"] <= pd.Timestamp(_aware_utc(end))]
        if adjusted and self.instruments[symbol].asset_class == "equity":
            factor = frame["adjusted_close"] / frame["close"]
            for column in ("open", "high", "low", "close"):
                frame[f"unadjusted_{column}"] = frame[column]
                frame[column] = frame[column] * factor
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        reports = sorted(
            (self.lake.root / "quality_reports" / symbol / timeframe).glob("*.json")
        )
        frame.attrs.update({
            "symbol": symbol, "timeframe": timeframe,
            "adjustment": self.instruments[symbol].adjustment_policy if adjusted else "none",
            "data_sources": sorted(frame["data_source"].dropna().unique().tolist()),
            "quality_report": str(reports[-1]) if reports else None,
        })
        return frame.reset_index(drop=True)

    def update_qqq_holdings(
        self, provider: QqqHoldingsCsvProvider, snapshot_date: str | None = None
    ) -> Path:
        frame = provider.fetch(snapshot_date)
        dates = frame["snapshot_date"].unique()
        if len(dates) != 1:
            raise ValueError("one holdings file must represent exactly one snapshot date")
        relative = (
            Path("reference") / "qqq_holdings" / f"snapshot_date={dates[0]}" / "top20.parquet"
        )
        return self.lake.write_frame(frame, relative)

    def load_qqq_holdings(self, as_of: str | datetime | None = None) -> pd.DataFrame:
        paths = list((self.lake.root / "reference" / "qqq_holdings").glob(
            "snapshot_date=*/top20.parquet"
        ))
        if not paths:
            raise FileNotFoundError("no QQQ holdings snapshots available")
        snapshots = sorted((p.parent.name.split("=", 1)[1], p) for p in paths)
        if as_of is None:
            selected = snapshots[-1]
        else:
            target = pd.Timestamp(as_of).date().isoformat()
            eligible = [item for item in snapshots if item[0] <= target]
            if not eligible:
                raise SurvivorshipBiasError(
                    f"no QQQ holdings snapshot on or before {target}; current holdings cannot "
                    "be substituted for a historical backtest"
                )
            selected = eligible[-1]
        frame = pd.read_parquet(selected[1])
        frame.attrs["snapshot_date"] = selected[0]
        frame.attrs["survivorship_bias_safe"] = True
        return frame


def load_bars(
    symbol: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    adjusted: bool = True,
    *,
    root: str | Path = _DEFAULT_ROOT,
) -> pd.DataFrame:
    return HistoricalDataService(root).load_bars(symbol, timeframe, start, end, adjusted)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


def _missing_intervals(report: object) -> list[str]:
    if not getattr(report, "missing_count", 0):
        return []
    return [f"{report.missing_count} expected bars missing; see quality report"]
