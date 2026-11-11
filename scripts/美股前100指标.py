"""计算 QQQ、SPY 前 100 去重股票的日线指标，并写出可筛选的单文件页面。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from inv_trend.core.features import FeatureRequest, PreparedBars
from inv_trend.data.calendar import latest_completed_us_bar, us_session_bounds
from inv_trend.data.config import load_instruments
from inv_trend.data.models import InstrumentConfig
from inv_trend.data.processing import select_completed_bars
from inv_trend.data.providers import YahooChartProvider
from inv_trend.data.snapshot import resolve_snapshot_bars
from inv_trend.application.market_panels import (
    aggregate_market,
    allocate_portfolio,
    append_history,
    derive_symbol_decision,
)
from inv_trend.application.strategy_config import load_resolved_run_config
from inv_trend.observability.静态展示.快照看板 import render_snapshot_page as _page
from inv_trend.observability.静态展示.方向总览 import render_direction_overview as _map_page
from inv_trend.observability.静态展示.决策面板 import render_decision_desk as _desk_page
from inv_trend.observability.静态展示.市场温度面板 import render_market_thermometer as _thermo_page
from inv_trend.observability.静态展示.目录 import GENERATED_OUTPUT_NAMES

ROOT = Path(__file__).resolve().parents[1]
OUT_HTML = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["snapshot_html"]
OUT_MAP_HTML = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["direction_html"]
OUT_DESK_HTML = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["decision_html"]
OUT_THERMO_HTML = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["thermometer_html"]
OUT_HISTORY = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["history_json"]
OUT_JSON = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["data_json"]
OUT_RECEIPT = ROOT / "deliverables" / GENERATED_OUTPUT_NAMES["receipt_json"]
CACHE_ROOT = ROOT / ".tmp" / "yahoo-us-top100"
ASSETS_PATH = ROOT / "src" / "inv_trend" / "data" / "config" / "assets.yaml"
MIN_BARS = 169
WARMUP_FIELDS = ("120", "144", "169")
PRICE_BASIS = "provider_adjusted_close"

# The temperature-history archive is a fixed projection of the market block.
# Keep the field list explicit so the archived schema cannot drift silently.
HISTORY_FIELDS = (
    "as_of", "generated_at", "rating_dist", "breakout_counts",
    "volatility_counts", "breadth", "median_adx", "median_relative_volume",
    "temperature_score", "triggered_count", "near_count",
)

UNIVERSE_PATH = ROOT / "config" / "universes" / "qqq_spy_top100_20261023.json"
UNIVERSE_ID = "qqq_spy_top100_20261023"

REQUEST = FeatureRequest(
    atr_period=14,
    donchian_periods=(10, 20, 55),
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
    """Load the registered universe file; validate it at this boundary."""

    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    members = payload.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("universe file has no members")
    if payload.get("count") != len(members):
        raise ValueError("universe count does not match its members")
    seen: set[str] = set()
    for item in members:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str) or not item["symbol"]:
            raise ValueError("universe member symbol is required")
        if item["symbol"] in seen:
            raise ValueError(f"duplicate universe symbol: {item['symbol']}")
        seen.add(item["symbol"])
        for name in ("qqq_rank", "spy_rank"):
            value = item.get(name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"invalid {name} for {item['symbol']}")
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


def _ema_vote(row) -> tuple[dict, str]:
    """Three EMA fast/slow pairs and their aggregate display direction."""

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
    return pairs, bias


def _sma_alignment(row, warmup_ok: bool) -> tuple[list, str]:
    """Warmup-masked SMA values plus their ordered-alignment label."""

    periods = (5, 10, 20, 55, 120)
    smas = [_warm_number(row.get(f"sma_{period}_lag_0"), period, warmup_ok) for period in periods]
    raw_smas = [row.get(f"sma_{period}_lag_0") for period in periods]
    if warmup_ok and all(pd.notna(item) for item in raw_smas) and all(raw_smas[i] > raw_smas[i + 1] for i in range(4)):
        sma_bias = "多"
    elif warmup_ok and all(pd.notna(item) for item in raw_smas) and all(raw_smas[i] < raw_smas[i + 1] for i in range(4)):
        sma_bias = "空"
    elif warmup_ok and all(pd.notna(item) for item in raw_smas):
        sma_bias = "混"
    else:
        sma_bias = ""
    return smas, sma_bias


def _spark_block(tail: pd.DataFrame) -> dict:
    """The 120-bar close/EMA series drawn in the snapshot table detail row."""

    return {
        "date": [str(pd.Timestamp(item).tz_convert("America/New_York").date()) for item in tail.index],
        "close": [_num(item) for item in tail["close"]],
        "ema10": [_num(item) for item in tail["ema_10"]],
        "ema20": [_num(item) for item in tail["ema_20"]],
        "ema55": [_num(item) for item in tail["ema_55"]],
    }


def _timeline_block(prepared: pd.DataFrame) -> list:
    """The last ten bars consumed by the decision-desk timeline."""

    return [
        {
            "date": str(pd.Timestamp(stamp).tz_convert("America/New_York").date()),
            "close": _num(item.get("close")),
            "histogram": _num(item.get("histogram")),
            "adx": _num(item.get("adx")),
        }
        for stamp, item in prepared.tail(10).iterrows()
    ]


def indicator_snapshot(symbol: str, prepared: pd.DataFrame, meta: dict, *, partial: bool) -> dict:
    row = prepared.iloc[-1]
    bar_count = int(len(prepared))
    warmup_ok = bar_count >= MIN_BARS
    pairs, bias = _ema_vote(row)
    smas, sma_bias = _sma_alignment(row, warmup_ok)
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
            "high_10": _num(row.get("channel_high_10")),
            "low_10": _num(row.get("channel_low_10")),
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
        "spark": _spark_block(tail),
        "timeline10": _timeline_block(prepared),
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
    decision_config: dict | None = None,
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
        "decision_config": decision_config or {},
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


def _history_records() -> list[dict]:
    """Existing temperature history records; tolerate a corrupt file (page renders empty)."""

    if not OUT_HISTORY.is_file():
        return []
    try:
        payload = json.loads(OUT_HISTORY.read_text(encoding="utf-8"))
        return list(payload.get("records") or [])
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        # Read path stays side-effect free: a broken archive renders an empty
        # history. The write path (append_history) is the one that renames the
        # corrupt file and rebuilds it.
        return []


def _config_hash() -> str:
    return hashlib.sha256(ASSETS_PATH.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def compute_symbol(
    raw: pd.DataFrame, instrument: InstrumentConfig, source: str, now: datetime, meta: dict, *,
    completed_only: bool, decision_config: dict | None = None,
) -> tuple[dict, dict]:
    used, counts = select_completed_bars(
        raw, instrument, "D1", source, now=now, completed_only=completed_only,
    )
    features = _feature_frame(used)
    if features.empty:
        raise RuntimeError("no completed bars")
    prepared = PreparedBars.build(features, REQUEST).frame
    partial = (not completed_only) and bool(counts["partial_rows"])
    row = indicator_snapshot(instrument.symbol, prepared, meta, partial=partial)
    if decision_config:
        decision = derive_symbol_decision(
            features, prepared, decision_config["daily_checks"],
            session_anchor=features.index[-1], equity=decision_config["equity"],
            risk_pct=decision_config["risk_pct"], stop_n=decision_config["stop_n"],
        )
        row["decision"] = decision
    return row, counts


def _parse_args(argv: list[str] | None):
    """CLI surface; install the explicit proxy in-process because env proxies can be overridden."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--include-partial", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--equity", type=float, default=None)
    parser.add_argument("--risk-pct", type=float, default=None)
    parser.add_argument("--proxy", default=None,
                        help="显式代理（如 http://127.0.0.1:7897）。环境变量代理在部分运行环境会被强制覆盖，此选项在进程内直接生效。")
    args = parser.parse_args(argv)
    if args.proxy:
        from urllib.request import ProxyHandler, build_opener, install_opener
        install_opener(build_opener(ProxyHandler({"http": args.proxy, "https": args.proxy})))
    return args


