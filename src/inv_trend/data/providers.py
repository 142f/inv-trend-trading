from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .models import DownloadRequest, InstrumentConfig, ProviderResult


class BarsProvider(Protocol):
    name: str

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        """Fetch one instrument/timeframe range from the external platform."""

    def fetch_range(self, request: DownloadRequest) -> ProviderResult:
        """Fetch only the exact requested range; defaults to ``fetch``."""

    def normalize_symbol(self, source_symbol: str) -> str:
        """Return the canonical external symbol form the provider expects."""

    def validate_response(self, frame: pd.DataFrame, request: DownloadRequest) -> None:
        """Reject structurally invalid responses before they reach the pipeline."""


class AdapterDefaults:
    """Default adapter behavior shared by every provider implementation."""

    def fetch_range(self, request: DownloadRequest) -> ProviderResult:
        return self.fetch(request)  # type: ignore[attr-defined]

    def normalize_symbol(self, source_symbol: str) -> str:
        return source_symbol.strip().upper()

    def validate_response(self, frame: pd.DataFrame, request: DownloadRequest) -> None:
        if frame is None or frame.empty:
            raise ValueError(f"{self.name}: empty response for {request.instrument.source_symbol}")  # type: ignore[attr-defined]
        expected = {"timestamp", "open", "high", "low", "close"}
        missing = expected - set(str(c).lower() for c in frame.columns)
        if missing:
            raise ValueError(f"{self.name}: response missing columns {sorted(missing)}")
        for column in ("open", "high", "low", "close", "volume"):
            if column in frame:
                pd.to_numeric(frame[column], errors="coerce")


