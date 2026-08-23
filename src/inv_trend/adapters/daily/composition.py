"""Concrete composition for D1 artifact publication.

This is the intentionally narrow place where the filesystem artifact adapter
is paired with the observability HTML adapter.  ``artifact_publisher`` itself
only knows the renderer port, so tests and alternate deployments may inject a
different presentation implementation without changing artifact semantics.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from inv_trend.observability.daily import DailyReportRendererAdapter

from .artifact_publisher import DailyRunArtifactWriter

if TYPE_CHECKING:
    from inv_trend.application.daily.ports import DailyReportRendererPort


def create_daily_report_renderer() -> "DailyReportRendererPort":
    """Build the standard presentation adapter at the outer composition edge."""

    return DailyReportRendererAdapter()


def create_daily_run_artifact_writer(
    artifact_root: str | Path,
    *,
    signal_log_root: str | Path,
    renderer: "DailyReportRendererPort | None" = None,
) -> DailyRunArtifactWriter:
    """Build the legacy-compatible filesystem publisher with HTML support."""

    return DailyRunArtifactWriter(
        artifact_root,
        signal_log_root=signal_log_root,
        renderer=renderer or create_daily_report_renderer(),
    )


__all__ = ["create_daily_report_renderer", "create_daily_run_artifact_writer"]
