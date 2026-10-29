"""Presentation-only terminal and self-contained report helpers.

Keep public helper imports lazy.  Daily artifact composition imports the
canonical renderer while application compatibility modules are still loading.

The historical ``write_backtest_report`` facade is gone: its deprecated
``backtest_html`` forwarder had no production or test references, and keeping it
forced this package to import the application layer.
"""

__all__ = ["write_daily_report"]


def __getattr__(name: str):
    """Preserve the historical package-level function without eager imports."""

    if name == "write_daily_report":
        from .daily_html import write_daily_report

        return write_daily_report
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
