from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.market_panels import (
    aggregate_market,
    append_history,
    derive_symbol_decision,
)
from inv_trend.application.strategy_config import load_resolved_run_config
from inv_trend.core.features import FeatureRequest, PreparedBars
from inv_trend.core.strategy.daily.strategy_checks import (
    analyze_prepared_strategy_checks,
    prepare_daily_strategy_checks,
)

CONFIG = load_resolved_run_config().daily_checks
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


def _bars(closes: list[float], *, start: str = "2024-01-01") -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(len(closes), 2_000_000.0),
        },
        index=pd.date_range(start, periods=len(closes), freq="B", tz="UTC", name="timestamp"),
    )


def _prepared(bars: pd.DataFrame) -> pd.DataFrame:
    return PreparedBars.build(bars, REQUEST).frame


def _decision(bars: pd.DataFrame, *, equity: float = 100_000.0, risk_pct: float = 0.005, stop_n: float = 2.0):
    prepared = _prepared(bars)
    decision = derive_symbol_decision(
        bars, prepared, CONFIG,
        session_anchor=bars.index[0], equity=equity, risk_pct=risk_pct, stop_n=stop_n,
    )
    return decision, prepared


def _breakout_series() -> list[float]:
    """Recover past the 20-day high while staying under the 55-day high."""

    ramp = np.linspace(50.0, 95.0, 240)
    decline = np.linspace(95.0, 85.0, 40)
    recover = np.linspace(85.0, 92.0, 19)
    return [*ramp, *decline, *recover, 93.5]


def test_rating_matches_the_daily_checks_pipeline_verbatim():
    bars = _bars(_breakout_series())
    decision, prepared = _decision(bars)
    expected = analyze_prepared_strategy_checks(
        prepare_daily_strategy_checks(
            bars, CONFIG, session_anchor=bars.index[0], prepared_base=prepared,
        )
    )["rating"]
    assert decision["rating"]["grade"] == expected["grade"]
    assert decision["rating"]["direction"] == expected["direction"]
    assert decision["family_votes"] == expected["family_votes"]
    assert decision["rating"]["quality_adjustments"] == expected["quality_adjustments"]


def test_breakout_trigger_and_distance_signs():
    bars = _bars(_breakout_series())
    decision, _prepared = _decision(bars)
    entries = {(item["period"], item["direction"]): item for item in decision["breakouts"]}
    assert entries[(20, "long")]["triggered"] is True
    assert entries[(20, "long")]["distance_pct"] < 0
    assert entries[(55, "long")]["triggered"] is False
    assert 0 < entries[(55, "long")]["distance_pct"] <= 2.0
    assert entries[(20, "short")]["triggered"] is False
    assert entries[(20, "short")]["distance_pct"] > 0
    assert set(entries) == {(20, "long"), (20, "short"), (55, "long"), (55, "short")}


def test_stops_follow_stop_n_times_atr():
    bars = _bars(_breakout_series())
    decision, prepared = _decision(bars, stop_n=2.0)
    close = float(prepared.iloc[-1]["close"])
    atr = float(prepared.iloc[-1]["atr"])
    assert decision["stops"]["long"] == pytest.approx(round(close - 2.0 * atr, 4))
    assert decision["stops"]["short"] == pytest.approx(round(close + 2.0 * atr, 4))
    exits = decision["exits"]
    row = prepared.iloc[-1]
    assert exits["long_fast"] == pytest.approx(round(float(row["channel_low_10"]), 4))
    assert exits["short_slow"] == pytest.approx(round(float(row["channel_high_20"]), 4))


def test_suggested_qty_floors_the_risk_budget():
    bars = _bars(_breakout_series())
    decision, prepared = _decision(bars, equity=100_000.0, risk_pct=0.005)
    atr = float(prepared.iloc[-1]["atr"])
    expected = int(100_000.0 * 0.005 / (2.0 * atr))  # floor via step 1
    assert decision["suggested_qty"]["long"] == expected
    assert decision["suggested_qty"]["long"] * 2.0 * atr <= 500.0 + 2.0 * atr


def test_short_history_degrades_to_nulls_without_raising():
    bars = _bars(list(np.linspace(100.0, 110.0, 12)))
    decision, _prepared = _decision(bars)
    assert decision["stops"] == {"long": None, "short": None}
    assert decision["suggested_qty"] == {"long": 0, "short": 0}
    assert decision["rating"]["grade"] in {"NONE", "CONFLICT", "C"}


