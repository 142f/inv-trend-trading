from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from portfolio.state import PositionState


@dataclass
class SimpleExitDecision:
    should_exit: bool
    exit_price: float
    reason: str


def calculate_trailing_stop(
    pos: PositionState, 
    current_price: float, 
    atr: float,
    bars_held: int
) -> float:
    """
    简化的动态跟踪止损
    
    逻辑：
    - 早期（前24根K线）：紧贴跟踪，2倍ATR
    - 中期（24-96根K线）：适度放宽，3倍ATR  
    - 后期（96根K线以上）：宽幅跟踪，4倍ATR
    
    这样既能保护利润，又能给趋势足够的发展空间
    """
    if atr <= 0:
        atr = current_price * 0.01  # fallback
    
    # 根据持仓时间调整ATR倍数
    if bars_held < 24:
        atr_mult = 2.0  # 早期紧贴
    elif bars_held < 96:
        atr_mult = 3.0  # 中期适度
    else:
        atr_mult = 4.0  # 后期宽幅
    
    if pos.side == "long":
        # 多头：从最高点向下跟踪
        trail_stop = pos.highest_high - (atr * atr_mult)
        # 确保止损不会低于初始止损（保护已实现的利润）
        return max(trail_stop, pos.stop_price)
    else:
        # 空头：从最低点向上跟踪
        trail_stop = pos.lowest_low + (atr * atr_mult)
        # 确保止损不会高于初始止损
        return min(trail_stop, pos.stop_price)


def check_profit_target(
    pos: PositionState, 
    current_price: float, 
    initial_stop_distance: float,
    partial_exit_done: bool
) -> tuple[bool, Optional[float], str]:
    """
    简化的止盈逻辑
    
    逻辑：
    - 达到2R时，减仓50%锁定部分利润
    - 达到5R时，全部平仓
    """
    if initial_stop_distance <= 0:
        return False, None, ""
    
    # 计算当前R倍数
    if pos.side == "long":
        r_multiple = (current_price - pos.entry_price) / initial_stop_distance
    else:
        r_multiple = (pos.entry_price - current_price) / initial_stop_distance
    
    # 第一次止盈：2R时减仓50%
    if not partial_exit_done and r_multiple >= 2.0:
        exit_ratio = 0.5
        return True, exit_ratio, f"profit_target_2R_partial_exit"
    
    # 第二次止盈：5R时全部平仓
    if r_multiple >= 5.0:
        exit_ratio = 1.0
        return True, exit_ratio, f"profit_target_5R_full_exit"
    
    return False, None, ""


def check_trend_exhaustion(
    pos: PositionState,
    current_price: float,
    ema_long: float,
    bars_since_entry: int
) -> bool:
    """
    趋势衰竭检查
    
    逻辑：
    - 价格重新穿越长期EMA（144或169）
    - 且持仓时间超过一定周期（避免过早退出）
    """
    if bars_since_entry < 12:  # 至少持仓3小时（12根15分钟K线）
        return False
    
    if pos.side == "long":
        return current_price < ema_long
    else:
        return current_price > ema_long


def check_simple_exit(
    pos: PositionState,
    current_price: float,
    atr: float,
    ema_long: float,
    partial_exit_done: bool = False,
    max_hold_bars: int = 384  # 最大持仓4天（384根15分钟K线）
) -> SimpleExitDecision:
    """
    简化的出场决策
    
    优先级：
    1. 硬止损（保护本金）
    2. 动态跟踪止损（保护利润）
    3. 止盈目标（锁定利润）
    4. 趋势衰竭（趋势反转信号）
    5. 时间止损（避免长期套牢）
    """
    
    # 1. 硬止损检查
    if pos.side == "long" and current_price <= pos.stop_price:
        return SimpleExitDecision(
            should_exit=True,
            exit_price=pos.stop_price,
            reason="hard_stop_loss"
        )
    elif pos.side == "short" and current_price >= pos.stop_price:
        return SimpleExitDecision(
            should_exit=True,
            exit_price=pos.stop_price,
            reason="hard_stop_loss"
        )
    
    # 2. 动态跟踪止损
    new_trail_stop = calculate_trailing_stop(pos, current_price, atr, pos.bars_held)
    if new_trail_stop != pos.stop_price:
        # 止损价格更新，但不立即出场
        pass
    
    # 检查是否触发跟踪止损
    if pos.side == "long" and current_price <= new_trail_stop:
        return SimpleExitDecision(
            should_exit=True,
            exit_price=new_trail_stop,
            reason="trailing_stop"
        )
    elif pos.side == "short" and current_price >= new_trail_stop:
        return SimpleExitDecision(
            should_exit=True,
            exit_price=new_trail_stop,
            reason="trailing_stop"
        )
    
    # 3. 止盈目标检查
    should_exit_tp, exit_ratio, tp_reason = check_profit_target(
        pos, current_price, pos.initial_stop_distance, partial_exit_done
    )
    if should_exit_tp:
        if exit_ratio == 1.0:
            return SimpleExitDecision(
                should_exit=True,
                exit_price=current_price,
                reason=tp_reason
            )
        else:
            # 部分止盈，这里返回特殊标记
            return SimpleExitDecision(
                should_exit=True,
                exit_price=current_price,
                reason=f"{tp_reason}_partial_{exit_ratio}"
            )
    
    # 4. 趋势衰竭检查
    if check_trend_exhaustion(pos, current_price, ema_long, pos.bars_held):
        return SimpleExitDecision(
            should_exit=True,
            exit_price=current_price,
            reason="trend_exhaustion_ema_cross"
        )
    
    # 5. 时间止损检查
    if pos.bars_held >= max_hold_bars:
        # 计算当前盈亏
        if pos.side == "long":
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        else:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price
        
        # 只有盈利时才考虑时间止损，亏损时让止损机制处理
        if pnl_pct > 0:
            return SimpleExitDecision(
                should_exit=True,
                exit_price=current_price,
                reason="time_stop_max_hold_period"
            )
    
    return SimpleExitDecision(
        should_exit=False,
        exit_price=current_price,
        reason="hold"
    )