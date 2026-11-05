"""Market-level aggregation and the temperature history for the panels."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

HISTORY_SCHEMA_VERSION = 1
NEAR_TRIGGER_PCT = 2.0
ADX_STRONG = 25.0


def aggregate_market(rows: list[dict], *, as_of: str, generated_at: str, data_health: dict) -> dict:
    """Aggregate per-symbol decision rows into the market block."""

    ratings: dict[str, int] = {}
    breakout_counts = {"long_20": 0, "long_55": 0, "short_20": 0, "short_55": 0}
    volatility_counts = {"low": 0, "normal": 0, "high": 0, "unavailable": 0}
    macd_long = above_sma20 = above_ema55 = adx_strong = 0
    counted = 0
    adx_values: list[float] = []
    rel_volumes: list[float] = []
    triggered = 0
    triggered_ab = 0
    near = 0
    for row in rows:
        decision = row.get("decision") or {}
        rating = decision.get("rating") or {}
        grade = rating.get("grade") or "NONE"
        direction = rating.get("direction")
        if grade in ("CONFLICT", "NONE"):
            key = grade
        else:
            key = f"{grade}_{'long' if direction == 'long' else 'short'}"
        ratings[key] = ratings.get(key, 0) + 1
        entries = decision.get("breakouts") or []
        for item in entries:
            if item.get("triggered"):
                breakout_counts[f"{item['direction']}_{item['period']}"] = (
                    breakout_counts.get(f"{item['direction']}_{item['period']}", 0) + 1
                )
        symbol_triggered = any(item.get("triggered") for item in entries)
        symbol_near = any(
            not item.get("triggered")
            and item.get("distance_pct") is not None
            and 0 < item["distance_pct"] <= NEAR_TRIGGER_PCT
            for item in entries
        )
        strong_triggered = (
            symbol_triggered and grade in ("A", "B") and direction in ("long", "short")
        )
        if symbol_triggered:
            triggered += 1
            if strong_triggered:
                triggered_ab += 1
        elif symbol_near:
            near += 1
        state = (decision.get("volatility") or {}).get("state")
        volatility_counts[state if state in ("low", "normal", "high") else "unavailable"] += 1
        close, adx = row.get("close"), row.get("adx")
        sma20 = (row.get("sma") or {}).get("20")
        ema55 = (row.get("ema") or {}).get("55")
        rel = row.get("relative_volume")
        macd_long += 1 if row.get("macd") == "多" else 0
        if close is not None and sma20 is not None:
            above_sma20 += 1 if close > sma20 else 0
        if close is not None and ema55 is not None:
            above_ema55 += 1 if close > ema55 else 0
        if adx is not None:
            adx_values.append(float(adx))
            adx_strong += 1 if adx >= ADX_STRONG else 0
        if rel is not None:
            rel_volumes.append(float(rel))
        if adx is not None and row.get("macd") is not None:
            counted += 1
    total = max(len(rows), 1)
    long_breakouts = breakout_counts["long_20"] + breakout_counts["long_55"]
    short_breakouts = breakout_counts["short_20"] + breakout_counts["short_55"]
    macd_long_pct = macd_long / total * 100.0
    above_ema55_pct = above_ema55 / total * 100.0
    net_breakout_pct = (long_breakouts - short_breakouts) / total * 100.0
    temperature = 0.4 * macd_long_pct + 0.3 * above_ema55_pct + 0.3 * net_breakout_pct
    return {
        "as_of": as_of,
        "generated_at": generated_at,
        "rating_dist": ratings,
        "breakout_counts": breakout_counts,
        "volatility_counts": volatility_counts,
        "breadth": {
            "above_sma20_pct": _round1(above_sma20 / total * 100.0),
            "above_ema55_pct": _round1(above_ema55_pct),
            "macd_long_pct": _round1(macd_long_pct),
            "adx_strong_pct": _round1(adx_strong / total * 100.0),
        },
        "median_adx": _round1(statistics.median(adx_values)) if adx_values else None,
        "median_relative_volume": _round1(statistics.median(rel_volumes)) if rel_volumes else None,
        "temperature_score": _round1(min(100.0, max(0.0, temperature))),
        "triggered_count": triggered,
        # The desk only executes A/B-grade triggers; expose that count so the
        # thermometer and the desk cannot report different "triggered" totals.
        "triggered_ab_count": triggered_ab,
        "near_count": near,
        "counted_rows": counted,
        "data_health": data_health,
    }


def append_history(path: Path, record: dict, *, max_days: int = 250) -> dict:
    """Append (or same-day replace) one temperature record; keep the newest ``max_days``.

    A corrupted history file is renamed aside (``.corrupt-<timestamp>``) and
    rebuilt from the single new record instead of failing the run.
    """

    records: list[dict] = []
    rebuilt = False
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = list(payload.get("records") or [])
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path.rename(path.with_name(path.name + f".corrupt-{stamp}"))
            records = []
            rebuilt = True
    as_of = record["as_of"]
    records = [item for item in records if item.get("as_of") != as_of]
    records.append(dict(record))
    records.sort(key=lambda item: str(item.get("as_of")))
    records = records[-max_days:]
    path.write_text(
        json.dumps({"schema_version": HISTORY_SCHEMA_VERSION, "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"appended": True, "rebuilt": rebuilt, "records": len(records)}


def _round1(value) -> float | None:
    return None if value is None else round(float(value), 1)
