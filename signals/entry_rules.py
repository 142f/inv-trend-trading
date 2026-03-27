from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from signals.adaptive_filter import check_adaptive_market_filter
from signals.pullback_reclaim import PullbackSignal, detect_reclaim_at_close
from signals.resonance import DirectionGate, count_mid_tf_confirm


@dataclass
class EntryDecision:
    can_enter: bool
    side: str
    reason: str
    trigger_price: float = 0.0
    signal_timeframe: str | None = None
    signal_close_ts: pd.Timestamp | None = None


def _find_15m_row_by_close(fr_15m: pd.DataFrame, close_ts: pd.Timestamp) -> Optional[pd.Series]:
    hit = fr_15m[fr_15m["close_ts"] == close_ts]
    if hit.empty:
        return None
    return hit.iloc[-1]


def _choose_signal(
    fr_15m: pd.DataFrame,
    fr_30m: pd.DataFrame,
    signal_close_ts: pd.Timestamp,
    side: str,
) -> PullbackSignal | None:
    sig30 = detect_reclaim_at_close(fr_30m, signal_close_ts, side=side, timeframe="30m")
    if sig30.valid:
        return sig30
    sig15 = detect_reclaim_at_close(fr_15m, signal_close_ts, side=side, timeframe="15m")
    if sig15.valid:
        return sig15
    return None


def evaluate_entry(
    side: str,
    gate: DirectionGate,
    trend_states: Dict[str, int],
    frames: Dict[str, pd.DataFrame],
    decision_close_ts: pd.Timestamp,
    breakout_filter: float,
    min_mid_tf_confirm: int = 2,
    short_min_mid_tf_confirm: int = 2,
    require_daily_align_for_short: bool = False,
    adaptive_filter_enabled: bool = True,
    adaptive_filter_lookback_15m: int = 192,
    adaptive_atr_q_low: float = 0.20,
    adaptive_atr_q_high: float = 0.90,
    adaptive_score_q: float = 0.60,
) -> EntryDecision:
    fr_15m = frames["15m"]
    fr_30m = frames["30m"]
    curr_15m = _find_15m_row_by_close(fr_15m, decision_close_ts)
    if curr_15m is None:
        return EntryDecision(False, side, "missing_15m_current_bar")

    side_allowed = (side == "long" and gate.allow_long) or (side == "short" and gate.allow_short)
    if not side_allowed:
        return EntryDecision(False, side, "direction_gate_blocked")

    if adaptive_filter_enabled:
        passed, reason = check_adaptive_market_filter(
            fr_15m=fr_15m,
            decision_close_ts=decision_close_ts,
            lookback=adaptive_filter_lookback_15m,
            atr_q_low=adaptive_atr_q_low,
            atr_q_high=adaptive_atr_q_high,
            score_q=adaptive_score_q,
        )
        if not passed:
            return EntryDecision(False, side, reason)

    confirms = count_mid_tf_confirm(trend_states, side=side)
    need_confirms = short_min_mid_tf_confirm if side == "short" else min_mid_tf_confirm
    if confirms < need_confirms:
        return EntryDecision(False, side, "insufficient_1h_2h_4h_confirm")

    if require_daily_align_for_short and side == "short":
        if trend_states.get("1d", 0) != -1 or trend_states.get("2d", 0) != -1:
            return EntryDecision(False, side, "short_daily_alignment_required")

    signal_ts = decision_close_ts - pd.Timedelta(minutes=15)
    sig = _choose_signal(fr_15m, fr_30m, signal_ts, side=side)
    if sig is None:
        return EntryDecision(False, side, "no_valid_pullback_reclaim_signal")

    trigger = sig.trigger_price * (1 + breakout_filter if side == "long" else 1 - breakout_filter)
    trigger_hit = curr_15m["high"] >= trigger if side == "long" else curr_15m["low"] <= trigger
    if not trigger_hit:
        return EntryDecision(False, side, "trigger_not_hit_in_next_15m", trigger_price=trigger, signal_timeframe=sig.timeframe, signal_close_ts=sig.signal_close_ts)

    return EntryDecision(
        can_enter=True,
        side=side,
        reason="ok",
        trigger_price=float(trigger),
        signal_timeframe=sig.timeframe,
        signal_close_ts=sig.signal_close_ts,
    )
