from __future__ import annotations

import numpy as np
import pandas as pd

from config.models import Settings

BARS_PER_YEAR_15M = 365 * 24 * 4


def _safe_tail_hist(fr_15m: pd.DataFrame, decision_close_ts: pd.Timestamp, lookback: int) -> pd.DataFrame:
    if fr_15m.empty or "close_ts" not in fr_15m.columns:
        return pd.DataFrame()
    return fr_15m[fr_15m["close_ts"] <= decision_close_ts].tail(max(lookback, 64)).copy()


def _annualized_vol_from_close(close: pd.Series) -> float:
    lr = np.log(close / close.shift(1)).dropna()
    if lr.empty:
        return 0.0
    return float(lr.std(ddof=0) * np.sqrt(BARS_PER_YEAR_15M))


def _vol_parity_scalar(settings: Settings, hist: pd.DataFrame) -> float:
    vol = _annualized_vol_from_close(pd.to_numeric(hist["close"], errors="coerce"))
    if vol <= 1e-12:
        return 1.0
    raw = settings.risk.vol_target_annual / vol
    return float(np.clip(raw, settings.risk.vol_parity_scalar_min, settings.risk.vol_parity_scalar_max))


def _half_kelly_scalar(settings: Settings, hist: pd.DataFrame, side: str) -> float:
    close = pd.to_numeric(hist["close"], errors="coerce")
    lr = np.log(close / close.shift(1)).dropna()
    if len(lr) < 32:
        return 0.7

    sign = 1.0 if side == "long" else -1.0
    edge = sign * float(lr.mean())
    var = float(lr.var(ddof=0))
    if var <= 1e-12:
        return 0.7

    # Continuous-return approximation of Kelly fraction; constrained to half Kelly.
    kelly_raw = edge / var
    kelly_half = max(0.0, 0.5 * kelly_raw)
    kelly_cap = max(settings.risk.kelly_fraction_cap, 1e-9)
    norm = float(np.clip(kelly_half / kelly_cap, 0.0, 1.0))
    # Keep a non-zero floor so eligible trades are not eliminated by noisy short windows.
    return 0.4 + 0.6 * norm


def _regime_scalar(settings: Settings, hist: pd.DataFrame) -> float:
    close = pd.to_numeric(hist["close"], errors="coerce")
    atr = pd.to_numeric(hist.get("atr14", pd.Series(index=hist.index, dtype=float)), errors="coerce")
    atr_pct = (atr / close.replace(0, np.nan)).dropna()
    if len(close) < 64 or atr_pct.empty:
        return settings.risk.regime_scale_neutral

    # Trend-efficiency ratio sequence (0..1), adaptive quantile-based regime classifier.
    win = min(48, max(16, len(close) // 4))
    net = close.diff(win).abs()
    path = close.diff().abs().rolling(win, min_periods=win).sum()
    er = (net / path.replace(0, np.nan)).clip(0.0, 1.0).dropna()
    if er.empty:
        return settings.risk.regime_scale_neutral

    vol_rank = float((atr_pct <= atr_pct.iloc[-1]).mean())
    er_rank = float((er <= er.iloc[-1]).mean())

    if vol_rank >= 0.85:
        return settings.risk.regime_scale_stress
    if er_rank >= 0.60 and vol_rank <= 0.75:
        return settings.risk.regime_scale_trend
    return settings.risk.regime_scale_neutral


def apply_dynamic_risk_pct(
    settings: Settings,
    base_risk_pct: float,
    side: str,
    fr_15m: pd.DataFrame,
    decision_close_ts: pd.Timestamp,
) -> float:
    if not settings.risk.dynamic_sizing_enabled:
        return float(base_risk_pct)

    lookback = max(
        settings.risk.vol_parity_lookback_15m,
        settings.risk.kelly_lookback_15m,
        settings.risk.regime_lookback_15m,
    )
    hist = _safe_tail_hist(fr_15m=fr_15m, decision_close_ts=decision_close_ts, lookback=lookback)
    if len(hist) < 64:
        return float(np.clip(base_risk_pct, settings.risk.risk_pct_min, settings.risk.risk_pct_max))

    hist_vol = hist.tail(settings.risk.vol_parity_lookback_15m)
    hist_kelly = hist.tail(settings.risk.kelly_lookback_15m)
    hist_regime = hist.tail(settings.risk.regime_lookback_15m)

    vol_scalar = _vol_parity_scalar(settings=settings, hist=hist_vol)
    kelly_scalar = _half_kelly_scalar(settings=settings, hist=hist_kelly, side=side)
    regime_scalar = _regime_scalar(settings=settings, hist=hist_regime)

    adjusted = base_risk_pct * vol_scalar * kelly_scalar * regime_scalar
    return float(np.clip(adjusted, settings.risk.risk_pct_min, settings.risk.risk_pct_max))
