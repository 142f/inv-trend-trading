"""Canonical market-data boundary."""

from .normalizer import apply_split_adjustment, normalize_bars
from .providers import DataProvider, InMemoryProvider

__all__ = ["DataProvider", "InMemoryProvider", "apply_split_adjustment", "normalize_bars"]
