"""Presentation-only terminal and self-contained report helpers.

Keep public helper imports lazy.  Daily artifact composition imports the
canonical renderer while application compatibility modules are still loading;
eagerly importing the legacy HTML facade here would re-enter that application
package before its composition root is complete.
"""

__all__ = ["write_backtest_report", "write_daily_report"]


def __getattr__(name: str):
    """Preserve the historical package-level functions without eager imports."""

    if name == "write_daily_report":
        from .daily_html import write_daily_report

        return write_daily_report
    if name == "write_backtest_report":
        from .backtest_html import write_backtest_report

        return write_backtest_report
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
