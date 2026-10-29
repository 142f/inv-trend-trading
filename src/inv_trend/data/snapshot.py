"""Raw-bar resolution for research snapshots: local Repository → cache → live.

The snapshot script must not implement its own source policy.  This module owns
the order, the cache layout and the raw digest so a caller can only receive a
frame plus the provenance it needs for a receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pandas as pd

from .models import DownloadRequest, InstrumentConfig


@dataclass(frozen=True)
class SnapshotBars:
    frame: pd.DataFrame
    source: str
    digest: str


def frame_payload(frame: pd.DataFrame) -> str:
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).map(lambda item: item.isoformat())
    columns = [name for name in ("timestamp", "open", "high", "low", "close", "volume", "adjusted_close")
               if name in out.columns]
    return out.loc[:, columns].to_json(orient="records")


def frame_digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(frame_payload(frame).encode("utf-8")).hexdigest()


def cache_path(cache_root: str | Path, cutoff: datetime, symbol: str) -> Path:
    day = pd.Timestamp(cutoff).tz_convert("America/New_York").date().isoformat()
    return Path(cache_root) / day / f"{symbol}.json"


def read_cache(path: Path) -> tuple[pd.DataFrame, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(payload["rows"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame, str(payload.get("sha256", ""))


def write_cache(path: Path, frame: pd.DataFrame) -> str:
    body = frame_payload(frame)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sha256": digest, "rows": json.loads(body)}, ensure_ascii=False),
                    encoding="utf-8")
    return digest


def load_verified(service, symbol: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    """Load the Repository's canonical bars without pre-applying the adjustment.

    ``adjusted=False`` keeps raw OHLC plus ``adjusted_close`` so the shared
    selector applies the factor exactly once and produces the same completeness
    counts as the provider path.
    """

    if service is None:
        return None
    try:
        frame = service.load_bars(symbol, "D1", start, end, adjusted=False,
                                  allow_research=True, completed_only=True)
    except Exception:
        return None
    if frame is None or len(frame) == 0:
        return None
    return frame


def resolve_snapshot_bars(
    *,
    symbol: str,
    instrument: InstrumentConfig,
    service,
    provider,
    start: datetime,
    end: datetime,
    cache_root: str | Path,
    refresh: bool = False,
) -> SnapshotBars:
    if not refresh:
        local = load_verified(service, symbol, start, end)
        if local is not None:
            return SnapshotBars(local, "local", frame_digest(local))
        cached = cache_path(cache_root, end, symbol)
        if cached.is_file():
            frame, digest = read_cache(cached)
            return SnapshotBars(frame, "yahoo_cache", digest or frame_digest(frame))
    fetched = provider.fetch(DownloadRequest(instrument, "D1", start, end))
    digest = write_cache(cache_path(cache_root, end, symbol), fetched.frame)
    return SnapshotBars(fetched.frame, "yahoo_live", digest)
