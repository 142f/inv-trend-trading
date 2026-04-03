from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config.models import Settings
from indicators.ema_atr import add_atr, add_ema
from portfolio.accounting import AccountState
from portfolio.state import PositionState
from signals.simple_entry import SimpleSignal, detect_simple_entry
from signals.simple_exit import SimpleExitDecision, check_simple_exit
from execution.fills import Fill


@dataclass
class SimpleBacktestResult:
    fills: List[Fill] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    signals_generated: int = 0
    positions_taken: int = 0
    total_trades: int = 0
    win_rate: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0


class SimpleBacktestEngine:
    """
    简化的回测引擎 - V8策略
    
    核心特点：
    1. 极简入场：基于EMA回撤形态 + 动量确认
    2. 简化出场：动态跟踪止损 + 固定止盈目标
    3. 移除复杂的多重确认和状态机
    4. 固定仓位管理：每笔交易风险1%
    """
    
    def __init__(
        self,
        settings: Settings,
        dataset: Dict[str, Dict[str, pd.DataFrame]],
        initial_equity: float = 10000.0,
        min_confidence: float = 0.6,
        risk_per_trade: float = 0.01,  # 每笔交易风险1%
        max_positions: int = 1,  # 最多同时持有1个仓位
    ):
        self.settings = settings
        self.dataset = self._prepare_dataset(dataset)
        self.account = AccountState(initial_equity=initial_equity)
        self.positions: Dict[str, PositionState] = {
            sym: PositionState(symbol=sym) for sym in settings.strategy.symbols
        }
        self.min_confidence = min_confidence
        self.risk_per_trade = risk_per_trade
        self.max_positions = max_positions
        
        self.fills: List[Fill] = []
        self.equity_points: List[tuple[pd.Timestamp, float]] = []
        self.signals_generated = 0
        self.positions_taken = 0
        
        # 统计数据
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        
        self._init_cursor_state()
    
    def _prepare_dataset(self, dataset: Dict[str, Dict[str, pd.DataFrame]]) -> Dict[str, Dict[str, pd.DataFrame]]:
        """准备数据集，添加必要的指标"""
        prepared: Dict[str, Dict[str, pd.DataFrame]] = {}
        for symbol, frames in dataset.items():
            prepared[symbol] = {}
            for tf, df in frames.items():
                if df.empty:
                    continue
                # 仅在缺失时计算指标，避免对已准备数据重复计算
                df_with_indicators = df
                if "ema144" not in df_with_indicators.columns and "ema169" not in df_with_indicators.columns:
                    df_with_indicators = add_ema(df_with_indicators)
                if tf == "15m" and "atr14" not in df_with_indicators.columns:
                    df_with_indicators = add_atr(df_with_indicators, period=14)
                prepared[symbol][tf] = df_with_indicators
        return prepared
    
    def _init_cursor_state(self) -> None:
        """初始化游标状态，用于快速查找历史数据"""
        self._cursor_state: Dict[str, Dict[str, Dict[str, object]]] = {}
        for symbol, frames in self.dataset.items():
            self._cursor_state[symbol] = {}
            for tf, frame in frames.items():
                if frame is None or frame.empty or "close_ts" not in frame.columns:
                    continue
                close_idx = pd.DatetimeIndex(frame["close_ts"])
                if close_idx.tz is None:
                    close_idx = close_idx.tz_localize("UTC")
                else:
                    close_idx = close_idx.tz_convert("UTC")
                self._cursor_state[symbol][tf] = {
                    "cursor": -1,
                    "close_ns": close_idx.asi8,
                    "n": len(frame),
                }
    
    def _context_rows_fast(self, symbol: str, ts: pd.Timestamp) -> Dict[str, Optional[pd.Series]]:
        """快速获取指定时间点的各周期K线数据"""
        ctx: Dict[str, Optional[pd.Series]] = {}
        target_ns = int(ts.tz_convert("UTC").value if ts.tzinfo else ts.tz_localize("UTC").value)
        
        for tf in ["15m", "30m", "1h", "2h", "4h", "1d"]:
            frame = self.dataset[symbol].get(tf)
            st = self._cursor_state.get(symbol, {}).get(tf)
            if frame is None or st is None:
                ctx[tf] = None
                continue
            
            cursor = int(st["cursor"])
            close_ns = st["close_ns"]
            n = int(st["n"])
            
            # 移动游标到目标时间点
            while cursor + 1 < n and close_ns[cursor + 1] <= target_ns:
                cursor += 1
            st["cursor"] = cursor
            
            ctx[tf] = frame.iloc[cursor] if cursor >= 0 else None
        
        return ctx
    
    def _calculate_position_size(
        self, 
        entry_price: float, 
        stop_price: float, 
        equity: float
    ) -> float:
        """
        计算仓位大小
        
        逻辑：
        - 固定风险比例（默认1%）
        - 根据止损距离计算仓位大小
        """
        risk_amount = equity * self.risk_per_trade
        stop_distance = abs(entry_price - stop_price)
        
        if stop_distance <= 0:
            return 0.0
        
        raw_position_size = risk_amount / stop_distance
        if entry_price <= 0:
            return 0.0

        # 通过硬杠杆上限限制最大名义仓位，避免极小止损距离导致过大仓位
        max_notional = equity * float(self.settings.risk.max_leverage_hard)
        max_position_size = max_notional / entry_price
        position_size = min(raw_position_size, max_position_size)
        return position_size
    
    def _record_fill(
        self, 
        ts: datetime, 
        symbol: str, 
        side: str, 
        action: str, 
        price: float, 
        qty: float, 
        reason: str
    ) -> Fill:
        """记录成交"""
        notional = price * qty
        fee = notional * self.settings.cost.fee_taker
        
        fill = Fill(
            timestamp=ts,
            symbol=symbol,
            side=side,
            action=action,
            price=price,
            quantity=qty,
            notional=notional,
            fee=fee,
            slippage_cost=0.0,
            reason=reason,
        )
        self.fills.append(fill)
        return fill
    
    def _open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_price: float,
        qty: float,
        timestamp: datetime,
        confidence: float
    ):
        """开仓"""
        pos = self.positions[symbol]
        pos.side = side
        pos.entry_price = entry_price
        pos.entry_timestamp = timestamp
        pos.stop_price = stop_price
        pos.position_size = qty
        pos.initial_position_size = qty
        pos.initial_stop_distance = abs(entry_price - stop_price)
        pos.r_score_at_entry = confidence
        pos.bars_held = 0
        pos.highest_high = entry_price
        pos.lowest_low = entry_price
        pos.position_state = 1
        
        # 计算风险金额
        risk_amount = abs(entry_price - stop_price) * qty
        pos.risk_amount = risk_amount
        
        self.positions_taken += 1
        fill = self._record_fill(timestamp, symbol, side, "open", entry_price, qty, f"simple_entry_confidence_{confidence:.2f}")
        self.account.apply_realized(0.0, fill.fee)
    
    def _close_position(
        self,
        symbol: str,
        exit_price: float,
        timestamp: datetime,
        reason: str,
        exit_ratio: float = 1.0
    ):
        """平仓"""
        pos = self.positions[symbol]
        if not pos.is_open:
            return
        
        qty_to_close = pos.position_size * exit_ratio
        if qty_to_close <= 0:
            return
        
        # 计算盈亏
        if pos.side == "long":
            pnl = (exit_price - pos.entry_price) * qty_to_close
        else:
            pnl = (pos.entry_price - exit_price) * qty_to_close
        
        # 更新仓位状态
        pos.position_size -= qty_to_close
        pos.realized_pnl += pnl

        # 记录成交
        fill = self._record_fill(timestamp, symbol, pos.side, "close", exit_price, qty_to_close, reason)
        # 应用盈亏到账户状态
        self.account.apply_realized(pnl, fill.fee)
        
        # 仅在完整平仓时统计一次交易，避免部分平仓扭曲胜率与交易次数
        if pos.position_size <= 1e-12:
            trade_pnl = pos.realized_pnl
            self.total_trades += 1
            if trade_pnl > 0:
                self.winning_trades += 1
                self.gross_profit += trade_pnl
            else:
                self.losing_trades += 1
                self.gross_loss += abs(trade_pnl)
            pos.reset()
    
    def _collect_timeline(self) -> List[pd.Timestamp]:
        """收集所有时间点"""
        timeline = set()
        for symbol in self.settings.strategy.symbols:
            frame_15 = self.dataset[symbol]["15m"]
            timeline.update(frame_15["close_ts"].tolist())
        return sorted(timeline)
    
    def _get_current_equity(self, mark_prices: Dict[str, float]) -> float:
        """计算当前权益"""
        # 使用AccountState的equity方法
        return self.account.equity(self.positions, mark_prices)
    
    def run(self) -> SimpleBacktestResult:
        """运行回测"""
        timeline = self._collect_timeline()
        
        for ts in timeline:
            # 获取当前市场价格
            mark_prices: Dict[str, float] = {}
            ctx_by_symbol: Dict[str, Dict[str, Optional[pd.Series]]] = {}
            
            for symbol in self.settings.strategy.symbols:
                ctx = self._context_rows_fast(symbol, ts)
                ctx_by_symbol[symbol] = ctx
                row15 = ctx.get("15m")
                if row15 is not None:
                    mark_prices[symbol] = float(row15["close"])
            
            # 计算当前权益
            current_equity = self._get_current_equity(mark_prices)
            self.equity_points.append((ts, current_equity))
            
            # 1. 管理现有仓位
            for symbol in self.settings.strategy.symbols:
                pos = self.positions[symbol]
                if not pos.is_open:
                    continue
                
                ctx = ctx_by_symbol[symbol]
                row15 = ctx.get("15m")
                if row15 is None:
                    continue
                
                current_price = float(row15["close"])
                atr = float(row15.get("atr14", current_price * 0.01))
                ema_long = float(row15.get("ema144", row15.get("ema169", current_price)))
                
                # 更新持仓统计
                pos.bars_held += 1
                pos.highest_high = max(pos.highest_high, current_price)
                pos.lowest_low = min(pos.lowest_low, current_price)
                
                # 检查出场条件
                exit_decision = check_simple_exit(
                    pos=pos,
                    current_price=current_price,
                    atr=atr,
                    ema_long=ema_long,
                    partial_exit_done=(pos.position_size < pos.initial_position_size),
                    max_hold_bars=384  # 4天
                )
                
                if exit_decision.should_exit:
                    # 判断是部分平仓还是完全平仓
                    if "partial" in exit_decision.reason:
                        # 提取平仓比例
                        try:
                            exit_ratio = float(exit_decision.reason.split("_")[-1])
                        except:
                            exit_ratio = 0.5
                        self._close_position(
                            symbol, exit_decision.exit_price, ts.to_pydatetime(),
                            exit_decision.reason, exit_ratio
                        )
                    else:
                        # 完全平仓
                        self._close_position(
                            symbol, exit_decision.exit_price, ts.to_pydatetime(),
                            exit_decision.reason
                        )
            
            # 2. 检查新入场机会
            # 检查当前持仓数量
            open_positions = sum(1 for pos in self.positions.values() if pos.is_open)
            if open_positions >= self.max_positions:
                continue
            
            for symbol in self.settings.strategy.symbols:
                # 跳过已有仓位的品种
                if self.positions[symbol].is_open:
                    continue
                
                ctx = ctx_by_symbol[symbol]
                row15 = ctx.get("15m")
                if row15 is None:
                    continue
                
                # 检查数据完整性
                if not all(ctx.get(tf) is not None for tf in ["15m", "1h", "4h"]):
                    continue
                
                # 获取趋势方向（基于4小时EMA）
                row4h = ctx["4h"]
                ema4h = float(row4h.get("ema144", row4h.get("ema169", row4h["close"])))
                close4h = float(row4h["close"])
                
                # 确定交易方向
                if close4h > ema4h:
                    side = "long"
                elif close4h < ema4h:
                    side = "short"
                else:
                    continue  # 中性，不交易
                
                # 检测入场信号
                signal = detect_simple_entry(
                    df=self.dataset[symbol]["15m"],
                    close_ts=ts,
                    side=side,
                    min_confidence=self.min_confidence
                )

                if signal.valid:
                    self.signals_generated += 1
                    # 检查触发价格
                    current_price = float(row15["close"])
                    if (side == "long" and current_price >= signal.trigger_price) or \
                       (side == "short" and current_price <= signal.trigger_price):
                        # 使用当前可成交价执行，避免穿越触发后仍按旧触发价成交造成非现实利润
                        entry_price = current_price
                        
                        # 计算仓位大小
                        position_size = self._calculate_position_size(
                            entry_price, signal.stop_price, current_equity
                        )
                        
                        if position_size > 0:
                            self._open_position(
                                symbol, side, entry_price, 
                                signal.stop_price, position_size, 
                                ts.to_pydatetime(), signal.confidence
                            )
                            
                            # 只开一个仓位
                            break
        
        # 计算最终统计
        return self._calculate_results()
    
    def _calculate_results(self) -> SimpleBacktestResult:
        """计算回测结果"""
        if not self.equity_points:
            return SimpleBacktestResult()
        
        # 构建权益曲线
        equity_series = pd.Series(
            [eq for _, eq in self.equity_points],
            index=[ts for ts, _ in self.equity_points]
        )
        
        # 计算总收益
        initial_equity = self.equity_points[0][1]
        final_equity = self.equity_points[-1][1]
        total_return = (final_equity - initial_equity) / initial_equity
        
        # 计算最大回撤
        rolling_max = equity_series.expanding().max()
        drawdown = (equity_series - rolling_max) / rolling_max
        max_drawdown = drawdown.min()
        
        # 计算胜率
        win_rate = self.winning_trades / self.total_trades if self.total_trades > 0 else 0.0
        
        # 计算盈亏比
        profit_factor = self.gross_profit / self.gross_loss if self.gross_loss > 0 else 0.0
        
        return SimpleBacktestResult(
            fills=self.fills,
            equity_curve=equity_series,
            signals_generated=self.signals_generated,
            positions_taken=self.positions_taken,
            total_trades=self.total_trades,
            win_rate=win_rate,
            total_return=total_return,
            max_drawdown=max_drawdown,
            profit_factor=profit_factor
        )