def _build_decision_config(args) -> dict:
    """Risk-budget assumptions shared by the decision desk and portfolio allocator."""

    resolved = load_resolved_run_config()
    return {
        "equity": float(args.equity if args.equity is not None else resolved.risk["initial_equity"]),
        "risk_pct": float(args.risk_pct if args.risk_pct is not None else 0.005),
        "stop_n": float(resolved.rules["stop_n"]),
        "daily_checks": resolved.daily_checks,
        # Canonical portfolio caps; the decision desk allocates the risk budget
        # against these instead of taking every trigger at full size.
        "caps": {
            key: float(resolved.rules[key])
            for key in ("max_total_1n_risk_pct", "max_direction_1n_risk_pct",
                        "max_total_leverage", "max_direction_leverage")
            if key in resolved.rules
        },
    }


def _prepare_run(args, completed_only: bool) -> dict:
    """Resolve the fetch window, the registered universe and the data providers."""

    window = resolve_window(datetime.now(timezone.utc), args.as_of, completed_only=completed_only)
    instruments = load_instruments()
    members = universe_members()
    symbols = [item["symbol"] for item in members]
    missing = [symbol for symbol in symbols if symbol not in instruments]
    if missing:
        raise SystemExit(f"universe symbols are not registered: {missing}")
    return {
        "window": window,
        "cutoff": window["completeness_now"],
        "fetch_end": window["fetch_end"],
        "instruments": instruments,
        "members": members,
        "symbols": symbols,
        "provider": YahooChartProvider(timeout=25, retries=2),
        "service": _local_service(),
        "start": datetime(2023, 1, 1, tzinfo=timezone.utc),
    }