def test_constant_volatility_ranks_as_normal_state():
    bars = _bars([100.0] * 200)
    decision, _prepared = _decision(bars)
    assert decision["volatility"]["state"] == "normal"
    assert 0.0 <= decision["volatility"]["percentile"] <= 1.0


def _row(symbol: str, *, close, sma20, ema55, macd, adx, rel, grade, direction, breakouts, vol_state="normal"):
    return {
        "symbol": symbol,
        "close": close,
        "sma": {"20": sma20},
        "ema": {"55": ema55},
        "macd": macd,
        "adx": adx,
        "relative_volume": rel,
        "decision": {
            "rating": {"grade": grade, "direction": direction},
            "volatility": {"state": vol_state},
            "breakouts": breakouts,
        },
    }


def test_aggregate_market_counts_medians_and_temperature():
    rows = [
        _row("AAA", close=10.0, sma20=9.0, ema55=8.0, macd="多", adx=30.0, rel=2.0,
             grade="A", direction="long",
             breakouts=[{"period": 20, "direction": "long", "triggered": True, "distance_pct": -0.5},
                        {"period": 55, "direction": "long", "triggered": False, "distance_pct": 1.2}]),
        _row("BBB", close=8.0, sma20=9.0, ema55=10.0, macd="空", adx=18.0, rel=0.8,
             grade="CONFLICT", direction=None,
             breakouts=[{"period": 20, "direction": "short", "triggered": True, "distance_pct": -0.1},
                        {"period": 55, "direction": "short", "triggered": True, "distance_pct": -0.2}]),
        _row("CCC", close=9.5, sma20=9.0, ema55=9.0, macd="混", adx=None, rel=None,
             grade="B", direction="short",
             breakouts=[{"period": 20, "direction": "short", "triggered": False, "distance_pct": 1.9},
                        {"period": 55, "direction": "short", "triggered": False, "distance_pct": 5.0}],
             vol_state="low"),
    ]
    market = aggregate_market(
        rows, as_of="2026-11-05", generated_at="2026-11-05T00:00:00Z",
        data_health={"rows": 3, "failed": 0},
    )
    assert market["rating_dist"] == {"A_long": 1, "CONFLICT": 1, "B_short": 1}
    assert market["breakout_counts"] == {"long_20": 1, "long_55": 0, "short_20": 1, "short_55": 1}
    assert market["volatility_counts"] == {"low": 1, "normal": 2, "high": 0, "unavailable": 0}
    assert market["breadth"]["above_sma20_pct"] == 66.7
    assert market["breadth"]["above_ema55_pct"] == 66.7
    assert market["breadth"]["macd_long_pct"] == 33.3
    assert market["breadth"]["adx_strong_pct"] == 33.3
    assert market["median_adx"] == 24.0
    assert market["median_relative_volume"] == 1.4
    assert market["triggered_count"] == 2
    assert market["near_count"] == 1  # CCC only; AAA's 55-day near entry is masked by its triggered state
    assert 0.0 <= market["temperature_score"] <= 100.0
    net_pct = (1 - 2) / 3 * 100  # one long breakout, two short breakouts over 3 rows
    expected = 0.4 * 33.3 + 0.3 * 66.7 + 0.3 * net_pct
    assert market["temperature_score"] == pytest.approx(round(min(100.0, max(0.0, expected)), 1))


def test_append_history_replaces_same_day_and_caps_records(tmp_path):
    path = tmp_path / "历史.json"
    first = append_history(path, {"as_of": "2026-11-04", "temperature_score": 60.0})
    assert first == {"appended": True, "rebuilt": False, "records": 1}
    append_history(path, {"as_of": "2026-11-05", "temperature_score": 55.0})
    summary = append_history(path, {"as_of": "2026-11-05", "temperature_score": 56.5})
    assert summary["records"] == 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [item["temperature_score"] for item in payload["records"]] == [60.0, 56.5]
    for index in range(300):
        append_history(path, {"as_of": f"2027-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}", "v": index})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["records"]) == 250


def test_append_history_rebuilds_from_a_corrupt_file(tmp_path):
    path = tmp_path / "历史.json"
    path.write_text("{not json", encoding="utf-8")
    summary = append_history(path, {"as_of": "2026-11-05", "temperature_score": 42.0})
    assert summary["rebuilt"] is True
    assert summary["records"] == 1
    leftover = list(tmp_path.glob("历史.json.corrupt-*"))
    assert len(leftover) == 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["records"][0]["as_of"] == "2026-11-05"
