"""Data source discovery and inventory helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataSource:
    name: str
    source: str
    timeframe: str
    symbol: str
    path: Path
    dataset_dir: Path


def discover_processed_sources(
    dataset_dirs: list[str | Path],
    project_root: str | Path = ".",
) -> list[DataSource]:
    """Discover existing processed CSV files without changing source folders."""

    root = Path(project_root)
    sources: list[DataSource] = []
    for dataset in dataset_dirs:
        dataset_dir = (root / dataset).resolve() if not Path(dataset).is_absolute() else Path(dataset)
        processed_root = dataset_dir / "processed"
        if not processed_root.exists():
            continue
        for path in sorted(processed_root.rglob("*.csv")):
            rel = path.relative_to(processed_root)
            if len(rel.parts) < 3:
                continue
            source = rel.parts[0]
            timeframe = rel.parts[1].upper()
            symbol = path.stem
            sources.append(
                DataSource(
                    name=dataset_dir.name,
                    source=source,
                    timeframe=timeframe,
                    symbol=symbol,
                    path=path,
                    dataset_dir=dataset_dir,
                )
            )
    return sources