def get_bytes_with_retry(
    url: str, *, timeout: int, retries: int = 3, headers: dict[str, str] | None = None
) -> bytes:
    """GET with exponential backoff for 429/5xx and transient network errors."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = Request(url, headers=headers or {})
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            last = exc
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                delay = min(2 ** (attempt - 1), 8)
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, int(retry_after))
                time.sleep(delay)
                continue
            raise
        except URLError as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            raise
    raise RuntimeError(f"request failed after {retries} attempts: {last}")


@dataclass
class BinanceKlineProvider(AdapterDefaults):
    name: str = "binance"
    base_url: str = "https://data-api.binance.vision"
    timeout: int = 30
    limit: int = 1000
    retries: int = 3
    page_delay: float = 0.05

    def normalize_symbol(self, source_symbol: str) -> str:
        return source_symbol.strip().upper()

    def validate_response(self, frame: pd.DataFrame, request: DownloadRequest) -> None:
        if frame is None or frame.empty:
            raise ValueError(f"binance: empty klines for {request.instrument.source_symbol}")
        expected = {"open_time", "open", "high", "low", "close", "volume"}
        if not expected.issubset(frame.columns):
            raise ValueError(f"binance: unexpected kline columns: {sorted(frame.columns)}")
        if frame["open_time"].isna().any():
            raise ValueError("binance: klines contain invalid open_time values")

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        intervals = {"D1": "1d", "1d": "1d", "H4": "4h", "4h": "4h", "H1": "1h", "1h": "1h"}
        if request.timeframe not in intervals:
            raise ValueError(f"unsupported Binance timeframe: {request.timeframe}")
        cursor = int(request.start.timestamp() * 1000)
        end_ms = int(request.end.timestamp() * 1000)
        rows: list[list[object]] = []
        raw_batches: list[object] = []
        while cursor <= end_ms:
            query = urlencode({
                "symbol": request.instrument.source_symbol,
                "interval": intervals[request.timeframe],
                "startTime": cursor,
                "endTime": end_ms,
                "limit": self.limit,
            })
            body = get_bytes_with_retry(
                f"{self.base_url}/api/v3/klines?{query}", timeout=self.timeout, retries=self.retries
            )
            batch = json.loads(body)
            raw_batches.append(batch)
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][6]) + 1
            if next_cursor <= cursor:
                raise RuntimeError("Binance pagination did not advance")
            cursor = next_cursor
            if len(batch) < self.limit:
                break
            time.sleep(self.page_delay)
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
        self.validate_response(frame, request)
        return ProviderResult(
            frame=frame,
            actual_symbol=request.instrument.source_symbol,
            source=self.name,
            license=request.instrument.license,
            metadata={"endpoint": "/api/v3/klines", "market_type": "spot", "exchange": "binance"},
            raw_payload=json.dumps(raw_batches, separators=(",", ":")).encode(),
        )


@dataclass
class CsvBarsProvider(AdapterDefaults):
    """Adapter for licensed/official exports; accepts a file or HTTPS URL."""

    source: str | Path
    name: str = "licensed_csv"
    license: str = "configured source terms"
    timeout: int = 60

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        location = str(self.source)
        if location.startswith("https://"):
            raw_payload = get_bytes_with_retry(location, timeout=self.timeout)
            frame = pd.read_csv(StringIO(raw_payload.decode("utf-8-sig")))
        else:
            raw_payload = Path(location).read_bytes()
            frame = pd.read_csv(StringIO(raw_payload.decode("utf-8-sig")))
        frame = _filter_frame_to_request(frame, request)
        self.validate_response(frame, request)
        return ProviderResult(frame, request.instrument.source_symbol, self.name, self.license, {
            "location": location, "request_start": request.start.isoformat(),
            "request_end": request.end.isoformat(),
        }, raw_payload=raw_payload, raw_payload_suffix=".csv")


@dataclass
class YahooChartProvider(AdapterDefaults):
    """Free Yahoo Chart adapter. It is intentionally classified research-only upstream."""

    name: str = "yahoo_chart"
    base_url: str = "https://query1.finance.yahoo.com/v8/finance/chart"
    timeout: int = 30
    retries: int = 3

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        intervals = {"D1": "1d", "1d": "1d"}
        if request.timeframe not in intervals:
            raise ValueError("Yahoo Chart P0 only supports provider-native D1")
        query = urlencode({
            "period1": int(request.start.timestamp()), "period2": int(request.end.timestamp()),
            "interval": intervals[request.timeframe], "events": "history", "includeAdjustedClose": "true",
        })
        url = f"{self.base_url}/{request.instrument.source_symbol}?{query}"
        raw_payload = get_bytes_with_retry(
            url, timeout=self.timeout, retries=self.retries,
            headers={"User-Agent": "inv-trend-trading/0.1"},
        )
        payload = json.loads(raw_payload)
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            raise RuntimeError(f"Yahoo Chart returned no data for {request.instrument.source_symbol}")
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        adjclose = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose", [])
        values = {
            "timestamp": pd.to_datetime(result.get("timestamp", []), unit="s", utc=True),
            "open": quote.get("open", []), "high": quote.get("high", []),
            "low": quote.get("low", []), "close": quote.get("close", []),
            "volume": quote.get("volume", []), "adjusted_close": adjclose,
        }
        if adjclose and len(adjclose) == len(values["timestamp"]):
            frame = pd.DataFrame(values)
        else:
            values.pop("adjusted_close")
            frame = pd.DataFrame(values)
        if len(frame) and "adjusted_close" not in frame:
            frame["adjusted_close"] = frame["close"]
        self.validate_response(frame, request)
        return ProviderResult(frame, request.instrument.source_symbol, self.name,
                              request.instrument.license, {"url": url, "research_only": True}, raw_payload=raw_payload)


@dataclass
class AlphaVantageProvider(AdapterDefaults):
    """Optional D1 fallback. It is unavailable unless the caller configures a key."""

    api_key: str | None = None
    name: str = "alpha_vantage"
    base_url: str = "https://www.alphavantage.co/query"
    timeout: int = 30
    retries: int = 3

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        if request.timeframe.upper() != "D1":
            raise ValueError("Alpha Vantage P0 only supports D1")
        key = self.api_key or os.getenv("ALPHAVANTAGE_API_KEY")
        if not key:
            raise RuntimeError("ALPHAVANTAGE_API_KEY is not configured")
        query = urlencode({"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": request.instrument.source_symbol,
                           "outputsize": "full", "apikey": key})
        url = f"{self.base_url}?{query}"
        raw_payload = get_bytes_with_retry(url, timeout=self.timeout, retries=self.retries)
        payload = json.loads(raw_payload)
        rows = payload.get("Time Series (Daily)")
        if not rows:
            raise RuntimeError(payload.get("Note") or payload.get("Error Message") or "Alpha Vantage returned no data")
        frame = pd.DataFrame.from_dict(rows, orient="index").rename(columns={
            "1. open": "open", "2. high": "high", "3. low": "low", "4. close": "close",
            "5. adjusted close": "adjusted_close", "6. volume": "volume",
        }).reset_index(names="timestamp")
        frame = _filter_frame_to_request(frame, request)
        self.validate_response(frame, request)
        return ProviderResult(frame, request.instrument.source_symbol,
                              self.name, request.instrument.license, {
                                  "endpoint": self.base_url, "research_only": True,
                                  "instrument_identity": _instrument_identity(request.instrument),
                              },
                              raw_payload=raw_payload)


@dataclass
class DukascopyBarsProvider(AdapterDefaults):
    """Dukascopy free historical bar adapter.

    The public endpoint needs an instrument numeric id. Deployments may override
    ``instrument_ids`` as Dukascopy changes its public instrument catalogue.
    """

    instrument_ids: dict[str, int] | None = None
    name: str = "dukascopy"
    base_url: str = "https://freeserv.dukascopy.com/2.0/"
    timeout: int = 60
    api_key: str | None = None
    retries: int = 3

    def normalize_symbol(self, source_symbol: str) -> str:
        return source_symbol.strip().upper().replace("_DUKAS", "")

    def fetch(self, request: DownloadRequest) -> ProviderResult:
        intervals = {"D1": "1day", "H1": "1hour", "1d": "1day", "1h": "1hour"}
        if request.timeframe not in intervals:
            raise ValueError("Dukascopy provides D1/H1; derive H4 through resampling")
        ids = self.instrument_ids or {}
        instrument_id = ids.get(request.instrument.source_symbol) or self._discover_instrument_id(request.instrument.source_symbol)
        cursor, end_ms = int(request.start.timestamp() * 1000), int(request.end.timestamp() * 1000)
        step_ms = int(pd.Timedelta("1D" if request.timeframe.upper() == "D1" else "1h").total_seconds() * 1000)
        frames: list[pd.DataFrame] = []
        raw_pages: list[bytes] = []
        while cursor <= end_ms:
            params = {"path": "api/historicalPrices", "instrument": instrument_id,
                      "timeFrame": intervals[request.timeframe], "offerSide": "B", "start": cursor,
                      "end": end_ms, "count": 5000, "dayStartTime": "UTC"}
            if key := self._api_key():
                params["key"] = key
            payload, body = self._load_json_with_body(f"{self.base_url}?{urlencode(params)}")
            raw_pages.append(body)
            page = _dukascopy_frame(payload)
            if page.empty:
                break
            frames.append(page)
            stamps = pd.to_datetime(page["timestamp"], utc=True)
            next_cursor = int(stamps.max().timestamp() * 1000) + step_ms
            if next_cursor <= cursor:
                raise RuntimeError("Dukascopy pagination did not advance")
            cursor = next_cursor
            if len(page) < 5000:
                break
        frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        frame = _filter_frame_to_request(frame, request)
        self.validate_response(frame, request)
        return ProviderResult(frame, request.instrument.source_symbol,
                              self.name, request.instrument.license,
                              {"endpoint": self.base_url, "offer_side": "B", "price_basis": "bid", "pages": len(raw_pages)},
                              raw_payload=b"\n".join(raw_pages), raw_payload_suffix=".jsonl")

    def _discover_instrument_id(self, source_symbol: str) -> int:
        params = {"path": "api/instrumentList"}
        if key := self._api_key():
            params["key"] = key
        payload, _ = self._load_json_with_body(f"{self.base_url}?{urlencode(params)}")
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        target = self.normalize_symbol(source_symbol)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("symbol", row.get("name", row.get("instrument", "")))).upper().replace("/", "")
            if name == target:
                value = row.get("id", row.get("instrumentId", row.get("value")))
                if value is not None:
                    return int(value)
        raise RuntimeError(f"Dukascopy instrument id not found for {source_symbol}")

    def _api_key(self) -> str | None:
        return self.api_key or os.getenv("DUKASCOPY_API_KEY")

    def _load_json_with_body(self, url: str) -> tuple[object, bytes]:
        raw = get_bytes_with_retry(url, timeout=self.timeout, retries=self.retries)
        body = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(body), raw
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Dukascopy returned a non-JSON response; configure DUKASCOPY_API_KEY "
                "when the provider requires a developer key"
            ) from exc


def _filter_frame_to_request(frame: pd.DataFrame, request: DownloadRequest) -> pd.DataFrame:
    out = frame.copy()
    time_column = next((name for name in ("timestamp", "date", "datetime", "time", "open_time") if name in out), None)
    if time_column is None:
        raise ValueError("provider frame has no timestamp column")
    stamps = pd.to_datetime(out[time_column], utc=True, errors="coerce")
    return out.loc[(stamps >= pd.Timestamp(request.start)) & (stamps <= pd.Timestamp(request.end))].reset_index(drop=True)


def _instrument_identity(instrument: InstrumentConfig) -> dict[str, str]:
    """Economic-series contract required before a fallback can be accepted."""
    return {
        "instrument_type": instrument.instrument_type,
        "venue": instrument.venue,
        "currency": instrument.currency,
        "price_basis": instrument.price_basis,
        "adjustment_method": instrument.adjustment_method,
        "session_timezone": instrument.session_timezone,
        "bar_close_rule": instrument.bar_close_rule,
        "ohlc_definition": "provider_native",
    }


def _dukascopy_frame(payload: object) -> pd.DataFrame:
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("candles", []))
    frame = pd.DataFrame(payload)
    aliases = {"time": "timestamp", "timestamp": "timestamp", "open": "open", "close": "close",
               "high": "high", "low": "low", "volume": "volume"}
    frame = frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns})
    if "timestamp" not in frame:
        raise RuntimeError("unexpected Dukascopy historical bar payload")
    if pd.api.types.is_numeric_dtype(frame["timestamp"]):
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame


OFFICIAL_QQQ_HOLDINGS_URL = (
    "https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund"
    "?idType=ticker&interval=monthly&productType=ETF"
)
OFFICIAL_SPY_HOLDINGS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-spy.xlsx"
)


@dataclass
class HoldingsCsvProvider:
    """Parse an authorized ETF holdings CSV without web-page scraping."""

    source: str | Path
    fund: str | None = None
    name: str = "official_etf_holdings_csv"

    def fetch(self, snapshot_date: str | None = None, *, top: int = 50) -> pd.DataFrame:
        if top < 1:
            raise ValueError("top must be positive")
        location = str(self.source)
        if location.startswith("https://"):
            with urlopen(location, timeout=60) as response:
                payload = response.read()
            frame, source_date = _read_holdings_payload(payload, location)
        elif location.lower().endswith(".csv"):
            # Keep the ordinary CSV path compatible with callers that provide
            # pandas storage options or monkeypatch read_csv in tests.
            frame, source_date = pd.read_csv(location), None
        else:
            payload = Path(location).read_bytes()
            frame, source_date = _read_holdings_payload(payload, location)
        aliases = {
            "symbol": ("symbol", "ticker", "holding ticker"),
            "company_name": ("company_name", "name", "holding name", "security name"),
            "weight": ("weight", "weight (%)", "portfolio weight"),
            "sector": ("sector", "industry"),
            "snapshot_date": ("snapshot_date", "as of", "date"),
            "fund": ("fund", "etf", "portfolio"),
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
            snapshot_date = snapshot_date or source_date
            if snapshot_date is None:
                raise ValueError("snapshot_date must come from the source or caller")
            result["snapshot_date"] = snapshot_date
        result["snapshot_date"] = pd.to_datetime(result["snapshot_date"]).dt.date.astype(str)
        if result["snapshot_date"].nunique() != 1:
            raise ValueError("holdings CSV must contain exactly one snapshot_date")
        if "fund" not in result:
            if not self.fund:
                raise ValueError("fund must come from the source or caller")
            result["fund"] = self.fund
        result["fund"] = result["fund"].astype(str).str.upper().str.strip()
        if result["fund"].nunique() != 1 or result["fund"].iloc[0] not in {"QQQ", "SPY"}:
            raise ValueError("one holdings CSV must contain exactly one supported fund: QQQ or SPY")
        result["symbol"] = result["symbol"].astype(str).str.upper().str.strip()
        if result["symbol"].eq("").any() or result["symbol"].duplicated().any():
            raise ValueError("holdings symbols must be non-empty and unique")
        result["weight"] = pd.to_numeric(
            result["weight"].astype(str).str.rstrip("%"), errors="raise"
        )
        if result["weight"].max() > 1:
            result["weight"] /= 100
        if result["weight"].isna().any() or (result["weight"] <= 0).any():
            raise ValueError("holdings weights must be positive")
        result["data_source"] = self.name
        result["source_location"] = location
        result = result.sort_values("weight", ascending=False).head(top).reset_index(drop=True)
        result["rank"] = range(1, len(result) + 1)
        return result


def _read_holdings_payload(payload: bytes, location: str) -> tuple[pd.DataFrame, str | None]:
    stripped = payload.lstrip()
    if stripped.startswith(b"{"):
        raw = json.loads(payload)
        rows = raw.get("holdings")
        if not isinstance(rows, list):
            raise ValueError("holdings JSON must contain a holdings list")
        frame = pd.DataFrame(rows).rename(columns={
            "ticker": "symbol", "issuerName": "company_name",
            "percentageOfTotalNetAssets": "weight",
        })
        if "securityTypeCode" in frame:
            frame = frame.loc[frame["securityTypeCode"].isin({"COM", "ADR", "DRNY"})]
        # Some fund holdings are private securities whose displayed ticker can
        # collide with an unrelated public Yahoo symbol (for example SpaceX / SPCX).
        if "company_name" in frame:
            frame = frame.loc[
                ~frame["company_name"].astype(str).str.contains(
                    "Space Exploration Technologies", case=False, na=False
                )
            ]
        frame = frame.loc[frame["symbol"].notna() & frame["weight"].notna()]
        return frame, raw.get("effectiveBusinessDate") or raw.get("effectiveDate")
    if payload.startswith(b"PK\x03\x04") or location.lower().endswith(".xlsx"):
        raw = pd.read_excel(BytesIO(payload), sheet_name="holdings", header=None)
        date_text = str(raw.iloc[2, 1]) if len(raw) > 2 else ""
        source_date = pd.to_datetime(
            date_text.replace("As of", "").strip(), errors="coerce"
        )
        header_rows = raw.index[raw.iloc[:, 0].astype(str).str.strip().eq("Name")]
        if len(header_rows) != 1:
            raise ValueError("holdings XLSX does not contain a unique Name header")
        header = int(header_rows[0])
        frame = raw.iloc[header + 1:].copy()
        frame.columns = raw.iloc[header].astype(str).str.strip()
        frame = frame.rename(columns={
            "Ticker": "symbol", "Name": "company_name", "Weight": "weight",
            "Sector": "sector",
        })
        frame = frame.loc[frame["symbol"].notna() & frame["weight"].notna()]
        return frame, None if pd.isna(source_date) else source_date.date().isoformat()
    return pd.read_csv(StringIO(payload.decode("utf-8-sig"))), None


class QqqHoldingsCsvProvider(HoldingsCsvProvider):
    """Backward-compatible QQQ provider; new callers should use HoldingsCsvProvider."""

    def __init__(self, source: str | Path, name: str = "official_qqq_holdings_csv") -> None:
        super().__init__(source=source, fund="QQQ", name=name)

    def fetch(self, snapshot_date: str | None = None, *, top: int = 20) -> pd.DataFrame:
        return super().fetch(snapshot_date, top=top)
