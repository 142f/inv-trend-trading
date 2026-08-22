"""Presentation-only terminal and self-contained report helpers."""

from .daily_html import write_daily_report
from .backtest_html import write_backtest_report

__all__ = ["write_backtest_report", "write_daily_report"]