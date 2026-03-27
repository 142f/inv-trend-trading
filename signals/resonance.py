from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from indicators.trend_state import TrendState


@dataclass
class DirectionGate:
    r_score: float
    allow_long: bool
    allow_short: bool


def compute_r_score(states: Dict[str, int], weights: Dict[str, int]) -> float:
    numer = 0.0
    denom = 0.0
    for tf, w in weights.items():
        s = float(states.get(tf, 0))
        numer += w * s
        denom += w
    if denom == 0:
        return 0.0
    return numer / denom


def direction_gate(states: Dict[str, int], weights: Dict[str, int], long_gate: float, short_gate: float) -> DirectionGate:
    r = compute_r_score(states, weights)
    state_7d = states.get("7d", 0)
    state_5d = states.get("5d", 0)
    allow_long = r >= long_gate and state_7d != TrendState.BEARISH and state_5d != TrendState.BEARISH
    allow_short = r <= short_gate and state_7d != TrendState.BULLISH and state_5d != TrendState.BULLISH
    return DirectionGate(r_score=r, allow_long=allow_long, allow_short=allow_short)


def count_mid_tf_confirm(states: Dict[str, int], side: str) -> int:
    want = 1 if side == "long" else -1
    return sum(1 for tf in ["4h", "2h", "1h"] if states.get(tf, 0) == want)


def classify_entry_grade(r_score: float, mid_confirm_count: int, side: str) -> str | None:
    sign = 1 if side == "long" else -1
    adj_r = r_score * sign
    if adj_r >= 0.75 and mid_confirm_count >= 3:
        return "A"
    if 0.65 <= adj_r < 0.75 and mid_confirm_count >= 2:
        return "B"
    if 0.55 <= adj_r < 0.65 and mid_confirm_count >= 2:
        return "C"
    return None
