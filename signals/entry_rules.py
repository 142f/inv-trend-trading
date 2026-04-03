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


def _macd_snapshot(df: pd.DataFrame, decision_close_ts: pd.Timestamp, lookback: int = 120) -> tuple[float, float, float, float] | None:
    hist = df[df["close_ts"] <= decision_close_ts].tail(lookback)
    if len(hist) < 35:
        return None
    close_px = hist["close"].astype(float)
    ema12 = close_px.ewm(span=12, adjust=False).mean()
    ema26 = close_px.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    histo = macd - signal
    if len(histo) < 4:
        return None
    hist_slope3 = float(histo.iloc[-1] - histo.iloc[-4])
    return float(macd.iloc[-1]), float(signal.iloc[-1]), float(histo.iloc[-1]), hist_slope3


def _btc_extreme_risk_proxy(
    side: str,
    btc_frames: Dict[str, pd.DataFrame] | None,
    decision_close_ts: pd.Timestamp,
) -> bool:
    """
    BTC 见顶/见底风险代理：仅在可用 BTC 数据时启用。
    返回 True 表示风险过高，应阻断当前 side 入场。
    """
    if not btc_frames:
        return False

    fr5 = btc_frames.get("5d")
    fr7 = btc_frames.get("7d")
    if fr5 is None or fr7 is None or fr5.empty or fr7.empty:
        return False

    b5 = fr5[fr5["close_ts"] <= decision_close_ts].tail(120)
    b7 = fr7[fr7["close_ts"] <= decision_close_ts].tail(120)
    if len(b5) < 35 or len(b7) < 35:
        return False

    macd5 = _macd_snapshot(fr5, decision_close_ts)
    macd7 = _macd_snapshot(fr7, decision_close_ts)
    if macd5 is None or macd7 is None:
        return False

    m5, s5, _, _ = macd5
    _, _, h7, hs7 = macd7
    close7 = float(b7.iloc[-1]["close"])
    ema144_7 = float(b7.iloc[-1]["ema144"]) if "ema144" in b7.columns else close7
    overheat = close7 > ema144_7 * 1.12

    # 顶部代理：高位过热 + 7d 动能衰减 + 5d 死叉
    top_risk = int(overheat) + int(h7 > 0 and hs7 < 0) + int(m5 < s5)
    # 底部代理：深度折价 + 7d 动能回升 + 5d 金叉
    deep_value = close7 < ema144_7 * 0.90
    bottom_risk = int(deep_value) + int(h7 < 0 and hs7 > 0) + int(m5 > s5)

    if side == "long":
        return top_risk >= 2
    return bottom_risk >= 2


def _macro_dominance_gate(
    side: str,
    trend_states: Dict[str, int],
    frames: Dict[str, pd.DataFrame],
    decision_close_ts: pd.Timestamp,
    btc_frames: Dict[str, pd.DataFrame] | None,
) -> tuple[bool, str]:
    """
    放宽的大周期门控 (V8.1)：
    大周期为主导趋势。5d/7d 允许“一强一中性”。
    BTC 仅作参考因子，不作硬性阻断。
    """
    fr5 = frames.get("5d")
    fr7 = frames.get("7d")
    if fr5 is None or fr7 is None or fr5.empty or fr7.empty:
        return False, "missing_5d_7d_frames"

    m5 = _macd_snapshot(fr5, decision_close_ts)
    m7 = _macd_snapshot(fr7, decision_close_ts)
    if m5 is None or m7 is None:
        return False, "insufficient_5d_7d_for_macd"

    macd5, sig5, hist5, slope5 = m5
    macd7, sig7, hist7, slope7 = m7

    st5 = int(trend_states.get("5d", 0))
    st7 = int(trend_states.get("7d", 0))

    if side == "long":
        # 放宽：7d 必须 >= 0，或者 5d 必须 > 0 即可
        state_ok = (st7 >= 0) or (st5 > 0)
        # MACD 放宽：只需 7d MACD >= Signal 或 5d MACD >= Signal 即可
        macd_ok = (macd5 >= sig5) or (macd7 >= sig7) or (hist7 > 0)
        if not state_ok:
            return False, "macro_5d_7d_state_not_bullish"
        if not macd_ok:
            return False, "macro_5d_7d_macd_not_bullish"
        
        # BTC 仅作参考，不再 hard block
        # if _btc_extreme_risk_proxy(side, btc_frames, decision_close_ts):
        #     return False, "btc_top_risk_block_long"
    else:
        state_ok = (st7 <= 0) or (st5 < 0)
        macd_ok = (macd5 <= sig5) or (macd7 <= sig7) or (hist7 < 0)
        if not state_ok:
            return False, "macro_5d_7d_state_not_bearish"
        if not macd_ok:
            return False, "macro_5d_7d_macd_not_bearish"
        
        # BTC 仅作参考
        # if _btc_extreme_risk_proxy(side, btc_frames, decision_close_ts):
        #     return False, "btc_bottom_risk_block_short"

    return True, "ok"


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
    # 强制以 30m 信号为主抓手，其次回退到 15m
    sig30 = detect_reclaim_at_close(fr_30m, signal_close_ts, side=side, timeframe="30m")
    if sig30.valid:
        return sig30
    
    # 放宽：如果 30m 没有信号，允许 15m 级别的信号
    sig15 = detect_reclaim_at_close(fr_15m, signal_close_ts, side=side, timeframe="15m")
    if sig15.valid:
        return sig15
        
    return None


def evaluate_entry(
    symbol: str,
    side: str,
    gate: DirectionGate,
    trend_states: Dict[str, int],
    frames: Dict[str, pd.DataFrame],
    btc_frames: Dict[str, pd.DataFrame] | None,
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

    macro_ok, macro_reason = _macro_dominance_gate(
        side=side,
        trend_states=trend_states,
        frames=frames,
        decision_close_ts=decision_close_ts,
        btc_frames=btc_frames,
    )
    if not macro_ok:
        return EntryDecision(False, side, f"{macro_reason}")

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
