from __future__ import annotations
from dataclasses import dataclass
import pandas as pd
import numpy as np

@dataclass
class SimpleSignal:
    valid: bool
    side: str
    confidence: float
    trigger_price: float
    stop_price: float
    signal_close_ts: pd.Timestamp | None


def _normalize_atr_ratio(atr_current: float, atr_history: pd.Series) -> float:
    """
    计算当前ATR相对于历史ATR的比率，用于波动率自适应
    """
    if len(atr_history) < 20 or atr_current <= 0:
        return 1.0
    
    atr_median = float(atr_history.tail(48).median())
    if atr_median <= 0:
        return 1.0
    
    ratio = atr_current / atr_median
    return max(0.5, min(2.0, ratio))


def _detect_pullback_pattern(hist: pd.DataFrame, side: str) -> tuple[bool, float]:
    """
    简化的回撤形态识别：寻找价格触及EMA后的反弹
    
    Returns:
        (is_valid, confidence_score)
    """
    if len(hist) < 3:
        return False, 0.0
    
    curr = hist.iloc[-1]
    prev = hist.iloc[-2]
    prev2 = hist.iloc[-3]
    
    ema = float(curr["ema144"])
    atr = float(curr.get("atr14", 0.0))
    
    if atr <= 0:
        return False, 0.0
    
    candle_range = curr["high"] - curr["low"]
    if candle_range < 0.1 * atr:
        return False, 0.0
    
    confidence = 0.0
    
    if side == "long":
        # 多头：价格触及EMA下方后反弹
        touched_ema = (curr["low"] <= ema) or (prev["low"] <= ema) or (prev2["low"] <= ema)
        recovering = curr["close"] > ema
        
        if not (touched_ema and recovering):
            return False, 0.0
        
        # 计算触及深度
        min_price = min(curr["low"], prev["low"], prev2["low"])
        depth = (ema - min_price) / atr
        depth_score = min(1.0, depth / 0.5)  # 0.5 ATR深度为满分
        
        # 计算反弹强度
        if candle_range > 0:
            close_position = (curr["close"] - curr["low"]) / candle_range
            recovery_score = min(1.0, close_position / 0.7)  # 70%收盘位置为满分
        else:
            recovery_score = 0.0
        
        # 连续性检查：最近3根K线是否都在回升
        consecutive_up = (curr["close"] > prev["close"]) and (prev["close"] > prev2["close"])
        momentum_score = 1.0 if consecutive_up else 0.5
        
        confidence = 0.4 * depth_score + 0.4 * recovery_score + 0.2 * momentum_score
        
    else:
        # 空头：价格触及EMA上方后回落
        touched_ema = (curr["high"] >= ema) or (prev["high"] >= ema) or (prev2["high"] >= ema)
        rejecting = curr["close"] < ema
        
        if not (touched_ema and rejecting):
            return False, 0.0
        
        # 计算触及深度
        max_price = max(curr["high"], prev["high"], prev2["high"])
        depth = (max_price - ema) / atr
        depth_score = min(1.0, depth / 0.5)
        
        # 计算回落强度
        if candle_range > 0:
            close_position = (curr["high"] - curr["close"]) / candle_range
            rejection_score = min(1.0, close_position / 0.7)
        else:
            rejection_score = 0.0
        
        # 连续性检查
        consecutive_down = (curr["close"] < prev["close"]) and (prev["close"] < prev2["close"])
        momentum_score = 1.0 if consecutive_down else 0.5
        
        confidence = 0.4 * depth_score + 0.4 * rejection_score + 0.2 * momentum_score
    
    return True, confidence


def _calculate_momentum_score(hist_df: pd.DataFrame, side: str) -> float:
    """
    简化的动量评分：基于价格变化率和趋势一致性
    """
    if len(hist_df) < 10:
        return 0.5
    
    closes = hist_df["close"].values
    
    # 短期动量（最近5根K线）
    short_momentum = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0
    
    # 中期动量（最近20根K线）
    medium_momentum = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 and closes[-20] > 0 else 0
    
    # 根据方向调整
    if side == "long":
        momentum_score = 0.6 * max(0, short_momentum * 100) + 0.4 * max(0, medium_momentum * 100)
    else:
        momentum_score = 0.6 * max(0, -short_momentum * 100) + 0.4 * max(0, -medium_momentum * 100)
    
    return min(1.0, momentum_score)


