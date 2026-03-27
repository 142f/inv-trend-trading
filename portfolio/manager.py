from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

import pandas as pd

from config.models import Settings
from portfolio.accounting import AccountState
from portfolio.state import PositionState
from signals.exit_rules import should_force_exit, reverse_cross_30m


@dataclass
class PositionEvent:
    symbol: str
    side: str
    action: str
    quantity: float
    price: float
    reason: str


class PortfolioManager:
    def __init__(self, settings: Settings, account: AccountState):
        self.settings = settings
        self.account = account
        self.positions: Dict[str, PositionState] = {
            sym: PositionState(symbol=sym) for sym in settings.strategy.symbols
        }

    def mark_prices(self, latest_close_by_symbol: Dict[str, float]) -> Dict[str, float]:
        return latest_close_by_symbol

    def open_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        stop_price: float,
        risk_amount: float,
        grade: str,
        r_score: float,
        timestamp: datetime,
        fee: float,
        stop_distance: float,
    ) -> None:
        pos = self.positions[symbol]
        pos.side = side
        pos.entry_price = entry_price
        pos.entry_timestamp = timestamp
        pos.stop_price = stop_price
        pos.position_size = qty
        pos.initial_position_size = qty
        pos.initial_stop_distance = stop_distance
        pos.risk_amount = risk_amount
        pos.entry_grade = grade
        pos.r_score_at_entry = r_score
        pos.tp1_done = False
        pos.tp2_done = False
        pos.added_once = False
        pos.breakeven_active = False
        pos.reduce_stage = 0
        pos.force_exit_pending = False
        pos.bars_held = 0
        pos.highest_high = entry_price
        pos.lowest_low = entry_price
        pos.position_state = 1
        pos.fees_paid += fee
        self.account.apply_realized(0.0, fee)

    def _close_qty(self, pos: PositionState, qty: float, fill_price: float, fee: float) -> float:
        qty = min(qty, pos.position_size)
        if qty <= 0:
            return 0.0
        direction = 1.0 if pos.side == "long" else -1.0
        pnl = (fill_price - pos.entry_price) * qty * direction
        pos.position_size -= qty
        pos.realized_pnl += pnl
        pos.fees_paid += fee
        self.account.apply_realized(pnl, fee)
        if pos.position_size <= 1e-12:
            pos.reset()
        return pnl

    @staticmethod
    def _apply_exit_slippage(side: str, ref_price: float, slippage_bps: float) -> float:
        slip = slippage_bps / 10000.0
        # closing long => sell (worse lower), closing short => buy (worse higher)
        if side == "long":
            return ref_price * (1 - slip)
        return ref_price * (1 + slip)

    def add_on(
        self,
        symbol: str,
        qty: float,
        fill_price: float,
        fee: float,
    ) -> bool:
        pos = self.positions[symbol]
        if not pos.is_open or not pos.tp1_done or not pos.breakeven_active or pos.added_once:
            return False
        old_qty = pos.position_size
        new_qty = old_qty + qty
        if new_qty <= 0:
            return False
        pos.entry_price = (pos.entry_price * old_qty + fill_price * qty) / new_qty
        pos.position_size = new_qty
        pos.added_once = True
        pos.fees_paid += fee
        self.account.apply_realized(0.0, fee)
        return True

    def _update_r_multiple(self, pos: PositionState, mark_price: float) -> None:
        if not pos.is_open or pos.initial_stop_distance <= 0:
            return
        direction = 1.0 if pos.side == "long" else -1.0
        pos.r_multiple_current = (mark_price - pos.entry_price) * direction / pos.initial_stop_distance

    def manage_open_position(
        self,
        symbol: str,
        ts: datetime,
        bar_15m: pd.Series,
        row_30m: pd.Series | None,
        trend_state_1h: int,
        trend_state_2h: int,
        gate_allow_long: bool,
        gate_allow_short: bool,
        portfolio_force: bool,
        fee_rate: float,
        slippage_bps: float,
    ) -> List[PositionEvent]:
        events: List[PositionEvent] = []
        pos = self.positions[symbol]
        if not pos.is_open:
            return events

        pos.bars_held += 1
        close_px = float(bar_15m["close"])
        high_px = float(bar_15m["high"])
        low_px = float(bar_15m["low"])
        self._update_r_multiple(pos, close_px)

        # 1) hard stop
        if pos.side == "long" and low_px <= pos.stop_price:
            qty = pos.position_size
            fill_px = self._apply_exit_slippage(pos.side, pos.stop_price, slippage_bps)
            fee = fill_px * qty * fee_rate
            side = pos.side
            self._close_qty(pos, qty, fill_px, fee)
            events.append(PositionEvent(symbol, side, "close", qty, fill_px, "stop_loss"))
            return events
        if pos.side == "short" and high_px >= pos.stop_price:
            qty = pos.position_size
            fill_px = self._apply_exit_slippage(pos.side, pos.stop_price, slippage_bps)
            fee = fill_px * qty * fee_rate
            side = pos.side
            self._close_qty(pos, qty, fill_px, fee)
            events.append(PositionEvent(symbol, side, "close", qty, fill_px, "stop_loss"))
            return events

        # 2) forced exit
        force = should_force_exit(
            pos=pos,
            trend_state_1h=trend_state_1h,
            row_30m=row_30m,
            portfolio_force=portfolio_force,
            time_stop_bars=self.settings.strategy.time_stop_bars_15m,
            time_stop_min_r=self.settings.strategy.time_stop_min_r,
        )
        if force.force_exit:
            qty = pos.position_size
            fill_px = self._apply_exit_slippage(pos.side, close_px, slippage_bps)
            fee = fill_px * qty * fee_rate
            side = pos.side
            self._close_qty(pos, qty, fill_px, fee)
            events.append(PositionEvent(symbol, side, "close", qty, fill_px, force.reason))
            return events

        # Update extreme prices since entry
        pos.highest_high = max(pos.highest_high, high_px)
        pos.lowest_low = min(pos.lowest_low, low_px)

        # 3) Timeframe Promotion & Moonbag (Volatility-Adjusted)
        atr = float(bar_15m.get("atr14", 0.0))
        if atr <= 0:
            atr = pos.entry_price * 0.01  # fallback

        unrealized_profit = (close_px - pos.entry_price) if pos.side == "long" else (pos.entry_price - close_px)
        promotion_threshold = atr * 3.0

        if pos.position_state == 1 and unrealized_profit > promotion_threshold:
            # 跃迁：当浮盈超过3倍ATR时，认为脱离成本敏感区，执行分批减仓与状态升维
            pos.position_state = 2
            
            # 分批减仓 50%锁定初始仓位风险 (Risk Free Trade)
            qty = min(pos.initial_position_size * 0.5, pos.position_size)
            if qty > 0:
                fill_px = close_px  # 隐患1防御：Passive Limit Trailing 避免幽灵滑点穿仓
                fee_scale = fill_px * qty * fee_rate
                side_str = pos.side
                self._close_qty(pos, qty, fill_px, fee_scale)
                events.append(PositionEvent(symbol, side_str, "reduce", qty, fill_px, "timeframe_promotion_scale_out"))
            
            # 推保护性护城河保本损
            fee_buf = pos.entry_price * fee_rate * 2.0
            if pos.side == "long":
                pos.stop_price = max(pos.stop_price, pos.entry_price + fee_buf)
            else:
                pos.stop_price = min(pos.stop_price, pos.entry_price - fee_buf)

        if not pos.is_open:
            return events

        # 3.5) Dynamic ATR Trailing Stop (Chandelier Exit mechanism)
        # 短期紧贴跟踪 (2ATR)，长期宽幅跟踪抵御洗盘 (4ATR)
        atr_multiplier = 2.0 if pos.position_state == 1 else 4.0
        
        if pos.side == "long":
            trail_stop = pos.highest_high - (atr * atr_multiplier)
            if trail_stop > pos.stop_price:
                pos.stop_price = trail_stop
        elif pos.side == "short":
            trail_stop = pos.lowest_low + (atr * atr_multiplier)
            if trail_stop < pos.stop_price:
                pos.stop_price = trail_stop

        # 4) reduce rules
        if pos.side == "long":
            lost_gate_not_reversed = (not gate_allow_long) and (not gate_allow_short)
            if pos.reduce_stage < 1 and lost_gate_not_reversed:
                qty = pos.position_size * self.settings.risk.reduce_step_ratio
                fill_px = self._apply_exit_slippage(pos.side, close_px, slippage_bps)
                fee = fill_px * qty * fee_rate
                side = pos.side
                self._close_qty(pos, qty, fill_px, fee)
                pos.reduce_stage = 1
                events.append(PositionEvent(symbol, side, "reduce", qty, fill_px, "lost_gate_reduce_25"))
            if (
                pos.is_open
                and pos.reduce_stage < 2
                and trend_state_2h == 0
                and close_px < float(bar_15m["ema169"])
            ):
                qty = pos.position_size * self.settings.risk.reduce_step_ratio
                fill_px = self._apply_exit_slippage(pos.side, close_px, slippage_bps)
                fee = fill_px * qty * fee_rate
                side = pos.side
                self._close_qty(pos, qty, fill_px, fee)
                pos.reduce_stage = 2
                events.append(PositionEvent(symbol, side, "reduce", qty, fill_px, "2h_neutral_and_lost_ema169"))
        else:
            lost_gate_not_reversed = (not gate_allow_short) and (not gate_allow_long)
            if pos.reduce_stage < 1 and lost_gate_not_reversed:
                qty = pos.position_size * self.settings.risk.reduce_step_ratio
                fill_px = self._apply_exit_slippage(pos.side, close_px, slippage_bps)
                fee = fill_px * qty * fee_rate
                side = pos.side
                self._close_qty(pos, qty, fill_px, fee)
                pos.reduce_stage = 1
                events.append(PositionEvent(symbol, side, "reduce", qty, fill_px, "lost_gate_reduce_25"))
            if (
                pos.is_open
                and pos.reduce_stage < 2
                and trend_state_2h == 0
                and close_px > float(bar_15m["ema169"])
            ):
                qty = pos.position_size * self.settings.risk.reduce_step_ratio
                fill_px = self._apply_exit_slippage(pos.side, close_px, slippage_bps)
                fee = fill_px * qty * fee_rate
                side = pos.side
                self._close_qty(pos, qty, fill_px, fee)
                pos.reduce_stage = 2
                events.append(PositionEvent(symbol, side, "reduce", qty, fill_px, "2h_neutral_and_lost_ema169"))

        if not pos.is_open:
            return events

        # 5) runner exit by 30m reverse cross
        if pos.tp2_done and row_30m is not None and reverse_cross_30m(pos.side, row_30m):
            qty = pos.position_size
            fill_px = self._apply_exit_slippage(pos.side, close_px, slippage_bps)
            fee = fill_px * qty * fee_rate
            side = pos.side
            self._close_qty(pos, qty, fill_px, fee)
            events.append(PositionEvent(symbol, side, "close", qty, fill_px, "runner_30m_ema169_exit"))

        return events
