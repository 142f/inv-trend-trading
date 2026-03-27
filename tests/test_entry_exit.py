from __future__ import annotations

import pandas as pd

from signals.entry_rules import evaluate_entry
from signals.exit_rules import reverse_cross_30m
from signals.resonance import DirectionGate


def _frame_from_rows(rows):
    if not rows:
        return pd.DataFrame(
            columns=[
                "open",
                "high",
                "low",
                "close",
                "ema144",
                "ema169",
                "base_low",
                "base_high",
                "close_ts",
            ]
        )
    df = pd.DataFrame(rows)
    df["open_ts"] = pd.to_datetime(df["open_ts"], utc=True)
    df = df.set_index("open_ts")
    df["close_ts"] = pd.to_datetime(df["close_ts"], utc=True)
    return df


def test_long_entry_triggered():
    fr15 = _frame_from_rows(
        [
            # previous 15m bar is signal candle (close_ts 00:30)
            {
                "open_ts": "2024-01-01 00:00:00+00:00",
                "close_ts": "2024-01-01 00:15:00+00:00",
                "open": 99,
                "high": 100,
                "low": 98.8,
                "close": 99.2,
                "ema144": 99.5,
                "ema169": 99.0,
                "base_low": 99.0,
                "base_high": 99.5,
            },
            {
                "open_ts": "2024-01-01 00:15:00+00:00",
                "close_ts": "2024-01-01 00:30:00+00:00",
                "open": 99.2,
                "high": 100.0,
                "low": 99.4,
                "close": 99.8,
                "ema144": 99.6,
                "ema169": 99.1,
                "base_low": 99.1,
                "base_high": 99.6,
            },
            # current bar (close_ts 00:45) should break trigger high
            {
                "open_ts": "2024-01-01 00:30:00+00:00",
                "close_ts": "2024-01-01 00:45:00+00:00",
                "open": 99.8,
                "high": 100.2,
                "low": 99.7,
                "close": 100.1,
                "ema144": 99.7,
                "ema169": 99.2,
                "base_low": 99.2,
                "base_high": 99.7,
            },
        ]
    )
    fr30 = _frame_from_rows([])
    gate = DirectionGate(r_score=0.7, allow_long=True, allow_short=False)
    states = {"4h": 1, "2h": 1, "1h": 0}
    dec = evaluate_entry(
        side="long",
        gate=gate,
        trend_states=states,
        frames={"15m": fr15, "30m": fr30},
        decision_close_ts=pd.Timestamp("2024-01-01 00:45:00+00:00"),
        breakout_filter=0.0005,
    )
    assert dec.can_enter is True


def test_reverse_cross():
    row = pd.Series({"close": 99.0, "base_low": 100.0, "base_high": 101.0})
    assert reverse_cross_30m("long", row) is True
