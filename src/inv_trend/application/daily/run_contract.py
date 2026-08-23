"""Public compatibility result contract for a completed D1 workflow run.

The value object is intentionally independent from the legacy daily pipeline
so both the staged workflow and the historical ``DailyMarketScanService`` can
return the same public result without importing each other's implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .artifact_publication import DailyArtifactPublication


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_BOOTSTRAP_DAYS = 400


@dataclass(frozen=True)
class DailyMarketScanResult:
    """Public result returned by both legacy and staged daily entry points."""

    snapshot_path: Path
    database_path: Path
    signal_log_root: Path
    snapshot: Mapping[str, Any]
    artifact_publication: DailyArtifactPublication | None = None

    @property
    def failed_symbols(self) -> tuple[str, ...]:
        bad = {"failed", "blocked", "stale"}
        return tuple(
            str(row["symbol"])
            for row in self.snapshot.get("symbols", ())
            if isinstance(row, Mapping) and row.get("run_status") in bad
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.failed_symbols else 0


__all__ = ["BEIJING", "DEFAULT_BOOTSTRAP_DAYS", "DailyMarketScanResult"]
