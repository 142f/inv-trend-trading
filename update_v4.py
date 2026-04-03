from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

@dataclass
class PullbackSignal:
    valid: bool
    side: str
    timeframe: str
    trigger_price: float
    signal_close_ts: pd.Timestamp | None

def _reclaim_condition(row: pd.Series, prev_row: pd.Series | None, side: str) -> bool:
    """
    V4 工业级清洗与收复形态识别 (Noise-Resistant Pullback Reclaim)
    
    核心防御升级：
    1. 噪音底线 (Noise Floor)：刺穿深度提升至 0.3 ATR，过滤做市商的高频随机游走(HFT Random Walk)。
    2. 动量意图验证 (Momentum Thrust)：收复K线必须展现出明确的控盘意图，即收盘价必须位于该K线振幅的强势区域。
    """
    atr14 = float(row.get("atr14", 0.0))
    if atr14 <= 0.0:
        return False
        
    candle_range = row["high"] - row["low"]
    # 【边界异常拦截】：极其平缓的死水K线（波动小于 0.1 ATR），其形态不具备统计学置信度，直接丢弃
    if candle_range < (0.1 * atr14):
        return False

    if side == "long":
        # 1. 基准线防守：当前K线或前一根K线，必须探到底部均线带 (base_high) 以下
        touched_baseline = row["low"] <= row["base_high"] or (prev_row is not None and prev_row["low"] <= prev_row["base_high"])
        
        # 2. 洗盘深度约束：0.3 ATR 构筑噪音护城河（防微小波动）
        min_low = min(row["low"], prev_row["low"] if prev_row is not None else row["low"])
        wash_depth = row["ema144"] - min_low
        depth_ok = wash_depth >= (0.30 * atr14)
        
        # 3. 均线收复验证：上一根在水下/附近，当前根收盘价站上 EMA144
        cross_up = row["close"] > row["ema144"] and (prev_row is not None and prev_row["close"] <= prev_row["ema144"] * 1.001)
        
        # 4. 【动量意图核心】：拒绝长上影线的假突破，要求实体坚决，收盘价必须在 K 线上半部分 (Top 45%)
        close_pct = (row["close"] - row["low"]) / candle_range
        thrust_ok = close_pct >= 0.55 
        
        return touched_baseline and depth_ok and cross_up and thrust_ok

    else: # Short side
        touched_baseline = row["high"] >= row["base_low"] or (prev_row is not None and prev_row["high"] >= prev_row["base_low"])
        
        max_high = max(row["high"], prev_row["high"] if prev_row is not None else row["high"])
        wash_depth = max_high - row["ema144"]
        depth_ok = wash_depth >= (0.30 * atr14)
        
        cross_down = row["close"] < row["ema144"] and (prev_row is not None and prev_row["close"] >= prev_row["ema144"] * 0.999)
        
        # 拒绝长下影线，要求坚决砸盘，收盘价必须在 K 线下半部分
        close_pct = (row["high"] - row["close"]) / candle_range
        thrust_ok = close_pct >= 0.55 
        
        return touched_baseline and depth_ok and cross_down and thrust_ok


def detect_reclaim_at_close(df: pd.DataFrame, close_ts: pd.Timestamp, side: str, timeframe: str) -> PullbackSignal:
    """
    在指定的 K 线闭合时刻，检测是否存在合规的回踩收复信号。
    """
    if df.empty or "close_ts" not in df.columns:
        return PullbackSignal(False, side, timeframe, 0.0, None)
    
    rows = df[df["close_ts"] == close_ts]
    if rows.empty:
        return PullbackSignal(False, side, timeframe, 0.0, None)
    
    idx = rows.index[-1]
    loc = df.index.get_loc(idx)
    row = df.iloc[loc]
    prev_row = df.iloc[loc - 1] if loc > 0 else None
    
    valid = _reclaim_condition(row, prev_row, side)
    
    # 【触发防插针增强】：废弃固定百分比(bps)的突破过滤器。
    # 强制要求价格在越过信号 K 线极值后，再额外走出 0.1 ATR 的物理距离，才算真实突破。
    atr14 = float(row.get("atr14", 0.0))
    if side == "long":
        trigger = float(row["high"]) + (0.10 * atr14)
    else:
        trigger = float(row["low"]) - (0.10 * atr14)
        
    return PullbackSignal(valid, side, timeframe, trigger, close_ts)
