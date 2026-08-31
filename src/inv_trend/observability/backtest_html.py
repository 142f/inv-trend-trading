"""Deprecated forwarding module for the unified backtest report renderer."""

import warnings
from typing import Any

from inv_trend.application.backtest.legacy_report import (
    write_backtest_report as _write_backtest_report,
)


def write_backtest_report(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "inv_trend.observability.write_backtest_report is deprecated; use the "
        "unified backtest artifact/reporting API",
        DeprecationWarning,
        stacklevel=2,
    )
    return _write_backtest_report(*args, **kwargs)

__all__ = ["write_backtest_report"]
