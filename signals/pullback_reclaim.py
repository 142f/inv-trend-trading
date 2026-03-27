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

def _reclaim_condition(row: pd.Series, prev_row: pd.Series, side: str) -> bool:
    """
    重构后的宽容型形态识别：
    1. 放弃单根K线深V的苛刻要求，识别 EMA144 附近的“浅层刺透”与“有效收复”。
    2. 引入 prev_row 进行交叉验证，确认这是一个【穿越】动作，而不是一直在均线上方/下方悬浮。
    """
    atr14 = float(row.get("atr14", 0.0))
    if atr14 == 0.0:
        return False

    if side == "long":
        # 宽容的刺破条件：只要当前 K 线的最低点，或者上一根的最低点，曾触及基准线(base_high)下方即可
        touched_baseline = row["low"] <= row["base_high"] or (prev_row is not None and prev_row["low"] <= prev_row["base_high"])
        
        # 深度验证大幅降维：仅需 0.15 ATR 的微波段确认，过滤随机跳动，捕捉主力浅洗盘
        wash_depth = row["ema144"] - min(row["low"], prev_row["low"] if prev_row is not None else row["low"])
        depth_ok = wash_depth >= (0.15 * atr14)
        
        # 核心收复逻辑：上一根在 EMA144 之下或附近徘徊，当前根以收盘价实体坚决站上 EMA144
        cross_up = row["close"] > row["ema144"] and (prev_row is not None and prev_row["close"] <= prev_row["ema144"] * 1.001)
        
        return touched_baseline and depth_ok and cross_up

    else: # Short side
        touched_baseline = row["high"] >= row["base_low"] or (prev_row is not None and prev_row["high"] >= prev_row["base_low"])
        
        wash_depth = max(row["high"], prev_row["high"] if prev_row is not None else row["high"]) - row["ema144"]
        depth_ok = wash_depth >= (0.15 * atr14)
        
        cross_down = row["close"] < row["ema144"] and (prev_row is not None and prev_row["close"] >= prev_row["ema144"] * 0.999)
        
        return touched_baseline and depth_ok and cross_down

def detect_reclaim_at_close(df: pd.DataFrame, close_ts: pd.Timestamp, side: str, timeframe: str) -> PullbackSignal:
    if df.empty or "close_ts" not in df.columns:
        return PullbackSignal(False, side, timeframe, 0.0, None)
    
    rows = df[df["close_ts"] == close_ts]
    if rows.empty:
        return PullbackSignal(False, side, timeframe, 0.0, None)
    
    idx = rows.index[-1]
    loc = df.index.get_loc(idx)
    row = df.iloc[loc]
    prev_row = df.iloc[loc - 1] if loc > 0 else None
    
    # 启用带有前置 K 线记忆的收复判断
    valid = _reclaim_condition(row, prev_row, side)
    
    # 触发价设定为突破当前 K 线的高点/低点
    trigger = float(row["high"] if side == "long" else row["low"])
    return PullbackSignal(valid, side, timeframe, trigger, close_ts)
