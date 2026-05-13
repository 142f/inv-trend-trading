"""Export standardized data artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd


def ensure_processed_layout(root: str | Path) -> dict[str, Path]:
    base = Path(root)
    paths = {
        "root": base,
        "raw_index": base / "raw_index",
        "cleaned": base / "cleaned",
        "merged": base / "merged",
        "backtest_ready": base / "backtest_ready",
        "metadata": base / "metadata",
        "logs": base / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def export_csv(df: pd.DataFrame, path: str | Path) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    return out_path


def export_json(payload: object, path: str | Path) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out_path


def export_metadata_rows(rows: Iterable[dict], path: str | Path) -> Path:
    return export_csv(pd.DataFrame(list(rows)), path)
