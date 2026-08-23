"""Concrete adapters used by the modular D1 workflow."""

from .artifact_publisher import DailyArtifactPublisherAdapter, DailyRunArtifactWriter

__all__ = [
    "DailyArtifactPublisherAdapter",
    "DailyRunArtifactWriter",
    "create_daily_report_renderer",
    "create_daily_run_artifact_writer",
]


def __getattr__(name: str):
    """Expose composition helpers without eagerly importing observability."""

    if name in {"create_daily_report_renderer", "create_daily_run_artifact_writer"}:
        from .composition import create_daily_report_renderer, create_daily_run_artifact_writer

        return {
            "create_daily_report_renderer": create_daily_report_renderer,
            "create_daily_run_artifact_writer": create_daily_run_artifact_writer,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
