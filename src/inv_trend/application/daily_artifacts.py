"""Legacy compatibility exports for daily artifact publication.

Concrete filesystem publication moved to :mod:`inv_trend.adapters.daily`.
This module remains only so existing callers can retain their historical
import path while composition roots migrate to ``ArtifactPublisherPort``.
"""

from .daily.artifact_publication import DailyArtifactPublication
from inv_trend.adapters.daily.artifact_publisher import DailyRunArtifactWriter

__all__ = ["DailyArtifactPublication", "DailyRunArtifactWriter"]
