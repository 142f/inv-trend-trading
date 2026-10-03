from __future__ import annotations

import json

import pandas as pd
import pytest

from inv_trend.adapters.detector.indicators.daily_signals import (
    analyze_daily_signals,
    prepare_daily_signal_frame,
)


def _bars(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    complete: list[bool] | None = None,
) -> pd.DataFrame:
    closes = [float(value) for value in closes]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2024-01-01", periods=len(closes), freq="D", tz="UTC"
            ),
            "open": closes,
            "high": highs if highs is not None else [value + 1.0 for value in closes],
            "low": lows if lows is not None else [value - 1.0 for value in closes],
            "close": closes,
            "volume": [1_000.0] * len(closes),
            "is_complete": complete if complete is not None else [True] * len(closes),
        }
    )


def _flat_then_breakout(last_close: float) -> pd.DataFrame:
    # The final close is compared against exactly 55 prior daily bars.
    closes = [100.0] * 55 + [last_close]
    return _bars(
        closes,
        highs=[110.0] * 55 + [max(110.0, last_close)],
        lows=[90.0] * 55 + [min(90.0, last_close)],
    )


@pytest.mark.parametrize(
    ("last_close", "event", "direction"),
    [
        (111.0, "breakout_up", "long"),
        (89.0, "breakout_down", "short"),
    ],
)
def test_turtle_20_and_55_use_prior_bars_and_strict_breakout(
    last_close: float, event: str, direction: str
) -> None:
    result = analyze_daily_signals(_flat_then_breakout(last_close))

    for name in ("turtle_20", "turtle_55"):
        indicator = result["indicators"][name]
        assert indicator["status"] == "ready"
        assert indicator["event"] == event
        assert indicator["direction"] == direction
    assert [item["indicator"] for item in result["signals"][:2]] == [
        "turtle_20",
        "turtle_55",
    ]

    # A close exactly at the prior channel is not a breakout.
    equal = analyze_daily_signals(_flat_then_breakout(110.0))
    assert equal["indicators"]["turtle_20"]["event"] is None
    assert equal["indicators"]["turtle_55"]["event"] is None
    assert equal["indicators"]["turtle_20"]["channel_high"] == 110.0


def test_turtle_20_is_independent_when_55_is_still_warming_up() -> None:
    closes = [100.0] * 20 + [111.0]
    result = analyze_daily_signals(
        _bars(
            closes,
            highs=[110.0] * 20 + [111.0],
            lows=[90.0] * 21,
        )
    )

    assert result["indicators"]["turtle_20"]["event"] == "breakout_up"
    assert result["indicators"]["turtle_55"]["status"] == "unavailable"
    assert {
        "indicator": "turtle_20",
        "event": "breakout_up",
        "direction": "long",
        "signal_time": "2024-01-21T00:00:00+00:00",
        "breakout_level": 110.0,
    } in result["signals"]


@pytest.mark.parametrize(
    ("last_close", "event", "direction"),
    [
        (110.0, "golden_cross", "long"),
        (90.0, "death_cross", "short"),
    ],
)
def test_sma_10_20_crosses_include_current_completed_close(
    last_close: float, event: str, direction: str
) -> None:
    result = analyze_daily_signals(_bars([100.0] * 20 + [last_close]))
    indicator = result["indicators"]["sma_10_20"]

    # Previous averages are equal; the documented <= / >= rule must trigger.
    assert indicator["previous_sma_10"] == indicator["previous_sma_20"] == 100.0
    assert indicator["event"] == event
    assert indicator["direction"] == direction

    flat = analyze_daily_signals(_bars([100.0] * 21))
    assert flat["indicators"]["sma_10_20"]["event"] is None


@pytest.mark.parametrize(
    ("last_close", "event", "direction"),
    [
        (110.0, "golden_cross", "long"),
        (90.0, "death_cross", "short"),
    ],
)
def test_macd_uses_adjust_false_dif_dea_crosses(
    last_close: float, event: str, direction: str
) -> None:
    bars = _flat_then_breakout(last_close)
    result = analyze_daily_signals(bars)
    indicator = result["indicators"]["macd_12_26_9"]

    close = bars["close"]
    expected_dif = close.ewm(span=12, adjust=False).mean() - close.ewm(
        span=26, adjust=False
    ).mean()
    expected_dea = expected_dif.ewm(span=9, adjust=False).mean()
    assert indicator["dif"] == pytest.approx(expected_dif.iloc[-1])
    assert indicator["dea"] == pytest.approx(expected_dea.iloc[-1])
    assert indicator["macd"] == pytest.approx(
        2.0 * (expected_dif.iloc[-1] - expected_dea.iloc[-1])
    )
    assert indicator["event"] == event
    assert indicator["direction"] == direction


def test_incomplete_latest_bar_is_excluded_and_preheat_is_not_a_failure() -> None:
    bars = _flat_then_breakout(111.0)
    bars.loc[bars.index[-1], "is_complete"] = False
    result = analyze_daily_signals(bars)

    assert result["latest_bar"]["timestamp"] == "2024-02-24T00:00:00+00:00"
    assert result["status"]["completed_bars"] == 55
    assert result["status"]["state"] == "warming_up"
    assert result["signals"] == []
    assert result["indicators"]["turtle_55"]["status"] == "unavailable"


def test_resonance_reports_bullish_bearish_none_and_conflict() -> None:
    bullish = analyze_daily_signals(_flat_then_breakout(111.0))
    bearish = analyze_daily_signals(_flat_then_breakout(89.0))
    none = analyze_daily_signals(_bars([100.0] * 60))
    # The latest close breaks the 20-day high and turns MACD positive while
    # SMA10 remains below SMA20, so the component directions disagree.
    conflict = analyze_daily_signals(_bars([float(value) for value in range(160, 100, -1)] + [122.0]))

    assert bullish["resonance"]["status"] == "bullish"
    assert bullish["resonance"]["direction"] == "long"
    assert bearish["resonance"]["status"] == "bearish"
    assert bearish["resonance"]["direction"] == "short"
    assert none["resonance"]["status"] == "none"
    assert conflict["resonance"]["status"] == "conflict"


def test_prepared_indicators_are_causal_and_output_is_json_safe() -> None:
    prefix = _bars([100.0] * 55 + [110.0])
    future = _bars([200.0, 50.0])
    future["timestamp"] = pd.date_range("2024-02-26", periods=2, freq="D", tz="UTC")
    extended = pd.concat([prefix, future], ignore_index=True)

    prefix_prepared = prepare_daily_signal_frame(prefix)
    extended_prepared = prepare_daily_signal_frame(extended).iloc[: len(prefix)]
    pd.testing.assert_frame_equal(prefix_prepared, extended_prepared)

    payload = analyze_daily_signals(prefix)
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload
