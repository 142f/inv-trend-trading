from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import time
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from .models import DownloadRequest, ProviderResult


class BarsProvider(Protocol):
    name: str

    def fetch(self, request: DownloadRequest) -> ProviderResult: ...


@dataclass
class BinanceKlineProvider:
    name: str = "binance"
    base_url: str = "https://data-api.binance.vision"
    timeout: int = 30
    limit: int = 1000

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        intervals = {"D1": "1d", "1d": "1d", "H4": "4h", "4h": "4h", "H1": "1h", "1h": "1h"}
        if request.timeframe not in intervals:
            raise ValueError(f"unsupported Binance timeframe: {request.timeframe}")
        cursor = int(request.start.timestamp() * 1000)
        end_ms = int(request.end.timestamp() * 1000)
        rows: list[list[object]] = []
        while cursor <= end_ms:
            query = urlencode({
                "symbol": request.instrument.source_symbol,
                "interval": intervals[request.timeframe],
                "startTime": cursor,
                "endTime": end_ms,
                "limit": self.limit,
            })
            with urlopen(f"{self.base_url}/api/v3/klines?{query}", timeout=self.timeout) as response:
                batch = json.loads(response.read())
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][6]) + 1
            if next_cursor <= cursor:
                raise RuntimeError("Binance pagination did not advance")
            cursor = next_cursor
            if len(batch) < self.limit:
                break
            time.sleep(0.05)
        columns = [
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_volume", "trade_count", "taker_buy_base_volume",
            "taker_buy_quote_volume", "ignore",
        ]
        frame = pd.DataFrame(rows, columns=columns)
        if not frame.empty:
            frame["timestamp"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
            frame["is_complete"] = frame["close_time"].astype("int64") < int(
                datetime.now(timezone.utc).timestamp() * 1000
            )
        return ProviderResult(
            frame=frame,
            actual_symbol=request.instrument.source_symbol,
            source=self.name,
            license=request.instrument.license,
            metadata={"endpoint": "/api/v3/klines", "market_type": "spot", "exchange": "binance"},
        )


@dataclass
class CsvBarsProvider:
    """Adapter for licensed/official exports; accepts a file or HTTPS URL."""

    source: str | Path
    name: str = "licensed_csv"
    license: str = "configured source terms"

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        location = str(self.source)
        if location.startswith("https://"):
            with urlopen(location, timeout=60) as response:
                frame = pd.read_csv(StringIO(response.read().decode("utf-8-sig")))
        else:
            frame = pd.read_csv(Path(location))
        return ProviderResult(frame, request.instrument.source_symbol, self.name, self.license, {
            "location": location,
        })


@dataclass
class QqqHoldingsCsvProvider:
    """Parses an official manager/vendor CSV without scraping rendered web pages."""

    source: str | Path
    name: str = "official_qqq_holdings_csv"

    def fetch(self, snapshot_date: str | None = None) -> pd.DataFrame:
        location = str(self.source)
        if location.startswith("https://"):
            with urlopen(location, timeout=60) as response:
                frame = pd.read_csv(StringIO(response.read().decode("utf-8-sig")))
        else:
            frame = pd.read_csv(Path(location))
        aliases = {
            "symbol": ("symbol", "ticker", "holding ticker"),
            "company_name": ("company_name", "name", "holding name", "security name"),
            "weight": ("weight", "weight (%)", "portfolio weight"),
            "sector": ("sector", "industry"),
            "snapshot_date": ("snapshot_date", "as of", "date"),
        }
        lowered = {str(c).strip().lower(): c for c in frame.columns}
        out: dict[str, object] = {}
        for target, choices in aliases.items():
            match = next((lowered[c] for c in choices if c in lowered), None)
            if match is not None:
                out[target] = frame[match]
        result = pd.DataFrame(out)
        if "symbol" not in result or "weight" not in result:
            raise ValueError("holdings CSV must contain symbol/ticker and weight columns")
        if "snapshot_date" not in result:
            if snapshot_date is None:
                raise ValueError("snapshot_date must come from the source or caller")
            result["snapshot_date"] = snapshot_date
        result["snapshot_date"] = pd.to_datetime(result["snapshot_date"]).dt.date.astype(str)
        result["weight"] = pd.to_numeric(
            result["weight"].astype(str).str.rstrip("%"), errors="raise"
        )
        if result["weight"].max() > 1:
            result["weight"] /= 100
        result["data_source"] = self.name
        result["source_location"] = location
        return result.sort_values("weight", ascending=False).head(20).reset_index(drop=True)
