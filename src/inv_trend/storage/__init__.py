"""Versioned storage authority; strategy calculation stays outside this package."""

from .仓库 import Storage, StorageIntegrityError

__all__ = ["Storage", "StorageIntegrityError"]
