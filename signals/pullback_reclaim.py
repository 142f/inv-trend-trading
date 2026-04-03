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


def _clip01(v: float) -> float:
    return max(0.0, min(1.0, v))

def _is_true_liquidity_sweep(hist: pd.DataFrame, side: str) -> bool:
    """
    微观流动性猎杀判定引擎 (Liquidity Sweep)
    """
    curr = hist.iloc[-1]
    prev = hist.iloc[-2]

    ema144 = float(curr["ema144"])
    atr14 = float(curr.get("atr14", 0.0))
    if atr14 <= 0:
        return False

    # 【边界异常拦截】：过滤毫无波动的极窄死水 K 线
    candle_range = curr["high"] - curr["low"]
    if candle_range < (0.15 * atr14):
        return False

    if side == "long":
        # 形态 1：单K线深V探底 (Pin Bar)
        single_candle_sweep = (curr["low"] < ema144) and (curr["close"] > ema144)
        # 形态 2：双K线诱空反包 (Bullish Engulfing)
        two_candle_sweep = (prev["close"] <= ema144) and (curr["close"] > ema144)

        if not (single_candle_sweep or two_candle_sweep):
            return False

        # 深度要求：必须拿到均线下方至少 0.2 ATR 的爆仓流动性
        local_min = min(curr["low"], prev["low"])
        if (ema144 - local_min) < (0.2 * atr14):
            return False

        # 意图要求：收盘价必须强硬，绝不允许长上影线骗炮
        close_pct = (curr["close"] - curr["low"]) / candle_range
        if close_pct < 0.55:
            return False

        return True

    # 空头逻辑镜像
    single_candle_sweep = (curr["high"] > ema144) and (curr["close"] < ema144)
    two_candle_sweep = (prev["close"] >= ema144) and (curr["close"] < ema144)

    if not (single_candle_sweep or two_candle_sweep):
        return False

    local_max = max(curr["high"], prev["high"])
    if (local_max - ema144) < (0.2 * atr14):
        return False

    close_pct = (curr["high"] - curr["close"]) / candle_range
    if close_pct < 0.55:
        return False

    return True


def _get_dynamic_macd_weight(atr_ratio: float) -> float:
    """
    波动率自适应 MACD 权重：
    高波动时降低 MACD 约束，低波动时提高 MACD 约束。
    """
    if atr_ratio > 1.5:
        return 0.30
    if atr_ratio < 0.7:
        return 0.80
    return 0.50


def _get_dynamic_score_threshold(atr_ratio: float) -> float:
    """
    波动率自适应总分阈值：
    高波动放宽，低波动收紧。
    """
    if atr_ratio > 1.5:
        return 48.0
    if atr_ratio < 0.7:
        return 62.0
    return 55.0


def _timeframe_weight(timeframe: str) -> float:
    tf = (timeframe or "").lower()
    if tf == "15m":
        return 0.55
    if tf == "30m":
        return 0.65
    if tf == "1h":
        return 0.75
    if tf == "2h":
        return 0.85
    if tf == "4h":
        return 1.00
    return 0.70


def _calc_sweep_strength(hist: pd.DataFrame, side: str) -> float:
    """
    量化猎杀强度：综合下探/上刺深度与收盘控盘位置。
    输出范围 [0, 1]。
    """
    curr = hist.iloc[-1]
    prev = hist.iloc[-2]
    atr14 = float(curr.get("atr14", 0.0))
    if atr14 <= 0:
        return 0.0

    candle_range = float(curr["high"] - curr["low"])
    if candle_range <= 0:
        return 0.0

    ema144 = float(curr["ema144"])
    if side == "long":
        local_extreme = min(float(curr["low"]), float(prev["low"]))
        depth = max(0.0, (ema144 - local_extreme) / atr14)
        depth_score = _clip01(depth / 0.8)
        close_pct = (float(curr["close"]) - float(curr["low"])) / candle_range
        close_score = _clip01((close_pct - 0.50) / 0.50)
    else:
        local_extreme = max(float(curr["high"]), float(prev["high"]))
        depth = max(0.0, (local_extreme - ema144) / atr14)
        depth_score = _clip01(depth / 0.8)
        close_pct = (float(curr["high"]) - float(curr["close"])) / candle_range
        close_score = _clip01((close_pct - 0.50) / 0.50)

    return 0.6 * depth_score + 0.4 * close_score


