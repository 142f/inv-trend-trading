from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from indicators.trend_state import TrendState


@dataclass
class DirectionGate:
    r_score: float
    allow_long: bool
    allow_short: bool


def compute_r_score(states: Dict[str, int]) -> float:
    # 降维共振权重计算：将宏观周期的“一票否决权”剥离，聚焦于1d至1h的中短期趋势爆发。
    # 权重中枢下放，强调近期趋势的动量
    weights = {"1d": 30, "4h": 25, "2h": 15, "1h": 10, "30m": 3, "15m": 1}
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
    r = compute_r_score(states)
    
    # 废弃宏观否决权，只依赖 R-score
    # 当共振得分绝对值 >= 0.60 时，激活单向门控 (这里可以用传入的 long_gate/short_gate 做兼容，默认为 0.60/-0.60)
    allow_long = r >= 0.60
    allow_short = r <= -0.60
    
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
