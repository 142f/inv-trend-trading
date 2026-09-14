"""Versioned storage authority; strategy calculation stays outside this package."""

from .仓库 import Storage, StorageIntegrityError, open_storage

__all__ = ["Storage", "StorageIntegrityError", "open_storage"]
