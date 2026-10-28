"""计算 QQQ、SPY 前 100 去重股票的日线指标，并写出可筛选的单文件页面。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pandas as pd

from inv_trend.core.features import FeatureRequest, PreparedBars
from inv_trend.data.calendar import latest_completed_us_bar, us_session_bounds
from inv_trend.data.config import load_instruments
from inv_trend.data.models import DownloadRequest, InstrumentConfig
from inv_trend.data.processing import select_completed_bars
from inv_trend.data.providers import YahooChartProvider

ROOT = Path(__file__).resolve().parents[1]
OUT_HTML = ROOT / "deliverables" / "美股前100指标看板.html"
OUT_JSON = ROOT / "deliverables" / "美股前100指标数据.json"
OUT_RECEIPT = ROOT / "deliverables" / "美股前100指标_回执.json"
CACHE_ROOT = ROOT / ".tmp" / "yahoo-us-top100"
ASSETS_PATH = ROOT / "src" / "inv_trend" / "data" / "config" / "assets.yaml"
MIN_BARS = 169
WARMUP_FIELDS = ("120", "144", "169")
PRICE_BASIS = "provider_adjusted_close"

QQQ = """NVDA AAPL MSFT MU AMZN AMD TSLA META GOOGL GOOG SPCX WMT AVGO INTC PLTR COST CSCO PANW AMAT LRCX NFLX CRWD TXN AMGN LIN SHOP MRVL GILD KLAC ADI PEP QCOM SNDK ISRG TMUS ASML STX VRTX FTNT WDC ADP BKNG SBUX ADBE MAR CSX INTU MELI ARM MNST DDOG CEG CTAS CDNS SNPS CMCSA MDLZ REGN MRNA APP ROST DASH ORLY ABNB AEP HON LITE FAST NXPI FANG PCAR MPWR BKR MSTR PDD ADSK HONA PYPL XEL TER CCEP KDP TRI EXC ALAB IDXX ROP PAYX WDAY MCHP TTWO ODFL NBIS FER AXON GEHC RKLB ALNY DXCM CPRT""".split()
SPY = """NVDA AAPL MSFT AMZN GOOGL AVGO GOOG META TSLA LLY BRK.B MU JPM AMD XOM V JNJ WMT ABBV MA CVX PLTR COST INTC CSCO UNH PG KO MRK BAC HD CAT PM PANW AMAT GE LRCX NFLX CRWD TXN RTX GS WFC TMO IBM AMGN LIN ORCL GEV MS CRM C MRVL DIS GILD ADI KLAC QCOM VZ PEP APH COP SNDK ANET ABT MCD UNP NEE PFE WELL AXP NOW DE BLK TJX ISRG ACN BA SCHW DHR T STX ETN DELL UBER SPGI VRTX PGR MPC BMY CB MO WDC COF LMT ADP VLO PLD CVS MDT""".split()

REQUEST = FeatureRequest(
    atr_period=14,
    donchian_periods=(20, 55),
    sma_lags=tuple((period, 0) for period in (5, 10, 20, 55, 120)),
    ema_periods=(10, 20, 55, 144, 169),
    macd_periods=(12, 26, 9),
    dmi_period=14,
    volume_sma_lags=((20, 1),),
    include_true_range=True,
)


def feature_spec() -> dict:
    return {
        "atr_period": REQUEST.atr_period,
        "donchian_periods": list(REQUEST.donchian_periods),
        "sma_lags": [list(item) for item in REQUEST.sma_lags],
        "ema_periods": list(REQUEST.ema_periods),
        "macd_periods": list(REQUEST.macd_periods or ()),
        "dmi_period": REQUEST.dmi_period,
        "volume_sma_lags": [list(item) for item in REQUEST.volume_sma_lags],
        "relative_volume": "volume / volume_sma_20_lag_1",
        "min_bars": MIN_BARS,
    }


def universe_members() -> list[dict]:
    """QQQ 99 + SPY 100. SPCX stays at source rank 11 and is not a member."""

    members = []
    seen: set[str] = set()
    for index, symbol in enumerate(QQQ, 1):
        if symbol == "SPCX":
            continue
        seen.add(symbol)
        members.append({"symbol": symbol, "qqq_rank": index, "spy_rank": None})
    for index, symbol in enumerate(SPY, 1):
        if symbol in seen:
            for item in members:
                if item["symbol"] == symbol:
                    item["spy_rank"] = index
            continue
        members.append({"symbol": symbol, "qqq_rank": None, "spy_rank": index})
    return members


def session_end_cutoff(now: datetime | None = None, *, market: str = "xnas") -> datetime:
    """Latest completed US session end. Midnight of that date is not complete."""

    clock = now or datetime.now(timezone.utc)
    session = latest_completed_us_bar(clock, market=market, freq="1D")
    day = pd.Timestamp(session).tz_convert("UTC").date()
    bounds = us_session_bounds(day, market=market)
    if bounds is None:
        raise ValueError("latest completed session has no US bounds")
    return bounds[1].to_pydatetime()


def _as_utc(now: datetime | None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def _session_end(day, *, market: str = "xnas") -> datetime:
    bounds = us_session_bounds(day, market=market)
    if bounds is None:
        raise ValueError(f"{day.isoformat()} is not a US cash session")
    return bounds[1].to_pydatetime()


def _inside_session(clock: datetime, *, market: str = "xnas") -> bool:
    day = pd.Timestamp(clock).tz_convert("America/New_York").date()
    bounds = us_session_bounds(day, market=market)
    if bounds is None:
        return False
    return bounds[0].to_pydatetime() <= clock < bounds[1].to_pydatetime()


def resolve_window(
    now: datetime | None,
    as_of: str | None,
    *,
    completed_only: bool,
    market: str = "xnas",
) -> dict:
    """Split the Yahoo fetch end from the bar-completeness clock.

    Completed-only never uses a session end that is still in the future.
    Partial mode, while that session is open, fetches through the clock so the
    13:30 UTC open print is downloaded and stays incomplete.
    """

    clock = _as_utc(now)
    closed = session_end_cutoff(clock, market=market)
    if not as_of:
        if completed_only or not _inside_session(clock, market=market):
            return {"fetch_end": closed, "completeness_now": closed, "partial_mode": False}
        return {"fetch_end": clock, "completeness_now": clock, "partial_mode": True}
    stamp = pd.Timestamp(as_of)
    if len(as_of) <= 10:
        session_end = _session_end(stamp.date(), market=market)
        if completed_only:
            if session_end > clock:
                raise ValueError(f"{as_of} session has not closed")
            return {"fetch_end": session_end, "completeness_now": session_end, "partial_mode": False}
        if session_end <= clock:
            return {"fetch_end": session_end, "completeness_now": session_end, "partial_mode": False}
        if _inside_session(clock, market=market) and pd.Timestamp(clock).tz_convert("America/New_York").date() == stamp.date():
            return {"fetch_end": clock, "completeness_now": clock, "partial_mode": True}
        raise ValueError(f"{as_of} is not an open US session")
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    instant = stamp.tz_convert("UTC").to_pydatetime()
    if instant > clock:
        raise ValueError("as-of is in the future")
    if not completed_only and _inside_session(instant, market=market):
        return {"fetch_end": instant, "completeness_now": instant, "partial_mode": True}
    end = session_end_cutoff(instant, market=market)
    return {"fetch_end": end, "completeness_now": end, "partial_mode": False}


def clip_fetch(frame: pd.DataFrame, fetch_end: datetime) -> pd.DataFrame:
    """Yahoo period2 is exclusive: drop bars timestamped at or after the fetch end."""

    if frame.empty or "timestamp" not in frame.columns:
        return frame.copy()
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.loc[stamps < pd.Timestamp(fetch_end)].copy()


def parse_as_of(text: str | None, now: datetime | None = None, *, market: str = "xnas") -> datetime:
    """Completed-session end. A date that has not closed is rejected."""

    return resolve_window(now, text, completed_only=True, market=market)["completeness_now"]


def relative_volume_value(volume, average) -> float | None:
    if volume is None or average is None or pd.isna(volume) or pd.isna(average) or float(average) == 0:
        return None
    return float(volume) / float(average)


def indicator_snapshot(symbol: str, prepared: pd.DataFrame, meta: dict, *, partial: bool) -> dict:
    row = prepared.iloc[-1]
    bar_count = int(len(prepared))
    warmup_ok = bar_count >= MIN_BARS
    pairs = {
        "10_20": _side(row.get("ema_10"), row.get("ema_20")),
        "10_55": _side(row.get("ema_10"), row.get("ema_55")),
        "20_55": _side(row.get("ema_20"), row.get("ema_55")),
    }
    votes = [item for item in pairs.values() if item in {"多", "空"}]
    if len(votes) == 3 and all(item == "多" for item in votes):
        bias = "多"
    elif len(votes) == 3 and all(item == "空" for item in votes):
        bias = "空"
    elif votes:
        bias = "混"
    else:
        bias = ""
    smas = [_warm_number(row.get(f"sma_{period}_lag_0"), period, warmup_ok) for period in (5, 10, 20, 55, 120)]
    raw_smas = [row.get(f"sma_{period}_lag_0") for period in (5, 10, 20, 55, 120)]
    if warmup_ok and all(pd.notna(item) for item in raw_smas) and all(raw_smas[i] > raw_smas[i + 1] for i in range(4)):
        sma_bias = "多"
    elif warmup_ok and all(pd.notna(item) for item in raw_smas) and all(raw_smas[i] < raw_smas[i + 1] for i in range(4)):
        sma_bias = "空"
    elif warmup_ok and all(pd.notna(item) for item in raw_smas):
        sma_bias = "混"
    else:
        sma_bias = ""
    stamp = prepared.index[-1]
    as_of = str(pd.Timestamp(stamp).tz_convert("America/New_York").date())
    tail = prepared.tail(120)
    raw_close = row["unadjusted_close"] if "unadjusted_close" in prepared.columns else row["close"]
    factor = row["adjustment_factor"] if "adjustment_factor" in prepared.columns else 1.0
    missing = [name for name in ("sma_120", "ema_144", "ema_169") if not warmup_ok]
    return {
        "symbol": symbol,
        "funds": meta["funds"],
        "qqq_rank": meta["qqq_rank"],
        "spy_rank": meta["spy_rank"],
        "as_of": as_of,
        "close": _num(raw_close),
        "adjusted_close": _num(row["close"]),
        "adjustment_factor": _num(factor),
        "price_basis": PRICE_BASIS,
        "bias": bias,
        "pairs": pairs,
        "sma_bias": sma_bias,
        "sma": {
            "5": smas[0], "10": smas[1], "20": smas[2], "55": smas[3], "120": smas[4],
        },
        "ema": {
            "10": _warm_number(row.get("ema_10"), 10, warmup_ok),
            "20": _warm_number(row.get("ema_20"), 20, warmup_ok),
            "55": _warm_number(row.get("ema_55"), 55, warmup_ok),
            "144": _warm_number(row.get("ema_144"), 144, warmup_ok),
            "169": _warm_number(row.get("ema_169"), 169, warmup_ok),
        },
        "dif": _num(row.get("dif")),
        "dea": _num(row.get("dea")),
        "macd": _side(row.get("dif"), row.get("dea")),
        "adx": _num(row.get("adx")),
        "plus_di": _num(row.get("plus_di")),
        "minus_di": _num(row.get("minus_di")),
        "atr": _num(row.get("atr")),
        "histogram": _num(row.get("histogram")),
        "macd_bar": _num(row.get("macd_bar")),
        "volume": _num(row.get("volume")),
        "volume_sma_20": _num(row.get("volume_sma_20_lag_1")),
        "channel": {
            "high_20": _num(row.get("channel_high_20")),
            "low_20": _num(row.get("channel_low_20")),
            "high_55": _num(row.get("channel_high_55")),
            "low_55": _num(row.get("channel_low_55")),
        },
        "relative_volume": _num(relative_volume_value(row.get("volume"), row.get("volume_sma_20_lag_1"))),
        "bar_count": bar_count,
        "warmup_ok": warmup_ok,
        "partial": partial,
        "missing_fields": missing,
        "spark": {
            "date": [str(pd.Timestamp(item).tz_convert("America/New_York").date()) for item in tail.index],
            "close": [_num(item) for item in tail["close"]],
            "ema10": [_num(item) for item in tail["ema_10"]],
            "ema20": [_num(item) for item in tail["ema_20"]],
            "ema55": [_num(item) for item in tail["ema_55"]],
        },
    }


def _warm_number(value, period: int, warmup_ok: bool):
    if not warmup_ok and str(period) in WARMUP_FIELDS:
        return None
    return _num(value)


def incomplete_entry(row: dict) -> dict | None:
    if row["warmup_ok"]:
        return None
    return {
        "symbol": row["symbol"],
        "bar_count": row["bar_count"],
        "missing_fields": row["missing_fields"],
        "reason": f"warmup<{MIN_BARS}",
    }


def build_receipt(
    *,
    generated_at: str,
    data_cutoff: str,
    provider: str,
    completed_only: bool,
    universe: dict,
    config_hash: str,
    script_git_commit: str,
    counts: dict,
    failed: list,
    incomplete: list,
    content_hash: str,
    bar_count: dict,
    raw_hash: str,
    research_only: bool = True,
) -> dict:
    return {
        "schema_version": 2,
        "generated_at": generated_at,
        "data_cutoff": data_cutoff,
        "provider": provider,
        "research_only": research_only,
        "price_basis": PRICE_BASIS,
        "completed_only": completed_only,
        "feature_request": feature_spec(),
        "universe": universe,
        "config_hash": config_hash,
        "script_git_commit": script_git_commit,
        "counts": counts,
        "bar_count": bar_count,
        "raw_hash": raw_hash,
        "failed": failed,
        "incomplete": incomplete,
        "content_hash": content_hash,
    }


def _side(fast, slow) -> str:
    if pd.isna(fast) or pd.isna(slow):
        return ""
    if fast > slow:
        return "多"
    if fast < slow:
        return "空"
    return "平"


def _num(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value), 4)


def _feature_frame(used: pd.DataFrame) -> pd.DataFrame:
    frame = used.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
    frame = frame.set_index("timestamp")
    return frame


def _member_meta(symbol: str, members: list[dict]) -> dict:
    item = next(row for row in members if row["symbol"] == symbol)
    funds = [name for name, rank in (("QQQ", item["qqq_rank"]), ("SPY", item["spy_rank"])) if rank]
    return {"funds": ",".join(funds), "qqq_rank": item["qqq_rank"], "spy_rank": item["spy_rank"]}


def _cache_path(cutoff: datetime, symbol: str) -> Path:
    day = pd.Timestamp(cutoff).tz_convert("America/New_York").date().isoformat()
    return CACHE_ROOT / day / f"{symbol}.json"


def _frame_payload(frame: pd.DataFrame) -> str:
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).map(lambda item: item.isoformat())
    columns = [name for name in ("timestamp", "open", "high", "low", "close", "volume", "adjusted_close") if name in out.columns]
    return out.loc[:, columns].to_json(orient="records")


def _read_cache(path: Path) -> tuple[pd.DataFrame, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(payload["rows"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame, str(payload.get("sha256", ""))


def _frame_digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(_frame_payload(frame).encode("utf-8")).hexdigest()


def _write_cache(path: Path, frame: pd.DataFrame) -> str:
    body = _frame_payload(frame)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sha256": digest, "rows": json.loads(body)}, ensure_ascii=False), encoding="utf-8")
    return digest


def _load_local(service, symbol: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    if service is None:
        return None
    try:
        frame = service.load_bars(
            symbol, "D1", start, end, allow_research=True, completed_only=True,
        )
    except Exception:
        return None
    if frame is None or len(frame) == 0:
        return None
    return frame


def _content_hash(rows: list[dict]) -> str:
    stable = []
    for row in rows:
        stable.append({key: row[key] for key in (
            "symbol", "as_of", "close", "adjusted_close", "adjustment_factor", "bias",
            "sma", "ema", "dif", "dea", "macd", "adx", "plus_di", "minus_di", "atr",
            "histogram", "channel", "volume", "volume_sma_20", "relative_volume",
            "bar_count", "warmup_ok", "partial",
        )})
    body = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _config_hash() -> str:
    return hashlib.sha256(ASSETS_PATH.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def compute_symbol(raw: pd.DataFrame, instrument: InstrumentConfig, source: str, now: datetime, meta: dict, *, completed_only: bool, already_adjusted: bool = False) -> tuple[dict, dict]:
    if already_adjusted:
        used = raw.copy()
        counts = {
            "duplicate_count": 0, "ohlc_anomaly_count": 0, "conflicting_duplicate_count": 0,
            "quarantine_count": 0, "partial_rows": 0, "adjustment_reject_count": 0,
            "volume_missing_count": int(used["volume"].isna().sum()) if "volume" in used.columns else 0,
        }
    else:
        used, counts = select_completed_bars(
            raw, instrument, "D1", source, now=now, completed_only=completed_only,
        )
    features = _feature_frame(used)
    if features.empty:
        raise RuntimeError("no completed bars")
    prepared = PreparedBars.build(features, REQUEST).frame
    partial = (not completed_only) and bool(counts["partial_rows"])
    row = indicator_snapshot(instrument.symbol, prepared, meta, partial=partial)
    return row, counts


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--include-partial", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    completed_only = not args.include_partial
    window = resolve_window(datetime.now(timezone.utc), args.as_of, completed_only=completed_only)
    cutoff = window["completeness_now"]
    fetch_end = window["fetch_end"]
    instruments = load_instruments()
    members = universe_members()
    symbols = [item["symbol"] for item in members]
    missing = [symbol for symbol in symbols if symbol not in instruments]
    if missing:
        raise SystemExit(f"universe symbols are not registered: {missing}")
    provider = YahooChartProvider(timeout=25, retries=2)
    service = _local_service()
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    rows = []
    failed = []
    sources = []
    raw_hashes: dict[str, str] = {}
    reject_totals = {
        "duplicate_count": 0, "ohlc_anomaly_count": 0, "conflicting_duplicate_count": 0,
        "quarantine_count": 0, "volume_missing_count": 0, "partial_rows": 0,
        "adjustment_reject_count": 0,
    }
    for index, symbol in enumerate(symbols, 1):
        meta = _member_meta(symbol, members)
        try:
            raw, source_name, raw_digest = _raw_bars(
                symbol, instruments[symbol], service, provider, start, fetch_end,
                refresh=args.refresh,
            )
            raw_hashes[symbol] = raw_digest
            raw = clip_fetch(raw, fetch_end)
            row, counts = compute_symbol(
                raw, instruments[symbol], provider.name, cutoff, meta,
                completed_only=completed_only, already_adjusted=source_name == "local",
            )
            rows.append(row)
            sources.append(source_name)
            for key in reject_totals:
                reject_totals[key] += int(counts[key])
            print(f"{index}/{len(symbols)} {symbol} {row['bias']} {row['as_of']} bars={row['bar_count']} src={source_name}", flush=True)
        except Exception as exc:
            failed.append({"symbol": symbol, **meta, "error": str(exc)})
            print(f"{index}/{len(symbols)} FAIL {symbol} {exc}", flush=True)
    incomplete = [item for item in (incomplete_entry(row) for row in rows) if item]
    payload = {
        "as_of": pd.Timestamp(cutoff).tz_convert("America/New_York").date().isoformat(),
        "data_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "universe": "QQQ与SPY 2026-10-23 权重前100，按代码去重，未按公司合并。SPCX 不在名单内，名次保留源空缺。",
        "price_basis": PRICE_BASIS,
        "research_only": True,
        "provider": provider.name,
        "completed_only": completed_only,
        "partial": bool(window["partial_mode"]),
        "rows": rows,
        "failed": failed,
        "incomplete": incomplete,
    }
    content_hash = _content_hash(rows)
    bar_count = {
        "total": sum(row["bar_count"] for row in rows),
        "by_symbol": {row["symbol"]: row["bar_count"] for row in rows},
    }
    raw_hash = hashlib.sha256(
        "\n".join(f"{symbol}:{raw_hashes[symbol]}" for symbol in sorted(raw_hashes)).encode("utf-8")
    ).hexdigest()
    universe = {
        "id": "qqq_spy_top100_20261023",
        "hash": hashlib.sha256(json.dumps(members, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "count": len(members),
    }
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    receipt = build_receipt(
        generated_at=generated_at,
        data_cutoff=payload["data_cutoff"],
        provider=provider.name,
        completed_only=completed_only,
        universe=universe,
        config_hash=_config_hash(),
        script_git_commit=_git_commit(),
        counts={
            "rows": len(rows),
            "failed": len(failed),
            "incomplete": len(incomplete),
            "partial": 1 if window["partial_mode"] else 0,
            **reject_totals,
            "sources": sorted(set(sources)),
        },
        failed=failed,
        incomplete=incomplete,
        content_hash=content_hash,
        bar_count=bar_count,
        raw_hash=raw_hash,
    )
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    OUT_RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_HTML.write_text(_page(payload, {
        "provider": provider.name,
        "data_cutoff": payload["data_cutoff"],
        "price_basis": PRICE_BASIS,
        "config_hash": receipt["config_hash"],
        "content_hash": content_hash,
        "script_git_commit": receipt["script_git_commit"],
    }), encoding="utf-8")
    summary = {
        "rows": len(rows),
        "failed": len(failed),
        "incomplete": [item["symbol"] for item in incomplete],
        "partial": receipt["counts"]["partial"],
        "completed_only": completed_only,
        "price_basis": PRICE_BASIS,
        "data_cutoff": payload["data_cutoff"],
        "last_bar": max((row["as_of"] for row in rows), default=None),
        "relative_volume_median": _median([row["relative_volume"] for row in rows]),
        "content_hash": content_hash,
        "raw_hash": raw_hash,
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def _median(values: list) -> float | None:
    numbers = sorted(float(item) for item in values if item is not None)
    if not numbers:
        return None
    middle = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[middle]
    return (numbers[middle - 1] + numbers[middle]) / 2


def _local_service():
    try:
        from inv_trend.data.api import HistoricalDataService
        return HistoricalDataService(ROOT / "data", startup_check=False)
    except Exception:
        return None


def _raw_bars(symbol, instrument, service, provider, start, cutoff, *, refresh: bool) -> tuple[pd.DataFrame, str, str]:
    if not refresh:
        local = _load_local(service, symbol, start, cutoff)
        if local is not None and len(local):
            return local, "local", _frame_digest(local)
        cached = _cache_path(cutoff, symbol)
        if cached.is_file():
            frame, digest = _read_cache(cached)
            return frame, "yahoo_cache", digest or _frame_digest(frame)
    fetched = provider.fetch(DownloadRequest(instrument, "D1", start, cutoff))
    digest = _write_cache(_cache_path(cutoff, symbol), fetched.frame)
    return fetched.frame, "yahoo_live", digest


def _page(payload: dict, meta: dict | None = None) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    banner = '<p class="banner show" id="banner">盘中快照</p>' if payload.get("partial") else ""
    header = meta or {}
    meta_html = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(header.get(key, '—')))}</dd></div>"
        for key, label in (
            ("provider", "数据来源"),
            ("data_cutoff", "数据截止"),
            ("price_basis", "价格口径"),
            ("config_hash", "配置哈希"),
            ("content_hash", "内容哈希"),
            ("script_git_commit", "脚本提交"),
        )
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100指标看板</title>
<style>
  :root {{ color-scheme: light; --ink:#1c1915; --muted:#5c564c; --line:#e4ddd0; --paper:#f6f3ec; --card:#fffdf8; --long:#1f6b45; --short:#8d2b1f; --mix:#8a5a12; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:15px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:24px 18px 48px; }}
  h1 {{ margin:0 0 6px; font-size:28px; }}
  .lede {{ color:var(--muted); margin:0 0 14px; }}
  .bar {{ position:sticky; top:0; background:rgba(246,243,236,.95); padding:10px 0 12px; border-bottom:1px solid var(--line); }}
  .controls {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
  input, select, button {{ font:inherit; border:1px solid #d5cec0; background:var(--card); border-radius:999px; padding:7px 12px; }}
  button {{ cursor:pointer; }}
  button.on {{ background:#0c5c56; color:#f4fffc; border-color:#0c5c56; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); }}
  th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
  th {{ cursor:pointer; font-size:13px; color:var(--muted); }}
  tr.item {{ cursor:pointer; }}
  tr.item:hover {{ background:#f3f7f6; }}
  .多 {{ color:var(--long); font-weight:700; }}
  .空 {{ color:var(--short); font-weight:700; }}
  .混, .平 {{ color:var(--mix); font-weight:700; }}
  .detail td {{ white-space:normal; background:#f8faf8; }}
  canvas {{ width:100%; height:160px; background:#fff; border:1px solid var(--line); border-radius:8px; }}
  .count {{ font-variant-numeric:tabular-nums; }}
  .banner {{ display:none; margin:0 0 10px; padding:6px 12px; border-radius:999px; background:#f8efd8; color:#8a5a12; font-weight:700; }}
  .banner.show {{ display:inline-block; }}
  .meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:6px 18px; margin:0 0 14px; padding:10px 14px; background:var(--card); border:1px solid var(--line); border-radius:10px; font-size:13px; }}
  .meta dt {{ color:var(--muted); }}
  .meta dd {{ margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }}
  .wrap {{ overflow-x:auto; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100成分 / 163 只</h1>
  {banner}
  <dl class="meta">{meta_html}</dl>
  <p class="lede" id="lede"></p>
  <div class="bar">
    <div class="controls">
      <input id="q" placeholder="搜索代码（前缀）" aria-label="搜索代码">
      <select id="fund"><option value="">全部基金</option><option>QQQ</option><option>SPY</option><option value="both">两边都有</option></select>
      <select id="bias"><option value="">全部方向</option><option value="多">三对 EMA 多头</option><option value="空">三对 EMA 空头</option><option value="混">方向混合</option></select>
      <button type="button" id="adx" aria-pressed="false">只看 ADX≥25</button>
      <button type="button" id="vol" aria-pressed="false">只看放量</button>
      <button type="button" id="reset">清空筛选</button>
      <span class="count" id="count"></span>
    </div>
  </div>
  <table>
    <thead><tr>
      <th data-k="symbol">代码</th><th data-k="funds">基金</th><th data-k="qqq_rank">QQQ</th><th data-k="spy_rank">SPY</th>
      <th data-k="close">原始收盘</th><th data-k="adjusted_close">复权收盘</th><th data-k="bias">EMA</th><th data-k="sma_bias">SMA</th><th data-k="macd">MACD</th>
      <th data-k="adx">ADX</th><th data-k="relative_volume">相对前20日均量</th><th data-k="as_of">日期</th>
    </tr></thead>
    <tbody id="body"></tbody>
  </table>
  <h2>指标不完整</h2>
  <ul id="incomplete"></ul>
  <h2>没有算出指标</h2>
  <ul id="fails"></ul>
</div>
<script>
const DATA = {data};
const state = {{ fund:"", bias:"", adx:false, vol:false, q:"", sort:"symbol", desc:false, open:"" }};
const body = document.getElementById("body");
document.getElementById("lede").textContent = DATA.universe + "。Yahoo research-only。price_basis=" + DATA.price_basis + "。截止 " + DATA.data_cutoff + "。成功 " + DATA.rows.length + "，失败 " + DATA.failed.length + "，预热不足 " + (DATA.incomplete || []).length + "。三对 EMA 都在快线之上为多，都在之下为空。仅指标结果，非投资建议。";
function warmText(row, key) {{
  if (!row.warmup_ok && (key === "144" || key === "169")) return "预热不足";
  const value = row.ema[key];
  return value === null || value === undefined ? "—" : value;
}}
function show(row) {{
  if (state.q && row.symbol.indexOf(state.q) !== 0) return false;
  if (state.fund === "both" && row.funds.indexOf(",") < 0) return false;
  if (state.fund && state.fund !== "both" && row.funds.indexOf(state.fund) < 0) return false;
  if (state.bias && row.bias !== state.bias) return false;
  if (state.adx && !(row.adx >= 25)) return false;
  if (state.vol && !(row.relative_volume >= 1.5)) return false;
  return true;
}}
function val(row, key) {{
  const v = row[key];
  return v === null || v === undefined || v === "" ? (state.desc ? -Infinity : Infinity) : v;
}}
function draw(canvas, spark) {{
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * 2, h = canvas.height = 320;
  ctx.clearRect(0,0,w,h);
  const series = [spark.close, spark.ema10, spark.ema20, spark.ema55];
  const nums = series.flat().filter(v => v !== null);
  if (!nums.length) return;
  const min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
  const colors = ["#1c1915", "#0c5c56", "#8a5a12", "#8d2b1f"];
  series.forEach((line, index) => {{
    ctx.beginPath(); ctx.strokeStyle = colors[index]; ctx.lineWidth = index ? 2 : 3;
    let started = false;
    line.forEach((v, i) => {{
      if (v === null) {{ started = false; return; }}
      const x = (i / Math.max(line.length - 1, 1)) * (w - 16) + 8;
      const y = h - 12 - ((v - min) / Math.max(max - min, 1e-9)) * (h - 24);
      if (!started) {{ ctx.moveTo(x, y); started = true; }} else ctx.lineTo(x, y);
    }});
    ctx.stroke();
  }});
}}
function render() {{
  const rows = DATA.rows.filter(show).sort((a, b) => {{
    const av = val(a, state.sort), bv = val(b, state.sort);
    if (av < bv) return state.desc ? 1 : -1;
    if (av > bv) return state.desc ? -1 : 1;
    return a.symbol < b.symbol ? -1 : 1;
  }});
  document.getElementById("count").textContent = rows.length + " / " + DATA.rows.length;
  body.innerHTML = rows.map(row => {{
    const open = state.open === row.symbol;
    const main = `<tr class="item" data-s="${{row.symbol}}"><td>${{row.symbol}}</td><td>${{row.funds}}</td><td>${{row.qqq_rank || "—"}}</td><td>${{row.spy_rank || "—"}}</td><td>${{row.close ?? "—"}}</td><td>${{row.adjusted_close ?? "—"}}</td><td class="${{row.bias}}">${{row.bias || "—"}} ${{row.pairs["10_20"]}}/${{row.pairs["10_55"]}}/${{row.pairs["20_55"]}}</td><td class="${{row.sma_bias}}">${{row.sma_bias || "—"}}</td><td class="${{row.macd}}">${{row.macd || "—"}}</td><td>${{row.adx ?? "—"}}</td><td>${{row.relative_volume ?? "—"}}</td><td>${{row.as_of}}</td></tr>`;
    const smaText = [5, 10, 20, 55, 120].map(period => {{
      const value = row.sma[String(period)];
      if (period === 120 && !row.warmup_ok) return "预热不足";
      return value === null || value === undefined ? "—" : value;
    }}).join(" / ");
    const extra = open ? `<tr class="detail"><td colspan="12">
      <dl class="meta">
        <div><dt>${{row.symbol}} 原始 / 复权收盘</dt><dd>${{row.close ?? "—"}} / ${{row.adjusted_close ?? "—"}}（因子 ${{row.adjustment_factor ?? "—"}}）</dd></div>
        <div><dt>SMA 5/10/20/55/120</dt><dd>${{smaText}}</dd></div>
        <div><dt>EMA 10/20/55</dt><dd>${{warmText(row,"10")}} / ${{warmText(row,"20")}} / ${{warmText(row,"55")}}</dd></div>
        <div><dt>EMA 144/169</dt><dd>${{warmText(row,"144")}} / ${{warmText(row,"169")}}</dd></div>
        <div><dt>ADX · +DI / −DI</dt><dd>${{row.adx ?? "—"}} · ${{row.plus_di ?? "—"}} / ${{row.minus_di ?? "—"}}</dd></div>
        <div><dt>ATR · MACD 柱</dt><dd>${{row.atr ?? "—"}} · ${{row.histogram ?? "—"}}</dd></div>
        <div><dt>通道 20 日 高 / 低</dt><dd>${{row.channel?.high_20 ?? "—"}} / ${{row.channel?.low_20 ?? "—"}}</dd></div>
        <div><dt>通道 55 日 高 / 低</dt><dd>${{row.channel?.high_55 ?? "—"}} / ${{row.channel?.low_55 ?? "—"}}</dd></div>
        <div><dt>成交量 / 前20日均量</dt><dd>${{row.volume ?? "—"}} / ${{row.volume_sma_20 ?? "—"}}（相对 ${{row.relative_volume ?? "—"}}）</dd></div>
        <div><dt>预热</dt><dd>${{row.warmup_ok ? "足够" : "预热不足"}}，${{row.bar_count}} 根</dd></div>
      </dl>
      <canvas id="c-${{row.symbol.replace(".","_")}}" aria-label="收盘与EMA"></canvas>
    </td></tr>` : "";
    return main + extra;
  }}).join("");
  rows.forEach(row => {{
    if (state.open !== row.symbol) return;
    const canvas = document.getElementById("c-" + row.symbol.replace(".","_"));
    if (canvas) draw(canvas, row.spark);
  }});
  body.querySelectorAll("tr.item").forEach(tr => tr.addEventListener("click", () => {{
    state.open = state.open === tr.dataset.s ? "" : tr.dataset.s;
    render();
  }}));
}}
document.getElementById("q").addEventListener("input", e => {{ state.q = e.target.value.trim().toUpperCase(); render(); }});
document.getElementById("fund").addEventListener("change", e => {{ state.fund = e.target.value; render(); }});
document.getElementById("bias").addEventListener("change", e => {{ state.bias = e.target.value; render(); }});
document.getElementById("adx").addEventListener("click", e => {{ state.adx = !state.adx; e.target.classList.toggle("on", state.adx); e.target.setAttribute("aria-pressed", String(state.adx)); render(); }});
document.getElementById("vol").addEventListener("click", e => {{ state.vol = !state.vol; e.target.classList.toggle("on", state.vol); e.target.setAttribute("aria-pressed", String(state.vol)); render(); }});
document.getElementById("reset").addEventListener("click", () => {{
  Object.assign(state, {{ fund:"", bias:"", adx:false, vol:false, q:"", open:"" }});
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("bias").value = "";
  const adx = document.getElementById("adx"), vol = document.getElementById("vol");
  adx.classList.remove("on"); vol.classList.remove("on");
  adx.setAttribute("aria-pressed", "false"); vol.setAttribute("aria-pressed", "false");
  render();
}});
document.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {{
  if (state.sort === th.dataset.k) state.desc = !state.desc; else {{ state.sort = th.dataset.k; state.desc = false; }}
  render();
}}));
document.getElementById("fails").innerHTML = DATA.failed.map(item => `<li>${{item.symbol}}（${{item.funds}}）：${{item.error}}</li>`).join("") || "<li>没有</li>";
document.getElementById("incomplete").innerHTML = (DATA.incomplete || []).map(item => `<li>${{item.symbol}}：${{item.bar_count}} 根，${{item.reason}}，预热不足</li>`).join("") || "<li>没有</li>";
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
