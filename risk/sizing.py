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
    """
    计算初始抗脆弱硬止损位。
    引入历史长期 ATR 中位数作为下界保护，并前置拦截拓扑倒挂异常。
    """
    current_atr = float(bar_15m.get("atr14", 0.0))
    close_px = float(bar_15m["close"])
    ema169 = float(bar_15m["ema169"])
    
    # 提取过去 288 根 K 线（3天）的 ATR 中位数作为保底锚点
    effective_atr = current_atr
    if len(frame_15m) >= 288:
        historical_atrs = pd.to_numeric(frame_15m["atr14"].tail(288), errors="coerce").dropna()
        if not historical_atrs.empty:
            atr_median = float(historical_atrs.median())
            effective_atr = max(current_atr, atr_median) # 阻断局部极低波动率导致的止损阈值崩溃
            
    # 计算绝对价格滑点成本
    slip_cost = close_px * (slippage_buffer_bps / 10000.0)

    if side == "long":
        # 理论止损位：EMA169基线 向下延伸 stop_atr_mult 倍有效ATR，并扣除滑点
        theoretical_stop = ema169 - (stop_atr_mult * effective_atr) - slip_cost
        
        # 【二次拓扑约束校验】：极端暴涨单边市中，EMA169可能严重滞后，导致止损价高于现价。
        # 强制兜底：止损价绝对不能高于 (现价 - stop_atr_mult*ATR)
        safe_stop = min(theoretical_stop, close_px - (stop_atr_mult * effective_atr) - slip_cost)
        
    else:
        # 空头逻辑镜像
        theoretical_stop = ema169 + (stop_atr_mult * effective_atr) + slip_cost
        # 【二次拓扑约束校验】：空头止损价绝对不能低于 (现价 + stop_atr_mult*ATR)
        safe_stop = max(theoretical_stop, close_px + (stop_atr_mult * effective_atr) + slip_cost)

    # 阻断负价格导致的系统底层崩溃
    return float(max(safe_stop, 1e-8))


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
