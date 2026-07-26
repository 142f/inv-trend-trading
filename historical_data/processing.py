from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .models import CANONICAL_COLUMNS, InstrumentConfig, QualityReport

RULE_VERSION = "1.0.0"
FRAME_DELTAS = {"D1": "1D", "1d": "1D", "H4": "4h", "4h": "4h", "H1": "1h", "1h": "1h"}


@dataclass
class NormalizationResult:
    clean: pd.DataFrame
    quarantine: pd.DataFrame
    audit: pd.DataFrame
    duplicate_count: int
    ohlc_anomaly_count: int
    extreme_jump_count: int


def normalize_bars(
    raw: pd.DataFrame,
    instrument: InstrumentConfig,
    timeframe: str,
    source: str,
    *,
    now: datetime | None = None,
) -> NormalizationResult:
    frame = raw.copy()
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
    duplicate = frame["timestamp"].notna() & frame.duplicated("timestamp", keep="last")
    reasons[duplicate] += "duplicate_superseded;"

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
        "symbol": instrument.symbol, "asset_class": instrument.asset_class,
        "market": instrument.market, "instrument_type": instrument.instrument_type,
        "timezone": "UTC", "timeframe": timeframe, "quote_currency": instrument.quote_currency,
        "data_source": source, "ingested_at": pd.Timestamp.now(tz="UTC"),
    }
    for key, value in common.items():
        clean[key] = value
    clean = clean.drop(columns=["_row"], errors="ignore")
    quarantine = quarantine.drop(columns=["_row"], errors="ignore")
    ordered = list(CANONICAL_COLUMNS) + [c for c in clean if c not in CANONICAL_COLUMNS]
    clean = clean.loc[:, ordered]
    audit = pd.concat([
        quarantine.assign(audit_action="quarantined"),
        clean.loc[jump].assign(audit_action="retained_for_review"),
    ], ignore_index=True, sort=False)
    return NormalizationResult(
        clean, quarantine, audit, int(duplicate.sum()), int(ohlc_invalid.sum()), int(jump.sum())
    )


def assess_quality(
    result: NormalizationResult,
    instrument: InstrumentConfig,
    timeframe: str,
    source_deviation_bps_max: float | None = None,
) -> QualityReport:
    frame = result.clean
    if frame.empty:
        theoretical = missing = 0
        start = end = None
    else:
        start, end = frame["timestamp"].min(), frame["timestamp"].max()
        index = pd.date_range(start, end, freq=FRAME_DELTAS[timeframe])
        if instrument.session == "24x5":
            index = index[index.dayofweek < 5]
        elif instrument.session == "regular":
            index = index[index.dayofweek < 5]
        theoretical = len(index)
        missing = max(theoretical - frame["timestamp"].nunique(), 0)
        start, end = start.isoformat(), end.isoformat()
    ratio = missing / theoretical if theoretical else 0.0
    notes: list[str] = []
    if instrument.session == "regular":
        notes.append("business-day approximation used; install an exchange calendar for holidays")
    if result.extreme_jump_count:
        notes.append("extreme jumps retained and flagged for review")
    suitable = bool(
        len(frame)
        and result.ohlc_anomaly_count == 0
        and ratio <= 0.05
        and frame["is_complete"].any()
        and (instrument.asset_class != "equity" or instrument.adjustment_policy != "none")
    )
    return QualityReport(
        instrument.symbol, timeframe, start, end, theoretical, len(frame), missing, ratio,
        result.duplicate_count, result.ohlc_anomaly_count, result.extreme_jump_count,
        source_deviation_bps_max, True, instrument.session != "regular",
        instrument.asset_class != "equity" or instrument.adjustment_policy != "none",
        suitable, notes,
    )


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
