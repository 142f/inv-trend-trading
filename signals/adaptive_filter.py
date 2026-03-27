from __future__ import annotations

import pandas as pd


def check_adaptive_market_filter(
    fr_15m: pd.DataFrame,
    decision_close_ts: pd.Timestamp,
    lookback: int,
    atr_q_low: float,
    atr_q_high: float,
    score_q: float,
) -> tuple[bool, str]:
    """Adaptive non-linear filter using ATR regime and volume/efficiency score.

    The filter only uses bars with close_ts <= decision_close_ts to avoid look-ahead.
    """
    if fr_15m.empty:
        return False, "adaptive_missing_15m"
    if "close_ts" not in fr_15m.columns:
        return False, "adaptive_missing_close_ts"

    # Backward-compatible bypass for tests or datasets that do not include these columns.
    required_cols = {"atr14", "volume", "close"}
    if not required_cols.issubset(set(fr_15m.columns)):
        return True, "adaptive_missing_columns_bypass"

    hist = fr_15m[fr_15m["close_ts"] <= decision_close_ts].tail(max(lookback, 64)).copy()
    if len(hist) < 64:
        return True, "adaptive_warmup_bypass"

    close = pd.to_numeric(hist["close"], errors="coerce")
    atr14 = pd.to_numeric(hist["atr14"], errors="coerce")
    volume = pd.to_numeric(hist["volume"], errors="coerce")

    atr_pct = (atr14 / close.replace(0, pd.NA)).dropna()
    if atr_pct.empty:
        return True, "adaptive_atr_nan_bypass"

    curr_atr = float(atr_pct.iloc[-1])
    low_thr = float(atr_pct.quantile(atr_q_low))
    high_thr = float(atr_pct.quantile(atr_q_high))
    if curr_atr < low_thr or curr_atr > high_thr:
        return False, "adaptive_atr_regime_block"

    # Non-linear score: volume percentile + trend efficiency ratio.
    vol_rank = volume.rank(pct=True)
    net_move = close.diff(12).abs()
    path_move = close.diff().abs().rolling(12, min_periods=12).sum()
    eff_ratio = (net_move / path_move.replace(0, pd.NA)).clip(lower=0.0, upper=1.0).fillna(0.0)

    score_series = 0.5 * vol_rank.fillna(0.0) + 0.5 * eff_ratio
    dyn_threshold = float(score_series.quantile(score_q))
    curr_score = float(score_series.iloc[-1])
    if curr_score < dyn_threshold:
        return False, "adaptive_score_block"

    return True, "adaptive_pass"
