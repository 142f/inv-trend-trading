"""Pure merge, alignment and provider-identity helpers for the bars pipeline.

Single responsibility: decide how fetched bars fuse with stored bars (alignment,
completeness flags, change masks) and whether a provider identity is trusted.
These are side-effect-free and unit-tested without a data lake.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

import numpy as np
import pandas as pd

from inv_trend.infra.时间 import require_aware_utc as _aware_utc
from ..models import InstrumentConfig, ProviderIdentityError, ProviderResult

def _bounded_range(instrument: InstrumentConfig, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = _aware_utc(start), _aware_utc(end)
    if instrument.earliest_valid_date:
        start = max(start, pd.Timestamp(instrument.earliest_valid_date, tz="UTC").to_pydatetime())
    if start > end:
        raise ValueError("requested range ends before verified instrument history")
    return start, end


def _aligned_positions(existing: pd.DataFrame, incoming: pd.DataFrame) -> np.ndarray:
    """``existing`` position of each incoming timestamp (``-1`` when absent).

    Same definition as :func:`_timestamp_positions` + :func:`_locate_timestamp`
    (last occurrence wins, ``NaT`` maps to the last missing row), computed with a
    hash join instead of a per-row dictionary probe.
    """

    stamps = existing["timestamp"]
    targets = incoming["timestamp"]
    keep = (~stamps.duplicated(keep="last")) & stamps.notna()
    mapped = np.full(len(targets), -1, dtype=np.int64)
    real = ~targets.isna().to_numpy()
    if keep.any() and real.any():
        # ``keep`` selects the surviving row of each timestamp, but
        # ``get_indexer`` counts inside the compacted index; the original row
        # numbers have to be carried along explicitly.
        rows = np.flatnonzero(keep.to_numpy())
        if stamps.dtype == targets.dtype:
            found = pd.Index(stamps.iloc[rows].to_numpy()).get_indexer(
                pd.Index(targets[real].to_numpy())
            )
            mapped[real] = np.where(found >= 0, rows[np.maximum(found, 0)], -1)
        else:  # exotic dtype mixes keep the exact scalar definition
            positions = dict(zip(stamps.iloc[rows].tolist(), rows))
            mapped[real] = [positions.get(value, -1) for value in targets[real].tolist()]
    missing_rows = np.flatnonzero(stamps.isna().to_numpy())
    if (~real).any() and missing_rows.size:
        mapped[~real] = int(missing_rows[-1])
    return mapped


def _complete_flags(existing: pd.DataFrame, mapped: np.ndarray) -> np.ndarray:
    """``existing.is_complete`` for each incoming row (``False`` when unmatched)."""

    flags = np.zeros(len(mapped), dtype=bool)
    if "is_complete" not in existing.columns:
        return flags
    values = existing["is_complete"].to_numpy()
    matched = mapped >= 0
    if matched.any():
        flags[matched] = values[mapped[matched]].astype(bool)
    return flags


def _numeric_equal(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Vectorised form of the numeric branch of :func:`_same_bar_value`."""

    lhs = left.astype("float64", copy=False)
    rhs = right.astype("float64", copy=False)
    with np.errstate(invalid="ignore"):
        return (np.isnan(lhs) & np.isnan(rhs)) | (np.abs(lhs - rhs) <= 1e-12)


def _vectorisable(left: pd.Series, right: pd.Series) -> bool:
    return all(
        values.dtype.kind in "biuf" for values in (left.to_numpy(), right.to_numpy())
    )


def _bar_change_mask(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    columns: list[str],
    mapped: np.ndarray,
    complete: np.ndarray,
) -> tuple[np.ndarray, dict[int, list[str]]]:
    """Which matched, completed incoming rows actually revise a stored value.

    Numeric columns are compared in one vectorised pass; only rows that look
    changed are re-checked with :func:`_same_bar_value`, so the reported field
    list keeps the exact scalar semantics (and column order) while the common
    "nothing was revised" case costs no Python loop at all.
    """

    changed = np.zeros(len(mapped), dtype=bool)
    fields: dict[int, list[str]] = {}
    rows = np.flatnonzero((mapped >= 0) & complete)
    if rows.size == 0:
        return changed, fields
    existing_positions = mapped[rows]
    different = np.zeros(rows.size, dtype=bool)
    vectorised = True
    for column in columns:
        left, right = existing[column], incoming[column]
        if not _vectorisable(left, right):
            vectorised = False
            break
        different |= ~_numeric_equal(left.to_numpy()[existing_positions], right.to_numpy()[rows])
    candidates = rows if not vectorised else rows[different]
    if candidates.size == 0:
        return changed, fields
    existing_values = {column: existing[column].tolist() for column in columns}
    incoming_values = {column: incoming[column].tolist() for column in columns}
    for incoming_position in candidates.tolist():
        existing_position = int(mapped[incoming_position])
        revised = [
            column
            for column in columns
            if not _same_bar_value(
                existing_values[column][existing_position],
                incoming_values[column][incoming_position],
            )
        ]
        if revised:
            changed[incoming_position] = True
            fields[incoming_position] = revised
    return changed, fields


