"""Translate failures only within an explicitly owned catalog connection."""
from contextlib import contextmanager

from .models import CatalogAccessError


@contextmanager
def catalog_access(database_errors, constraint_errors):
    try:
        yield
    except database_errors as exc:
        # Immutable/unique constraint violations remain their original contract.
        if isinstance(exc, constraint_errors):
            raise
        raise CatalogAccessError(str(exc)) from exc
