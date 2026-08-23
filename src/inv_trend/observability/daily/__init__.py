"""Daily-report presentation boundary.

New callers should import renderers from this package.  The historical
``report_renderer`` module remains an import-compatible implementation while
the report code is migrated without changing the canonical JSON contract.
"""

from .complete_renderer import (
    DailyReportRendererAdapter,
    render_complete_analyses,
    render_complete_analysis,
)
from .renderer import render_daily_dashboard, write_daily_dashboard, write_instrument_report

__all__ = [
    "DailyReportRendererAdapter",
    "render_complete_analyses",
    "render_complete_analysis",
    "render_daily_dashboard",
    "write_daily_dashboard",
    "write_instrument_report",
]
