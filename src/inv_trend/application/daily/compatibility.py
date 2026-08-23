"""Compatibility composition for callers that still construct ``DailyWorkflow``.

The workflow itself depends only on its ports.  This tiny boundary keeps the
historical, eager ``DailyMarketScanService`` construction path available for
in-process callers while the CLI uses the deferred stage runtime instead.
"""

from __future__ import annotations

from typing import Any

from .ports import DailyWorkflowRuntimePort


def create_legacy_runtime(**kwargs: Any) -> DailyWorkflowRuntimePort:
    """Build the established runtime outside the workflow orchestration module."""

    from .legacy_runtime_adapter import LegacyDailyRuntimeAdapter

    return LegacyDailyRuntimeAdapter(**kwargs)


__all__ = ["create_legacy_runtime"]
