"""Cross-layer interface contracts (structural protocols and pure DTOs).

This package holds the interfaces that both ``application`` and ``adapters``
depend on.  It sits below both, so adapters can implement a port without ever
importing the application package.
"""

from .daily import (
    ArtifactPublisherPort,
    DailyArtifactPublication,
    DailyReportRendererPort,
    DailyStateRepositoryPort,
    DailyWorkflowRuntimePort,
    EligibilityPort,
    FreshnessPort,
    LineagePort,
    MarketDataPort,
    NotificationPort,
)

__all__ = [
    "ArtifactPublisherPort",
    "DailyArtifactPublication",
    "DailyReportRendererPort",
    "DailyStateRepositoryPort",
    "DailyWorkflowRuntimePort",
    "EligibilityPort",
    "FreshnessPort",
    "LineagePort",
    "MarketDataPort",
    "NotificationPort",
]