def _calc_macd_momentum_score(hist_df: pd.DataFrame, side: str) -> float:
    """
    MACD 动量评分（非硬门控）：
    同时考虑方向一致性与柱体强度。
    输出范围 [0, 1]。
    """
    close_px = hist_df["close"]
    ema12 = close_px.ewm(span=12, adjust=False).mean()
    ema26 = close_px.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    curr_diff = float(macd_line.iloc[-1] - signal_line.iloc[-1])
    prev_diff = float(macd_line.iloc[-2] - signal_line.iloc[-2])

    scale = float((close_px.pct_change().rolling(30).std().iloc[-1] or 0.0))
    scale = max(scale, 1e-4)
    norm_strength = _clip01(abs(curr_diff) / (3.0 * scale * float(close_px.iloc[-1])))

    if side == "long":
        dir_score = 1.0 if curr_diff > 0 else 0.0
        cross_bonus = 1.0 if (prev_diff <= 0 < curr_diff) else 0.0
    else:
        dir_score = 1.0 if curr_diff < 0 else 0.0
        cross_bonus = 1.0 if (prev_diff >= 0 > curr_diff) else 0.0

    return 0.55 * dir_score + 0.30 * norm_strength + 0.15 * cross_bonus


def _calc_volume_confirmation(hist_df: pd.DataFrame) -> float:
    """
    成交量确认评分：若无 volume 字段则给中性分。
    输出范围 [0, 1]。
    """
    if "volume" not in hist_df.columns:
        return 0.5

    vol = hist_df["volume"].astype(float)
    if len(vol) < 21:
        return 0.5

    baseline = float(vol.iloc[-21:-1].mean())
    if baseline <= 0:
        return 0.5

    ratio = float(vol.iloc[-1] / baseline)
    return _clip01((ratio - 0.7) / 1.0)


def detect_reclaim_at_close(df: pd.DataFrame, close_ts: pd.Timestamp, side: str, timeframe: str) -> PullbackSignal:
    """
    驱动信号检测的主入口。
    结合了微观 K 线形态与 MACD 宏观动能的交叉验证体系。
    """
    if df.empty or "close_ts" not in df.columns:
        return PullbackSignal(False, side, timeframe, 0.0, None)

    # 提取最后 100 根 K 线，确保 MACD 的 EMA 计算具备充足的预热周期 (Warm-up Period)
    hist_df = df[df["close_ts"] <= close_ts].tail(100).copy()
    if len(hist_df) < 35:
        return PullbackSignal(False, side, timeframe, 0.0, None)

    # 1. 验证微观流动性猎杀形态
    is_sweep = _is_true_liquidity_sweep(hist_df, side)
    if not is_sweep:
        return PullbackSignal(False, side, timeframe, 0.0, None)

    # 2. 构建软评分体系：猎杀强度 + MACD动量 + 成交量确认 + 时间框架权重
    curr = hist_df.iloc[-1]
    atr14 = float(curr.get("atr14", 0.0))
    atr_base = float(hist_df["atr14"].tail(48).mean()) if "atr14" in hist_df.columns else 0.0
    atr_ratio = (atr14 / atr_base) if atr_base > 0 else 1.0

    sweep_score = _calc_sweep_strength(hist_df, side)
    macd_score = _calc_macd_momentum_score(hist_df, side)
    volume_score = _calc_volume_confirmation(hist_df)
    tf_score = _timeframe_weight(timeframe)

    macd_weight = _get_dynamic_macd_weight(atr_ratio)
    # 权重归一化：保持总和为 1.0
    w_sweep = 0.40
    w_macd = 0.20 + 0.25 * macd_weight
    w_volume = 0.20
    w_tf = max(0.0, 1.0 - w_sweep - w_macd - w_volume)

    total_score = 100.0 * (
        w_sweep * sweep_score
        + w_macd * macd_score
        + w_volume * volume_score
        + w_tf * tf_score
    )

    threshold = _get_dynamic_score_threshold(atr_ratio)
    if total_score < threshold:
        return PullbackSignal(False, side, timeframe, 0.0, None)

    # 3. 执行级防线缓冲
    if side == "long":
        trigger = float(hist_df.iloc[-1]["high"]) + (0.05 * atr14)
    else:
        trigger = float(hist_df.iloc[-1]["low"]) - (0.05 * atr14)

    return PullbackSignal(True, side, timeframe, trigger, close_ts)
