"""Deprecated compatibility facade for :mod:`inv_trend.application.backtest`."""

import warnings

warnings.warn(
    "inv_trend.application.strategy_backtest is deprecated; use "
    "inv_trend.application.backtest",
    DeprecationWarning,
    stacklevel=2,
)

from ..backtest import *  # noqa: F401,F403,E402
from ..backtest import __all__ as __all__  # noqa: E402
