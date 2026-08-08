from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .calendar import classify_missing
from .models import CANONICAL_COLUMNS, InstrumentConfig, QualityReport

RULE_VERSION = "1.1.0"
FRAME_DELTAS = {"D1": "1D", "1d": "1D", "H4": "4h", "4h": "4h", "H1": "1h", "1h": "1h"}


@dataclass
class NormalizationResult:
    clean: pd.DataFrame
    quarantine: pd.DataFrame
    audit: pd.DataFrame
    duplicate_count: int
    ohlc_anomaly_count: int
    extreme_jump_count: int
    conflicting_duplicate_count: int = 0


def normalize_bars(
    raw: pd.DataFrame,
    instrument: InstrumentConfig,
    timeframe: str,
    source: str,
    *,
    now: datetime | None = None,
) -> NormalizationResult:
    frame = raw.copy().reset_index(drop=True)
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    if "timestamp" not in frame:
        for candidate in ("date", "datetime", "time", "open_time"):
            if candidate in frame:
                frame["timestamp"] = frame[candidate]
                break
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    for column in ("open", "high", "low", "close", "volume", "adjusted_close"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "volume" not in frame:
        frame["volume"] = np.nan
    if "adjusted_close" not in frame:
        frame["adjusted_close"] = frame["close"]
    stamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize(instrument.timezone, ambiguous="NaT", nonexistent="NaT")
    frame["timestamp"] = stamps.dt.tz_convert("UTC")
    frame["_row"] = range(len(frame))

    reasons = pd.Series("", index=frame.index, dtype="object")
    reasons[frame["timestamp"].isna()] += "invalid_timestamp;"
    numeric_invalid = frame[list(required - {"timestamp"})].isna().any(axis=1)
    reasons[numeric_invalid] += "non_numeric_price;"
    ohlc_invalid = (
        (frame["high"] < frame[["open", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "close"]].min(axis=1))
        | (frame["high"] < frame["low"])
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (frame["volume"].notna() & (frame["volume"] < 0))
    )
    reasons[ohlc_invalid] += "invalid_ohlc_or_volume;"
    if instrument.earliest_valid_date:
        earliest = pd.Timestamp(instrument.earliest_valid_date, tz="UTC")
        reasons[frame["timestamp"].notna() & (frame["timestamp"] < earliest)] += (
            "before_verified_history;"
        )
    duplicate = frame["timestamp"].notna() & frame["timestamp"].duplicated(keep=False)
    quarantined_duplicates: set[int] = set()
    if duplicate.any():
        ohlc_columns = [c for c in ("open", "high", "low", "close", "adjusted_close", "volume") if c in frame]
        for timestamp, group in frame.loc[duplicate].groupby("timestamp"):
            values = group[ohlc_columns].astype(float)
            identical = (values - values.iloc[0]).abs().max().max() <= 1e-9
            if identical:
                # exact duplicate: safe to drop, record in audit
                quarantined_duplicates.update(group.index[1:].tolist())
                reasons[group.index[1:]] += "duplicate_superseded;"
            else:
                # conflicting OHLCV on the same timestamp: never silently pick one
                quarantined_duplicates.update(group.index.tolist())
                reasons[group.index] += "conflicting_duplicate;"

    invalid = reasons.ne("")
    quarantine = frame.loc[invalid].copy()
    quarantine["quarantine_reason"] = reasons[invalid]
    clean = frame.loc[~invalid].copy().sort_values("timestamp")
    now = now or datetime.now(timezone.utc)
    if "is_complete" not in clean:
        delta = pd.Timedelta(FRAME_DELTAS[timeframe])
        clean["is_complete"] = clean["timestamp"] + delta <= pd.Timestamp(now)
    else:
        clean["is_complete"] = clean["is_complete"].fillna(False).astype(bool)

    jump = clean["close"].pct_change().abs() >= 0.30
    clean["quality_flags"] = np.where(jump, "extreme_jump_review", "")
    common = {
        "symbol": instrument.symbol, "instrument_id": instrument.instrument_id,
        "asset_class": instrument.asset_class,
        "market": instrument.market, "instrument_type": instrument.instrument_type,
        "source_symbol": instrument.source_symbol, "timezone": "UTC", "timeframe": timeframe,
        "currency": instrument.currency, "quote_currency": instrument.quote_currency,
        "data_source": source, "price_basis": instrument.price_basis,
        "adjustment_method": instrument.adjustment_method,
        "quality_status": "PENDING", "quality_score": np.nan,
        "raw_file_hash": "", "request_id": "", "raw_snapshot_id": "",
        "source_run_id": "", "dataset_version": "", "curated_version": "",
        "ingested_at": pd.Timestamp.now(tz="UTC"),
    }
    for key, value in common.items():
        clean[key] = value
    delta = pd.Timedelta(FRAME_DELTAS[timeframe])
    clean["bar_end"] = clean["timestamp"] + delta
    clean = clean.drop(columns=["_row"], errors="ignore")
    quarantine = quarantine.drop(columns=["_row"], errors="ignore")
    ordered = list(CANONICAL_COLUMNS) + [c for c in clean if c not in CANONICAL_COLUMNS]
    clean = clean.loc[:, ordered]
    audit = pd.concat([
        quarantine.assign(audit_action="quarantined"),
        clean.loc[jump].assign(audit_action="retained_for_review"),
    ], ignore_index=True, sort=False)
    return NormalizationResult(
        clean, quarantine, audit, len(quarantined_duplicates),
        int(ohlc_invalid.sum()), int(jump.sum()),
        int(reasons.str.contains("conflicting_duplicate").sum()),
    )


def assess_quality(
    result: NormalizationResult,
    instrument: InstrumentConfig,
    timeframe: str,
    source_deviation_bps_max: float | None = None,
    *,
    provider: str = "",
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
) -> QualityReport:
    """Assess completeness against the requested range, not just actual bars.

    The trailing edge is measured only up to the last settled boundary
    (``requested_end - bar_delta``) so an absent not-yet-closed bar never
    quarantines a healthy update.  A small head truncation relative to the
    verified listing date is treated as a provider defect.

    Missing bars are classified by trading calendar: weekends and known
    NYSE holidays are never provider gaps.  Crypto (24x7) requires a fully
    continuous daily series.  Backtest suitability is judged on the
    complete-bar view only.
    """
    frame = result.clean
    delta = pd.Timedelta(FRAME_DELTAS[timeframe])
    if frame.empty:
        theoretical = missing = 0
        start = end = None
        complete = 0
        latest_complete = None
        stored = 0
        latest_stored = None
        absent_index = pd.DatetimeIndex([])
        breakdown = {"weekend": 0, "holiday": 0, "provider_gap": 0, "unknown": 0}
    else:
        start, end = frame["timestamp"].min(), frame["timestamp"].max()
        stored = len(frame)
        complete_mask = frame["is_complete"].astype(bool)
        complete = int(complete_mask.sum())
        latest_complete = frame.loc[complete_mask, "timestamp"].max() if complete else None
        latest_stored = end
        span_start, span_end = start, end
        if requested_start is not None:
            requested_start = pd.Timestamp(requested_start)
            if requested_start < start:
                earliest = (pd.Timestamp(instrument.earliest_valid_date, tz="UTC")
                            if instrument.earliest_valid_date else None)
                if earliest is not None and requested_start >= earliest and (start - requested_start) <= delta * 5:
                    span_start = requested_start
        if requested_end is not None:
            settled = pd.Timestamp(requested_end) - delta
            if settled > end:
                span_end = settled
        absent_index, breakdown = classify_missing(
            pd.DatetimeIndex(frame["timestamp"].drop_duplicates()),
            market=instrument.market, session=instrument.session,
            start=span_start, end=span_end, freq=FRAME_DELTAS[timeframe],
        )
        theoretical = len(pd.date_range(span_start, span_end, freq=FRAME_DELTAS[timeframe]))
        if instrument.session in {"24x5", "regular"}:
            theoretical -= breakdown["weekend"]
        if instrument.session == "regular" and instrument.market in {"xnas", "xnys"}:
            theoretical -= breakdown["holiday"]
        missing = breakdown["provider_gap"]
        start, end = start.isoformat(), end.isoformat()
    ratio = missing / theoretical if theoretical else 0.0
    coverage = complete / theoretical if theoretical else 0.0
    notes: list[str] = []
    if instrument.session == "regular":
        notes.append("NYSE holiday calendar applied for xnas/xnys; halts are UNKNOWN")
    if result.extreme_jump_count:
        notes.append("extreme jumps retained and flagged for review")
    if result.conflicting_duplicate_count:
        notes.append("conflicting duplicate OHLCV rows quarantined for review")
    if instrument.asset_class == "equity":
        notes.append("adjustment uses provider adjusted-close factor; not a verified institutional corporate-action series")
    allow_missing = ratio <= 0.05
    if instrument.session == "24x7":
        # Crypto daily history must be continuous: one missing day is a defect.
        allow_missing = missing == 0
    suitable = bool(
        complete
        and result.ohlc_anomaly_count == 0
        and result.conflicting_duplicate_count == 0
        and allow_missing
        and (instrument.asset_class != "equity" or instrument.adjustment_policy != "none")
    )
    score = 15.0  # identity and required metadata
    if result.ohlc_anomaly_count == 0 and not frame.empty:
        score += 20.0
    if result.duplicate_count == 0 and not frame.empty:
        score += 20.0
    if not frame.empty and frame["is_complete"].any():
        score += 15.0
    if ratio <= 0.05:
        score += 15.0
    if not frame.empty:
        score += 15.0  # API writes raw hash before publishing
    status = "CURATED" if suitable and score >= 50 else "QUARANTINED"
    missing_intervals = _missing_interval_rows(frame, instrument, timeframe, breakdown)
    return QualityReport(
        instrument.symbol, timeframe, start, end, theoretical, stored, missing, ratio,
        result.duplicate_count, result.ohlc_anomaly_count, result.extreme_jump_count,
        source_deviation_bps_max, True, instrument.session != "regular",
        instrument.asset_class != "equity" or instrument.adjustment_policy != "none",
        suitable, notes, score, status, missing_intervals, provider,
        len(result.quarantine), coverage, stored, complete, stored - complete,
        latest_stored.isoformat() if latest_stored is not None else None,
        latest_complete.isoformat() if latest_complete is not None else None,
        breakdown["weekend"], breakdown["holiday"],
        breakdown["weekend"], breakdown["holiday"], breakdown["provider_gap"], breakdown["unknown"],
        instrument.earliest_valid_date or "",
        start or "", requested_start.isoformat() if requested_start is not None else "",
        requested_end.isoformat() if requested_end is not None else "",
    )


def _missing_interval_rows(
    frame: pd.DataFrame, instrument: InstrumentConfig, timeframe: str,
    breakdown: dict[str, int],
) -> list[dict[str, object]]:
    if frame.empty or not breakdown.get("provider_gap"):
        return []
    observed = pd.DatetimeIndex(frame["timestamp"].drop_duplicates())
    span_start, span_end = observed.min(), observed.max()
    absent, _ = classify_missing(
        observed, market=instrument.market, session=instrument.session,
        start=span_start, end=span_end, freq=FRAME_DELTAS[timeframe],
    )
    intervals: list[dict[str, object]] = []
    if absent.empty:
        return intervals
    step = pd.Timedelta(FRAME_DELTAS[timeframe])
    groups: list[list[pd.Timestamp]] = [[absent[0]]]
    for stamp in absent[1:]:
        if stamp - groups[-1][-1] == step:
            groups[-1].append(stamp)
        else:
            groups.append([stamp])
    for group in groups:
        intervals.append({
            "start": group[0].isoformat(), "end": group[-1].isoformat(),
            "expected_bars": len(group), "observed_bars": 0,
            "classification": "provider_gap",
            "calendar_id": f"{instrument.market}:{instrument.session}",
        })
    return intervals


def compare_sources(primary: pd.DataFrame, secondary: pd.DataFrame) -> pd.DataFrame:
    left = primary[["timestamp", "close"]].rename(columns={"close": "primary_close"})
    right = secondary[["timestamp", "close"]].rename(columns={"close": "secondary_close"})
    out = left.merge(right, on="timestamp", how="inner")
    out["deviation_bps"] = (
        (out["primary_close"] - out["secondary_close"]).abs()
        / out[["primary_close", "secondary_close"]].mean(axis=1)
        * 10_000
    )
    return out


def apply_equity_adjustments(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy().sort_values("timestamp")
    split = pd.to_numeric(out.get("split_factor", 1.0), errors="coerce").fillna(1.0)
    cumulative = split.iloc[::-1].cumprod().iloc[::-1] / split
    for column in ("open", "high", "low", "close"):
        out[f"adjusted_{column}"] = out[column] / cumulative
    dividends = pd.to_numeric(out.get("dividend", 0.0), errors="coerce").fillna(0.0)
    out["adjusted_close"] = out["adjusted_close"] - dividends
    out["adjustment_factor"] = out["adjusted_close"] / out["close"]
    return out


def build_back_adjusted_continuous(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "contract", "open", "high", "low", "close"}
    if not required.issubset(frame):
        raise ValueError(f"missing futures columns: {sorted(required - set(frame))}")
    out = frame.copy().sort_values("timestamp").reset_index(drop=True)
    out["roll_flag"] = out["contract"].ne(out["contract"].shift())
    out.loc[0, "roll_flag"] = False
    adjustment = 0.0
    offsets = np.zeros(len(out))
    for index in range(len(out) - 1, 0, -1):
        if out.loc[index, "roll_flag"]:
            adjustment += out.loc[index, "close"] - out.loc[index - 1, "close"]
        offsets[:index] += 0 if not out.loc[index, "roll_flag"] else (
            out.loc[index, "close"] - out.loc[index - 1, "close"]
        )
    for column in ("open", "high", "low", "close"):
        out[column] = out[column] + offsets
    out["continuous_method"] = "back_adjusted_difference"
    return out


def resample_ohlcv_session(frame: pd.DataFrame, timeframe: str, *, session_timezone: str,
                           close_hour: int = 17) -> pd.DataFrame:
    """Aggregate bars on a provider-defined local trading-day boundary.

    This is used only when a provider does not expose the requested bar size.
    """
    if timeframe.upper() != "H4":
        raise ValueError(f"unsupported derived timeframe: {timeframe}")
    out = frame.copy()
    stamp_col = "timestamp" if "timestamp" in out else "time"
    stamps = pd.to_datetime(out[stamp_col], utc=True, errors="raise")
    local = stamps.dt.tz_convert(session_timezone)
    bucket = (local - pd.Timedelta(hours=close_hour)).dt.floor("4h") + pd.Timedelta(hours=close_hour)
    out["_bucket"] = bucket.dt.tz_convert("UTC")
    out["_source_count"] = 1
    aggregation = {"open": "first", "high": "max", "low": "min", "close": "last", "_source_count": "sum"}
    if "volume" in out:
        aggregation["volume"] = "sum"
    if "adjusted_close" in out:
        aggregation["adjusted_close"] = "last"
    if "is_complete" in out:
        aggregation["is_complete"] = "all"
    result = out.sort_values(stamp_col).groupby("_bucket", as_index=False).agg(aggregation).rename(columns={"_bucket": "timestamp"})
    result["is_complete"] = result["is_complete"].astype(bool) & result["_source_count"].eq(4) if "is_complete" in result else result["_source_count"].eq(4)
    result = result.drop(columns=["_source_count"])
    return result
