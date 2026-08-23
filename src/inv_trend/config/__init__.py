"""Versioned, package-owned configuration resources.

The public helpers in this package deliberately expose mappings rather than
application or adapter models.  Each boundary remains responsible for
validating its own runtime model, while all of them can share the same
versioned YAML source.
"""

from .strategy import (
    CANONICAL_STRATEGY_SCHEMA_VERSION,
    canonical_strategy_config_path,
    canonical_to_application_mapping,
    canonical_to_backtest_mapping,
    canonical_to_detector_mapping,
    is_canonical_strategy_mapping,
    load_strategy_mapping,
)

__all__ = [
    "CANONICAL_STRATEGY_SCHEMA_VERSION",
    "canonical_strategy_config_path",
    "canonical_to_application_mapping",
    "canonical_to_backtest_mapping",
    "canonical_to_detector_mapping",
    "is_canonical_strategy_mapping",
    "load_strategy_mapping",
]
