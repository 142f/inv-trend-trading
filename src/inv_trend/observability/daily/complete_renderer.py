"""Render daily reports from canonical complete-analysis JSON only.

This is a presentation adapter.  It intentionally reshapes already-produced
JSON for the historical dashboard renderer, but does not evaluate conditions,
calculate indicators, or derive a trading decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .renderer import render_daily_dashboard


def render_complete_analysis(complete_result: Mapping[str, Any]) -> str:
    """Render one authoritative ``complete_analysis_result.json`` payload."""

    return render_daily_dashboard(_dashboard_snapshot((complete_result,)))


def render_complete_analyses(
    complete_results: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> str:
    """Render an aggregate dashboard from authoritative complete payloads.

    The aggregate compatibility report is deliberately rebuilt from the same
    complete JSON documents as the per-instrument reports.  It accepts either
    a symbol-to-payload mapping or an iterable of payloads to keep filesystem
    publishers independent from the report's display ordering details.
    """

    payloads = (
        tuple(complete_results.values())
        if isinstance(complete_results, Mapping)
        else tuple(complete_results)
    )
    return render_daily_dashboard(_dashboard_snapshot(payloads))


class DailyReportRendererAdapter:
    """Adapt the canonical JSON renderer to the daily publication port.

    It deliberately has no access to files, state, data lakes, or strategy
    code.  The filesystem publisher injects this adapter at a composition
    boundary and supplies the already-written canonical JSON payloads.
    """

    def render_complete_analysis(self, complete_result: Mapping[str, Any]) -> str:
        """Render one canonical ``complete_analysis_result.json`` payload."""

        return render_complete_analysis(complete_result)

    def render_complete_analyses(
        self,
        complete_results: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """Render the flat compatibility HTML from canonical payloads."""

        return render_complete_analyses(complete_results)


def _dashboard_snapshot(complete_results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    report_date = ""
    report_summary: Mapping[str, Any] = {}
    for complete in complete_results:
        if not isinstance(complete, Mapping):
            continue
        metadata = _mapping(complete.get("metadata"))
        bundle = _mapping(complete.get("report_bundle"))
        as_of = str(metadata.get("as_of") or "")
        if not report_date:
            report_date = str(metadata.get("report_date") or as_of)[:10]
        if not report_summary:
            report_summary = _mapping(metadata.get("report_summary"))
        rows.append(
            {
                "symbol": metadata.get("symbol"),
                "instrument_id": metadata.get("instrument_id"),
                "timeframe": metadata.get("timeframe", "D1"),
                "run_status": metadata.get("run_status", "updated"),
                "data_update_result": _mapping(complete.get("data_update")),
                "strategy_screening_result": _mapping(complete.get("strategy_screening")),
                "trend_decision_result": _mapping(complete.get("trend_decision")),
                "report_bundle": bundle,
            }
        )
    return {
        "schema_version": "4",
        "report_schema_version": "5",
        "report_date": report_date,
        "symbols": rows,
        "summary": report_summary or _aggregate_summary(rows),
    }


def _aggregate_summary(rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Expose only display metadata already present in report bundles."""

    return {
        "instrument_count": sum(1 for _ in rows),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "DailyReportRendererAdapter",
    "render_complete_analyses",
    "render_complete_analysis",
]