def _same_bar_value(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    try:
        return bool(abs(float(left) - float(right)) <= 1e-12)
    except (TypeError, ValueError):
        return left == right


def _validate_provider_identity(
    result: ProviderResult, instrument: InstrumentConfig, *, fallback: bool, timeframe: str
) -> None:
    """Reject a fallback that explicitly reports a different economic series."""
    actual = str(result.actual_symbol).upper().replace("-", "").replace("_", "")
    expected = instrument.source_symbol.upper().replace("-", "").replace("_", "")
    if actual != expected:
        raise ProviderIdentityError(
            f"provider symbol {result.actual_symbol!r} does not match {instrument.source_symbol!r}"
        )
    # Validate explicit row identity before normalization overwrites canonical
    # columns. A provider's request echo cannot override contradictory inputs.
    expected_columns = {
        'symbol': instrument.symbol, 'instrument_id': instrument.instrument_id,
        'source_symbol': instrument.source_symbol, 'timeframe': timeframe,
        'asset_class': instrument.asset_class, 'market': instrument.market,
    }
    aliases = {'1D': 'D1', '4H': 'H4', '1H': 'H1'}
    for column, expected_value in expected_columns.items():
        if column not in result.frame:
            continue
        values = result.frame[column]
        if values.isna().any():
            raise ProviderIdentityError(f'provider row identity {column} is missing')

        def canonical_identity(value):
            token = str(value).strip()
            if column == 'source_symbol':
                return token.upper().replace('-', '').replace('_', '')
            if column == 'timeframe':
                return aliases.get(token.upper(), token.upper())
            return token.upper()

        expected_token = canonical_identity(expected_value)
        if any(canonical_identity(value) != expected_token for value in values):
            raise ProviderIdentityError(f'provider row identity {column} contradicts requested instrument/timeframe')
    if not fallback:
        return
    reported = result.metadata.get("instrument_identity", {})
    if not isinstance(reported, Mapping):
        raise ProviderIdentityError("fallback must report instrument_identity metadata")
    required = {
        "instrument_type": instrument.instrument_type,
        "venue": instrument.venue,
        "currency": instrument.currency,
        "price_basis": instrument.price_basis,
        "adjustment_method": instrument.adjustment_method,
        "session_timezone": instrument.session_timezone,
        "bar_close_rule": instrument.bar_close_rule,
        "ohlc_definition": "provider_native",
    }
    mismatches = {
        key: {"expected": expected_value, "actual": reported.get(key)}
        for key, expected_value in required.items()
        if reported.get(key) != expected_value
    }
    if mismatches:
        raise ProviderIdentityError(f"fallback instrument identity mismatch: {mismatches}")


def _fallback_eligible(exc: Exception) -> bool:
    """Only operational/provider-data failures may trigger a source switch."""
    return isinstance(
        exc,
        (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError),
    )


def _legacy_instrument(symbol: str) -> InstrumentConfig:
    return InstrumentConfig(symbol=symbol.upper(), source_symbol=symbol.upper(), instrument_id=f"{symbol.upper()}.LEGACY.CSV",
                            asset_class="legacy", market="legacy", instrument_type="spot", quote_currency="USD",
                            timezone="UTC", session="24x7", primary_source="legacy_csv")


__all__ = [
    "_aligned_positions",
    "_bar_change_mask",
    "_bounded_range",
    "_complete_flags",
    "_fallback_eligible",
    "_legacy_instrument",
    "_numeric_equal",
    "_same_bar_value",
    "_validate_provider_identity",
    "_vectorisable",
]
