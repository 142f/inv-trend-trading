from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config.models import Settings


@dataclass
class SizePlan:
    grade: str
    risk_pct: float
    leverage_cap: float
    risk_amount: float
    position_size: float
    notional: float
    applied_leverage: float
    entry_price: float
    stop_price: float
    stop_distance: float


def tier_params(settings: Settings, grade: str) -> tuple[float, float]:
    if grade == "A":
        return settings.risk.risk_tier_a, settings.risk.leverage_tier_a
    if grade == "B":
        return settings.risk.risk_tier_b, settings.risk.leverage_tier_b
    return settings.risk.risk_tier_c, settings.risk.leverage_tier_c


def compute_initial_stop(
    side: str,
    bar_15m: pd.Series,
    frame_15m: pd.DataFrame,
    stop_atr_mult: float,
    slippage_buffer_bps: float,
) -> float:
    last10 = frame_15m.tail(10)
    atr = float(bar_15m["atr14"])
    slip = float(bar_15m["close"]) * (slippage_buffer_bps / 10000.0)

    if side == "long":
        stop = float(bar_15m["ema169"] - stop_atr_mult * atr) - slip
    else:
        stop = float(bar_15m["ema169"] + stop_atr_mult * atr) + slip
    return float(stop)


def compute_effective_stop_distance(
    side: str,
    entry_price: float,
    stop_price: float,
    atr: float,
    min_stop_atr_mult: float,
) -> float:
    raw = abs(entry_price - stop_price)
    min_dist = min_stop_atr_mult * atr
    return max(raw, min_dist)


def build_size_plan(
    settings: Settings,
    side: str,
    grade: str,
    equity: float,
    entry_price: float,
    stop_price: float,
    atr: float,
    risk_pct_override: float | None = None,
) -> SizePlan:
    risk_pct, lev_cap = tier_params(settings, grade)
    if risk_pct_override is not None:
        risk_pct = risk_pct_override
    risk_amount = equity * risk_pct
    stop_distance = compute_effective_stop_distance(
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        atr=atr,
        min_stop_atr_mult=settings.strategy.min_stop_atr_mult,
    )
    if stop_distance <= 0:
        raise ValueError("stop_distance must be > 0")
    qty = risk_amount / stop_distance
    notional = qty * entry_price
    applied_lev = notional / equity if equity > 0 else float("inf")
    lev_hard = settings.risk.max_leverage_hard
    lev_limit = min(lev_cap, lev_hard)
    if applied_lev > lev_limit:
        scale = lev_limit / applied_lev
        qty *= scale
        notional = qty * entry_price
        applied_lev = lev_limit
        # keep risk_amount as planned limit; actual risk tracked from qty/stop later
    return SizePlan(
        grade=grade,
        risk_pct=risk_pct,
        leverage_cap=lev_limit,
        risk_amount=risk_amount,
        position_size=qty,
        notional=notional,
        applied_leverage=applied_lev,
        entry_price=entry_price,
        stop_price=stop_price,
        stop_distance=stop_distance,
    )
