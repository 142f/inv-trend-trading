"""Application use case for retry-safe publication of D1 result artifacts.

The application layer owns the publication contract but not filesystem, CSV,
or HTML details.  A concrete :class:`ArtifactPublisherPort` is injected by a
composition root and is responsible for idempotently completing a partially
published run when the same ``run_id`` is retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .ports import ArtifactPublisherPort

if TYPE_CHECKING:
    from ..daily_artifacts import DailyRunArtifactWriter


@dataclass(frozen=True)
class DailyArtifactPublication:
    """Locations published for a single immutable daily-run snapshot."""

    run_directory: Path
    compatibility_json: Path
    compatibility_html: Path | None
    complete_results: Mapping[str, Path]


class ArtifactPublicationService:
    """Delegate authoritative publication to an injected, retry-safe adapter."""

    def __init__(self, publisher: ArtifactPublisherPort) -> None:
        self._publisher = publisher

    def publish(
        self,
        snapshot: Mapping[str, Any],
        *,
        render_html: bool = True,
    ) -> DailyArtifactPublication:
        """Publish or resume the immutable run identified by ``snapshot.run_id``."""

        _validate_snapshot_identity(snapshot)
        publication = self._publisher.publish(snapshot, render_html=render_html)
        if not isinstance(publication, DailyArtifactPublication):
            raise TypeError(
                "ArtifactPublisherPort.publish must return DailyArtifactPublication; "
                f"got {type(publication).__name__}"
            )
        return publication

    def resume(
        self,
        snapshot: Mapping[str, Any],
        *,
        render_html: bool = True,
    ) -> DailyArtifactPublication:
        """Retry a prior publication without changing the business payload."""

        return self.publish(snapshot, render_html=render_html)


def _validate_snapshot_identity(snapshot: Mapping[str, Any]) -> None:
    for field in ("report_date", "run_id"):
        if not str(snapshot.get(field) or "").strip():
            raise ValueError(f"artifact publication snapshot requires non-empty {field}")


def __getattr__(name: str):
    """Keep the transitional writer import available without owning it here."""

    if name == "DailyRunArtifactWriter":
        from ..daily_artifacts import DailyRunArtifactWriter

        return DailyRunArtifactWriter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ArtifactPublicationService", "DailyArtifactPublication", "DailyRunArtifactWriter"]
