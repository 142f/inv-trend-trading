"""Asset and rule profile exports.

This package replaces the old ``profiles.py`` compatibility module. It keeps
the strategy rule exports available while adding centralized asset profiles.
"""

from .asset_profiles import build_asset_specs, infer_asset_fields
from ..strategy.profiles import (
    DEFAULT_CLUSTER_1N_RISK_PCT,
    DEFAULT_CLUSTER_LEVERAGE,
    classic_bar_rules,
    h4_daily_equivalent_rules,
    turtle_rules,
)

__all__ = [
    "DEFAULT_CLUSTER_1N_RISK_PCT",
    "DEFAULT_CLUSTER_LEVERAGE",
    "build_asset_specs",
    "classic_bar_rules",
    "h4_daily_equivalent_rules",
    "infer_asset_fields",
    "turtle_rules",
]
