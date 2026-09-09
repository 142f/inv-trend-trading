"""Pinned inputs from the existing authority; no implicit symbol/calendar/FX conversion."""
from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd

from inv_trend.core.四资产合同_v1 import ASSUMPTION, CORE
from inv_trend.storage.结构化存储_v3 import UnifiedStore, canonical, digest


def quality_checks(frame, *, expected_version, expected_index=None, previous_hash=None,
                   current_hash=None, extreme_gap=0.3):
    required = {"instrument_id", "timestamp", "open", "high", "low", "close", "volume",
                "currency", "source", "dataset_version", "quality_status", "available_at"}
    errors, warnings = [], []
    if not required <= set(frame):
        return {"errors": ["missing schema fields: " + str(sorted(required - set(frame)))],
                "warnings": []}
    t = pd.to_datetime(frame.timestamp)
    if t.dt.tz is None or str(t.dt.tz) != "UTC":
        errors.append("timezone mismatch")
    a = pd.to_datetime(frame.available_at)
    if a.dt.tz is None or str(a.dt.tz) != "UTC":
        errors.append("availability timezone mismatch")
    elif (a <= t).any():
        errors.append("invalid availability time")
    if t.duplicated().any():
        errors.append("duplicate timestamp")
    if not t.is_monotonic_increasing:
        errors.append("non-monotonic timestamp")
    p = frame[["open", "high", "low", "close"]].to_numpy(float)
    if not np.isfinite(p).all() or (p <= 0).any():
        errors.append("negative price/nonfinite price")
    if ((frame.low > frame[["open", "close"]].min(axis=1)) |
            (frame.high < frame[["open", "close"]].max(axis=1))).any():
        errors.append("OHLC inconsistency")
    if not np.isfinite(frame.volume).all() or (frame.volume < 0).any():
        errors.append("invalid volume")
    if set(frame.dataset_version) != {expected_version}:
        errors.append("dataset version mismatch")
    if expected_index is not None and len(pd.DatetimeIndex(expected_index).difference(t)):
        errors.append("missing interval")
    if expected_index is not None and len(pd.DatetimeIndex(t).difference(expected_index)):
        errors.append("calendar mismatch")
    if frame.close.diff().eq(0).rolling(5).sum().ge(5).any():
        warnings.append("stale price")
    if frame.open.div(frame.close.shift(1)).sub(1).abs().gt(extreme_gap).any():
        warnings.append("extreme gap")
    if previous_hash and current_hash != previous_hash:
        errors.append("source revision")
    return {"errors": errors, "warnings": warnings}


@dataclass
class MarketBundle:
    frames: dict
    lineage: dict
    quality: dict
    version: str
    mode: str

    def aligned(self):
        # Union preserves crypto weekends and metal provider labels. Never inner join.
        parts = {s: f.set_index("timestamp") for s, f in self.frames.items()}
        index = pd.DatetimeIndex(sorted(set().union(*(set(f.index) for f in parts.values()))))
        index = pd.date_range(index.min(), index.max(), freq="D", tz="UTC")
        return index, {s: parts[s].reindex(index) for s in CORE}


def load_bundle(data_root="data", *, mode="FORMAL"):
    if mode not in ("FORMAL", ASSUMPTION):
        raise ValueError("unknown data mode")
    if mode == "FORMAL":
        raise ValueError("Data lineage invalid: metal session/FX/historical execution UNVERIFIED")
    store = UnifiedStore(data_root)
    frames, lineage, checks = {}, {}, {}
    # One SQLite snapshot pins heads and metal rows. Objects are immutable and hash checked.
    with store.connect(readonly=True) as db:
        db.execute("BEGIN")
        for symbol in CORE[:2]:
            rows = db.execute("""SELECT v.*,o.path,o.stored_sha256 FROM dataset_heads h
                JOIN dataset_versions v USING(version)
                JOIN data_aliases a ON a.logical_path=v.artifact_path
                JOIN data_objects o USING(object_id)
                WHERE h.symbol=? AND h.timeframe='D1' AND h.channel='curated'""",
                              (symbol,)).fetchall()
            if len(rows) != 1:
                raise ValueError(f"{symbol}: missing/ambiguous pinned source")
            row = dict(rows[0])
            path = (store.root / row["path"]).resolve()
            if not path.is_relative_to(store.root):
                raise ValueError("source path outside data root")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != row["stored_sha256"]:
                raise ValueError("source revision/hash mismatch")
            f = pd.read_parquet(path)
            if set(f.instrument_id) != {row["instrument_id"]} or set(f.currency) != {"USDT"}:
                raise ValueError("instrument/currency mismatch")
            f = f.rename(columns={"data_source": "source", "bar_end": "available_at"})
            expected = pd.date_range(f.timestamp.min(), f.timestamp.max(), freq="D")
            checks[symbol] = quality_checks(f, expected_version=row["version"],
                                            expected_index=expected)
            lineage[symbol] = {"dataset_version": row["version"], "input_hash": actual,
                               "instrument_id": row["instrument_id"],
                               "fx_assumption": "USDT/USD=1", "source_revision": "UNVERIFIED"}
            frames[symbol] = f
        rows = db.execute("SELECT * FROM market_datasets_v3 ORDER BY dataset_id").fetchall()
        sources = [dict(r) for r in rows if db.execute(
            "SELECT count(distinct symbol) FROM market_bars_v3 WHERE dataset_id=? "
            "AND symbol IN ('XAUUSD_DUKAS','XAGUSD_DUKAS')", (r["dataset_id"],)).fetchone()[0] == 2]
        if len(sources) != 1:
            raise ValueError("metal dataset missing/ambiguous: explicit migration required")
        source = sources[0]
        for symbol in CORE[2:]:
            f = pd.read_sql_query("SELECT * FROM market_bars_v3 WHERE dataset_id=? "
                                  "AND symbol=? ORDER BY ordinal", db,
                                  params=(source["dataset_id"], symbol + "USD_DUKAS"))
            if f.empty:
                raise ValueError("missing metal history")
            f["timestamp"] = pd.to_datetime(f.date, utc=True)
            f["available_at"] = f.timestamp + pd.Timedelta(days=1)
            f["instrument_id"] = symbol + "USD.DUKAS.BID.CFD"
            f["currency"] = "USD"
            f["dataset_version"] = source["dataset_hash"]
            f["quality_status"] = ASSUMPTION
            checks[symbol] = quality_checks(f, expected_version=source["dataset_hash"])
            checks[symbol]["warnings"].extend(["calendar mismatch UNVERIFIED",
                "missing interval classification UNVERIFIED", "bid/ask execution UNVERIFIED",
                "source revision UNVERIFIED"])
            lineage[symbol] = {"dataset_version": source["dataset_hash"],
                               "input_hash": digest(f.to_json(date_format="iso").encode()),
                               "instrument_id": symbol + "USD.DUKAS.BID.CFD",
                               "session_assumption": "provider label + 1 UTC day"}
            frames[symbol] = f
    for symbol, quality in checks.items():
        if quality["errors"]:
            raise ValueError(f"{symbol}: {quality['errors']}")
    return MarketBundle(frames, lineage, checks, digest(canonical(lineage)), mode)
