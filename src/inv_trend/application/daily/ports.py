"""Compatibility re-export of the cross-layer interface contracts.

The protocols and the pure :class:`DailyArtifactPublication` DTO now live in
``inv_trend.ports.daily`` so that adapters can implement them without importing
``inv_trend.application``.  This module preserves the historical import path
``inv_trend.application.daily.ports`` for existing callers.
"""

from __future__ import annotations

from inv_trend.ports.daily import (
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
