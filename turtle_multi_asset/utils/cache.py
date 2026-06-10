"""Cache helpers for deterministic DataFrame computations."""

from __future__ import annotations

from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, TypeVar

import pandas as pd

F = TypeVar("F", bound=Callable[..., pd.DataFrame])


def _dataframe_fingerprint(frame: pd.DataFrame) -> tuple[Any, ...]:
    values_hash = int(pd.util.hash_pandas_object(frame, index=True).sum())
    return (frame.shape, tuple(frame.columns), str(frame.index.dtype), values_hash)


def cached_dataframe(maxsize: int = 32) -> Callable[[F], F]:
    """Cache DataFrame-returning functions without mutating cached values."""

    def decorator(func: F) -> F:
        cache: OrderedDict[tuple[Any, ...], pd.DataFrame] = OrderedDict()

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> pd.DataFrame:
            key_parts: list[Any] = [func.__module__, func.__qualname__]
            for arg in args:
                if isinstance(arg, pd.DataFrame):
                    key_parts.append(_dataframe_fingerprint(arg))
                else:
                    key_parts.append(arg)
            for name, value in sorted(kwargs.items()):
                key_parts.append(name)
                key_parts.append(_dataframe_fingerprint(value) if isinstance(value, pd.DataFrame) else value)
            key = tuple(key_parts)
            if key in cache:
                cache.move_to_end(key)
                return cache[key].copy()
            result = func(*args, **kwargs)
            cache[key] = result.copy()
            if len(cache) > maxsize:
                cache.popitem(last=False)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