def _fetch_rows(context: dict, *, completed_only: bool, decision_config: dict, refresh: bool) -> dict:
    """One row per symbol; a per-symbol failure is recorded instead of aborting the run."""

    symbols = context["symbols"]
    members = context["members"]
    instruments = context["instruments"]
    provider = context["provider"]
    service = context["service"]
    fetch_end = context["fetch_end"]
    cutoff = context["cutoff"]
    start = context["start"]
    rows, failed, sources = [], [], []
    raw_hashes: dict[str, str] = {}
    reject_totals = {
        "duplicate_count": 0, "ohlc_anomaly_count": 0, "conflicting_duplicate_count": 0,
        "quarantine_count": 0, "volume_missing_count": 0, "partial_rows": 0,
        "adjustment_reject_count": 0,
    }
    for index, symbol in enumerate(symbols, 1):
        meta = _member_meta(symbol, members)
        try:
            resolved = resolve_snapshot_bars(
                symbol=symbol, instrument=instruments[symbol], service=service,
                provider=provider, start=start, end=fetch_end,
                cache_root=CACHE_ROOT, refresh=refresh,
            )
            raw_hashes[symbol] = resolved.digest
            raw = clip_fetch(resolved.frame, fetch_end)
            row, counts = compute_symbol(
                raw, instruments[symbol], provider.name, cutoff, meta,
                completed_only=completed_only, decision_config=decision_config,
            )
            rows.append(row)
            sources.append(resolved.source)
            for key in reject_totals:
                reject_totals[key] += int(counts[key])
            print(f"{index}/{len(symbols)} {symbol} {row['bias']} {row['as_of']} bars={row['bar_count']} src={resolved.source}", flush=True)
        except Exception as exc:
            failed.append({"symbol": symbol, **meta, "error": str(exc)})
            print(f"{index}/{len(symbols)} FAIL {symbol} {exc}", flush=True)
    return {
        "rows": rows, "failed": failed, "sources": sources,
        "raw_hashes": raw_hashes, "reject_totals": reject_totals,
    }