def _calculate_volume_score(hist_df: pd.DataFrame) -> float:
    """
    简化的成交量评分
    """
    if "volume" not in hist_df.columns or len(hist_df) < 20:
        return 0.5
    
    volumes = hist_df["volume"].values
    current_vol = volumes[-1]
    avg_vol = np.mean(volumes[-20:-1])
    
    if avg_vol <= 0:
        return 0.5
    
    vol_ratio = current_vol / avg_vol
    
    # 成交量放大是好事，但过度放大可能是陷阱
    if vol_ratio >= 2.0:
        return 0.7  # 过度放大，适度降分
    elif vol_ratio >= 1.5:
        return 1.0  # 理想放大
    elif vol_ratio >= 1.0:
        return 0.8  # 正常
    else:
        return 0.4  # 成交量不足


def detect_simple_entry(
    df: pd.DataFrame, 
    close_ts: pd.Timestamp, 
    side: str, 
    min_confidence: float = 0.6
) -> SimpleSignal:
    """
    简化的入场信号检测
    
    核心逻辑：
    1. 识别价格触及EMA后的反弹/回落形态
    2. 评估形态质量（深度、强度、连续性）
    3. 结合动量和成交量确认
    4. 根据波动率动态调整置信度要求
    """
    if df.empty or "close_ts" not in df.columns:
        return SimpleSignal(False, side, 0.0, 0.0, 0.0, None)
    
    # 提取历史数据
    hist_df = df[df["close_ts"] <= close_ts].tail(50).copy()
    if len(hist_df) < 20:
        return SimpleSignal(False, side, 0.0, 0.0, 0.0, None)
    
    # 1. 检测回撤形态
    has_pattern, pattern_confidence = _detect_pullback_pattern(hist_df, side)
    if not has_pattern:
        return SimpleSignal(False, side, 0.0, 0.0, 0.0, None)
    
    # 2. 计算动量评分
    momentum_score = _calculate_momentum_score(hist_df, side)
    
    # 3. 计算成交量评分
    volume_score = _calculate_volume_score(hist_df)
    
    # 4. 波动率自适应
    atr_current = float(hist_df.iloc[-1].get("atr14", 0.0))
    atr_history = hist_df["atr14"] if "atr14" in hist_df.columns else pd.Series([atr_current] * len(hist_df))
    atr_ratio = _normalize_atr_ratio(atr_current, atr_history)
    
    # 高波动时降低置信度要求，低波动时提高要求
    adaptive_threshold = min_confidence * (0.8 if atr_ratio > 1.3 else 1.2 if atr_ratio < 0.7 else 1.0)
    
    # 5. 综合评分
    total_confidence = (
        0.5 * pattern_confidence +  # 形态质量最重要
        0.3 * momentum_score +     # 动量确认
        0.2 * volume_score         # 成交量确认
    )
    
    # 6. 判断是否满足入场条件
    if total_confidence < adaptive_threshold:
        return SimpleSignal(False, side, 0.0, 0.0, 0.0, None)
    
    # 7. 计算触发价格和止损价格
    curr = hist_df.iloc[-1]
    atr = float(curr.get("atr14", 0.0))
    
    if side == "long":
        # 触发价格：当前K线高点 + 小幅缓冲
        trigger_price = float(curr["high"]) + (0.1 * atr)
        # 止损价格：形态最低点 - 安全缓冲
        min_price = min(hist_df.tail(3)["low"])
        stop_price = min_price - (0.2 * atr)
    else:
        # 触发价格：当前K线低点 - 小幅缓冲
        trigger_price = float(curr["low"]) - (0.1 * atr)
        # 止损价格：形态最高点 + 安全缓冲
        max_price = max(hist_df.tail(3)["high"])
        stop_price = max_price + (0.2 * atr)
    
    return SimpleSignal(
        valid=True,
        side=side,
        confidence=total_confidence,
        trigger_price=trigger_price,
        stop_price=stop_price,
        signal_close_ts=close_ts
    )