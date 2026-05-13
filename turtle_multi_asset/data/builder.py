"""Build unified standardized data artifacts from existing project data."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pandas as pd

from .cleaner import clean_ohlcv_frame
from .exporter import ensure_processed_layout, export_csv, export_json, export_metadata_rows
from .loader import load_local_csv
from .normalizer import CANONICAL_COLUMNS, normalize_ohlcv_frame
from .sources import DataSource, discover_processed_sources
from .validator import validate_alignment, validate_ohlcv_frame


DEFAULT_DATASET_DIRS = [
    "data_2010_xau_btc",
    "data_2015_xau_btc",
    "data_2020_xau_btc",
    "data_2022_xau_btc",
    "data_xau_earliest",
    "data_external_xau_btc",
    "data_external_xau_btc_xag_eth",
    "data_external_equities",
]


def build_unified_processed_data(
    dataset_dirs: list[str | Path] | None = None,
    output_dir: str | Path = "processed_data",
    project_root: str | Path = ".",
) -> dict[str, object]:
    """Create cleaned/backtest-ready files under one output directory."""

    paths = ensure_processed_layout(output_dir)
    sources = discover_processed_sources(dataset_dirs or DEFAULT_DATASET_DIRS, project_root)
    raw_index_rows = [_source_row(source) for source in sources]
    export_metadata_rows(raw_index_rows, paths["raw_index"] / "source_index.csv")

    quality_rows: list[dict] = []
    cleaned_rows: list[dict] = []
    grouped_frames: dict[tuple[str, str, str], dict[str, pd.DataFrame]] = defaultdict(dict)

    for source in sources:
        normalized = normalize_ohlcv_frame(
            load_local_csv(source.path),
            symbol=source.symbol,
            source=source.source,
            timeframe=source.timeframe,
        )
        cleaned = clean_ohlcv_frame(normalized)
        cleaned_name = data_filename(
            [source.symbol],
            cleaned,
            source.timeframe,
            "cleaned",
            prefix=source.name,
        )
        cleaned_path = export_csv(cleaned, paths["cleaned"] / source.name / cleaned_name)
        quality = validate_ohlcv_frame(cleaned, source.symbol, source.timeframe)
        quality.update(
            {
                "dataset": source.name,
                "source": source.source,
                "input_path": str(source.path),
                "cleaned_path": str(cleaned_path),
            }
        )
        quality_rows.append(quality)
        cleaned_rows.append(
            {
                "dataset": source.name,
                "source": source.source,
                "timeframe": source.timeframe,
                "symbol": source.symbol,
                "rows": len(cleaned),
                "start_date": cleaned["date"].iloc[0] if not cleaned.empty else "",
                "end_date": cleaned["date"].iloc[-1] if not cleaned.empty else "",
                "path": str(cleaned_path),
            }
        )
        grouped_frames[(source.name, source.source, source.timeframe)][source.symbol] = cleaned

    backtest_ready_rows: list[dict] = []
    alignment_rows: list[dict] = []
    for (dataset, source_name, timeframe), frames in grouped_frames.items():
        merged = pd.concat(frames.values(), ignore_index=True, sort=False)
        merged = merged[CANONICAL_COLUMNS].sort_values(["symbol", "date"]).reset_index(drop=True)
        filename = data_filename(
            list(frames),
            merged,
            timeframe,
            "backtest_ready",
            prefix=f"{dataset}_{source_name}",
        )
        out_path = export_csv(merged, paths["backtest_ready"] / dataset / filename)
        alignment = validate_alignment(frames)
        alignment.update(
            {
                "dataset": dataset,
                "source": source_name,
                "timeframe": timeframe,
                "path": str(out_path),
            }
        )
        alignment_rows.append(alignment)
        backtest_ready_rows.append(
            {
                "dataset": dataset,
                "source": source_name,
                "timeframe": timeframe,
                "symbols": " ".join(sorted(frames)),
                "symbol_count": len(frames),
                "rows": len(merged),
                "start_date": merged["date"].min() if not merged.empty else "",
                "end_date": merged["date"].max() if not merged.empty else "",
                "path": str(out_path),
            }
        )

    export_metadata_rows(cleaned_rows, paths["metadata"] / "cleaned_manifest.csv")
    export_metadata_rows(backtest_ready_rows, paths["metadata"] / "backtest_ready_manifest.csv")
    export_metadata_rows(quality_rows, paths["logs"] / "validation_report.csv")
    export_metadata_rows(alignment_rows, paths["logs"] / "alignment_report.csv")
    export_json(
        {
            "output_dir": str(Path(output_dir)),
            "source_count": len(sources),
            "cleaned_file_count": len(cleaned_rows),
            "backtest_ready_file_count": len(backtest_ready_rows),
            "field_schema": CANONICAL_COLUMNS,
            "alignment_policy": "asset_independent_no_forward_fill",
        },
        paths["metadata"] / "build_config.json",
    )
    return {
        "source_count": len(sources),
        "cleaned_file_count": len(cleaned_rows),
        "backtest_ready_file_count": len(backtest_ready_rows),
        "output_dir": str(Path(output_dir)),
    }


def data_filename(
    symbols: list[str],
    df: pd.DataFrame,
    timeframe: str,
    status: str,
    prefix: str = "",
) -> str:
    symbol_part = asset_range_name(symbols)
    years = date_year_range(df)
    name_parts = [sanitize(part) for part in [prefix, symbol_part, years, timeframe.lower(), status] if part]
    return "_".join(name_parts) + ".csv"


def asset_range_name(symbols: list[str]) -> str:
    clean = [sanitize(symbol).lower() for symbol in sorted(symbols)]
    if len(clean) <= 4:
        return "_".join(clean)
    return f"{len(clean)}assets"


def date_year_range(df: pd.DataFrame) -> str:
    if df.empty:
        return "empty"
    dates = pd.to_datetime(df["date"], utc=True)
    return f"{int(dates.min().year)}_{int(dates.max().year)}"


def sanitize(value: object) -> str:
    text = str(value).strip().lower()
    out = []
    for char in text:
        out.append(char if char.isalnum() else "_")
    return "_".join(part for part in "".join(out).split("_") if part)


def _source_row(source: DataSource) -> dict[str, object]:
    return {
        "dataset": source.name,
        "source": source.source,
        "timeframe": source.timeframe,
        "symbol": source.symbol,
        "path": str(source.path),
        "dataset_dir": str(source.dataset_dir),
    }
