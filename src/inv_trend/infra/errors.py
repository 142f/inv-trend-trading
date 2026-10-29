"""Shared failure vocabulary for the data and storage packages."""

from __future__ import annotations


class DataLineageError(RuntimeError):
    """Raised when a required immutable lineage artifact is missing or inconsistent.

    Defined here, below both ``data`` and ``storage``, so catalog failures stay
    catchable as lineage failures while ``storage`` no longer imports ``data``.
    """


class CatalogBackendConflictError(DataLineageError):
    """More than one metadata authority is present in a data root."""


class CatalogAccessError(DataLineageError):
    """An owned catalog cannot be opened, queried, or committed reliably."""