def _build_outputs(context: dict, fetched: dict, *, decision_config: dict, completed_only: bool) -> dict:
    """Pure assembly of payload, receipt hashes and page metadata."""

    rows = fetched["rows"]
    failed = fetched["failed"]
    sources = fetched["sources"]
    raw_hashes = fetched["raw_hashes"]
    reject_totals = fetched["reject_totals"]
    window = context["window"]
    cutoff = context["cutoff"]
    members = context["members"]
    provider_name = context["provider"].name
    incomplete = [item for item in (incomplete_entry(row) for row in rows) if item]
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    as_of = pd.Timestamp(cutoff).tz_convert("America/New_York").date().isoformat()
    data_health = {
        "rows": len(rows),
        "failed": len(failed),
        "failed_symbols": [item["symbol"] for item in failed],
        "incomplete": len(incomplete),
        "partial": 1 if window["partial_mode"] else 0,
        "sources": sorted(set(sources)),
    }
    market = aggregate_market(rows, as_of=as_of, generated_at=generated_at, data_health=data_health)
    portfolio = allocate_portfolio(
        rows,
        equity=decision_config["equity"],
        risk_pct=decision_config["risk_pct"],
        stop_n=decision_config["stop_n"],
        caps=decision_config.get("caps") or None,
    )
    payload = {
        "as_of": as_of,
        "data_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "universe": "QQQ与SPY 2026-10-23 权重前100，按代码去重，未按公司合并。SPCX 不在名单内，名次保留源空缺。",
        "price_basis": PRICE_BASIS,
        "research_only": True,
        "provider": provider_name,
        "completed_only": completed_only,
        "partial": bool(window["partial_mode"]),
        "rows": rows,
        "failed": failed,
        "incomplete": incomplete,
        "market": market,
        "portfolio": portfolio,
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
        "id": UNIVERSE_ID,
        "hash": hashlib.sha256(json.dumps(members, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "count": len(members),
    }
    receipt = build_receipt(
        generated_at=generated_at,
        data_cutoff=payload["data_cutoff"],
        provider=provider_name,
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
        decision_config={
            "equity": decision_config["equity"],
            "risk_pct": decision_config["risk_pct"],
            "stop_n": decision_config["stop_n"],
        },
    )
    page_meta = {
        "provider": provider_name,
        "data_cutoff": payload["data_cutoff"],
        "price_basis": PRICE_BASIS,
        "config_hash": receipt["config_hash"],
        "content_hash": content_hash,
        "script_git_commit": receipt["script_git_commit"],
    }
    return {
        "payload": payload, "receipt": receipt, "page_meta": page_meta,
        "market": market, "portfolio": portfolio,
        "rows": rows, "failed": failed, "incomplete": incomplete,
        "content_hash": content_hash, "raw_hash": raw_hash,
        "decision_config": decision_config,
    }


def _write_outputs(outputs: dict, *, completed_only: bool, partial_mode: bool):
    """Write the seven generated files, then append the temperature history."""

    payload = outputs["payload"]
    page_meta = outputs["page_meta"]
    receipt = outputs["receipt"]
    market = outputs["market"]
    decision_config = outputs["decision_config"]
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    OUT_RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_HTML.write_text(_page(payload, page_meta), encoding="utf-8")
    OUT_MAP_HTML.write_text(_map_page(payload, page_meta), encoding="utf-8")
    OUT_DESK_HTML.write_text(_desk_page(payload, {**page_meta,
        "equity": decision_config["equity"],
        "risk_pct": decision_config["risk_pct"],
        "stop_n": decision_config["stop_n"],
        "caps": decision_config.get("caps") or {},
    }), encoding="utf-8")
    history_summary = None
    if completed_only and not partial_mode:
        history_summary = append_history(
            OUT_HISTORY, {key: market[key] for key in HISTORY_FIELDS}
        )
    OUT_THERMO_HTML.write_text(_thermo_page(payload, page_meta, _history_records()), encoding="utf-8")
    return history_summary


def _build_summary(outputs: dict, *, completed_only: bool, history_summary) -> dict:
    """The one-line JSON summary printed for the daily automation."""

    payload = outputs["payload"]
    receipt = outputs["receipt"]
    market = outputs["market"]
    rows = outputs["rows"]
    failed = outputs["failed"]
    incomplete = outputs["incomplete"]
    decision_config = outputs["decision_config"]
    return {
        "rows": len(rows),
        "failed": len(failed),
        "incomplete": [item["symbol"] for item in incomplete],
        "partial": receipt["counts"]["partial"],
        "completed_only": completed_only,
        "price_basis": PRICE_BASIS,
        "data_cutoff": payload["data_cutoff"],
        "last_bar": max((row["as_of"] for row in rows), default=None),
        "relative_volume_median": _median([row["relative_volume"] for row in rows]),
        "content_hash": outputs["content_hash"],
        "raw_hash": outputs["raw_hash"],
        "decision_config": {
            "equity": decision_config["equity"],
            "risk_pct": decision_config["risk_pct"],
            "stop_n": decision_config["stop_n"],
        },
        "market": {
            "temperature_score": market["temperature_score"],
            "triggered_count": market["triggered_count"],
            "near_count": market["near_count"],
        },
        "history": history_summary,
    }


def main(argv: list[str] | None = None) -> dict:
    """Orchestrate fetch -> assemble -> write -> summarize; each stage is a pure step."""

    args = _parse_args(argv)
    completed_only = not args.include_partial
    decision_config = _build_decision_config(args)
    context = _prepare_run(args, completed_only)
    fetched = _fetch_rows(
        context, completed_only=completed_only,
        decision_config=decision_config, refresh=args.refresh,
    )
    outputs = _build_outputs(
        context, fetched, decision_config=decision_config, completed_only=completed_only,
    )
    history_summary = _write_outputs(
        outputs, completed_only=completed_only, partial_mode=context["window"]["partial_mode"],
    )
    summary = _build_summary(outputs, completed_only=completed_only, history_summary=history_summary)
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


if __name__ == "__main__":
    main()