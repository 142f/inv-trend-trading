from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config.models import Settings
from data.exchange_client import SymbolConstraints
from indicators.ema_atr import add_atr, add_ema
from indicators.trend_state import add_trend_state
from execution.fills import Fill
from execution.order_simulator import simulate_market_fill
from portfolio.accounting import AccountState
from portfolio.manager import PortfolioManager
from risk.portfolio_limits import (
    DailyDrawdownTracker,
    apply_correlation_haircut,
    evaluate_portfolio_limits,
)
from risk.pretrade_checks import check_pretrade
from risk.dynamic_sizing import apply_dynamic_risk_pct
from risk.sizing import build_size_plan, compute_initial_stop, tier_params
from signals.entry_rules import evaluate_entry
from signals.resonance import classify_entry_grade, direction_gate
from backtest.funding import calc_funding_charge, funding_rate_lookup, is_funding_settlement


ALL_TFS = ["15m", "30m", "1h", "2h", "4h", "1d", "2d", "5d", "7d"]
REENTRY_BLOCK_REASONS = {
    "portfolio_risk_or_daily_dd",
    "1h_flip_and_30m_reverse_cross",
    "time_stop_under_0.5R",
}


@dataclass
class BacktestResult:
    fills: List[Fill] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    alerts: List[str] = field(default_factory=list)


def _prepare_frame(tf: str, df: pd.DataFrame, atr_period: int) -> pd.DataFrame:
    out = add_ema(df)
    if tf == "15m":
        out = add_atr(out, period=atr_period)
    out = add_trend_state(out)
    return out


def _latest_closed_row(df: pd.DataFrame, decision_ts: pd.Timestamp) -> Optional[pd.Series]:
    if df.empty:
        return None
    hit = df[df["close_ts"] <= decision_ts]
    if hit.empty:
        return None
    return hit.iloc[-1]


def _clip_qty(qty: float, precision: int) -> float:
    if qty <= 0:
        return 0.0
    scale = 10 ** precision
    return np.floor(qty * scale) / scale


class BacktestEngine:
    def __init__(
        self,
        settings: Settings,
        dataset: Dict[str, Dict[str, pd.DataFrame]],
        initial_equity: float = 10000.0,
        constraints_by_symbol: Optional[Dict[str, SymbolConstraints]] = None,
        funding_rates: Optional[Dict[str, Dict[datetime, float]]] = None,
        trade_start_ts: Optional[pd.Timestamp] = None,
    ):
        self.settings = settings
        self.dataset_raw = dataset
        self.dataset = self._prepare_dataset(dataset)
        self.account = AccountState(initial_equity=initial_equity)
        self.pm = PortfolioManager(settings=settings, account=self.account)
        self.dd_tracker = DailyDrawdownTracker()
        self.constraints = constraints_by_symbol or {
            s: SymbolConstraints(
                symbol=s,
                min_amount=0.001,
                min_notional=5.0,
                amount_precision=3 if "ETH" in s else 4,
                price_precision=2,
            )
            for s in settings.strategy.symbols
        }
        self.funding_rates = funding_rates or {}
        self.trade_start_ts = None
        if trade_start_ts is not None:
            t = pd.Timestamp(trade_start_ts)
            self.trade_start_ts = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        self.fills: List[Fill] = []
        self.alerts: List[str] = []
        self.equity_points: List[tuple[pd.Timestamp, float]] = []
        self._cursor_state: Dict[str, Dict[str, Dict[str, object]]] = {}
        self._init_cursor_state()

    def _prepare_dataset(self, dataset: Dict[str, Dict[str, pd.DataFrame]]) -> Dict[str, Dict[str, pd.DataFrame]]:
        prepared: Dict[str, Dict[str, pd.DataFrame]] = {}
        for symbol, frames in dataset.items():
            prepared[symbol] = {}
            for tf in ALL_TFS:
                if tf not in frames:
                    continue
                prepared[symbol][tf] = _prepare_frame(tf, frames[tf], self.settings.strategy.atr_period)
        return prepared

    def _collect_timeline(self) -> List[pd.Timestamp]:
        timeline = set()
        for symbol in self.settings.strategy.symbols:
            frame_15 = self.dataset[symbol]["15m"]
            timeline.update(frame_15["close_ts"].tolist())
        return sorted(timeline)

    def _init_cursor_state(self) -> None:
        self._cursor_state = {}
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

    @staticmethod
    def _ts_to_ns(ts: pd.Timestamp) -> int:
        t = ts.tz_convert("UTC") if ts.tzinfo is not None else ts.tz_localize("UTC")
        return int(t.value)

    def _context_rows_fast(self, symbol: str, ts: pd.Timestamp) -> Dict[str, Optional[pd.Series]]:
        ctx: Dict[str, Optional[pd.Series]] = {}
        target_ns = self._ts_to_ns(ts)
        for tf in ALL_TFS:
            frame = self.dataset[symbol].get(tf)
            st = self._cursor_state.get(symbol, {}).get(tf)
            if frame is None or st is None:
                ctx[tf] = None
                continue
            cursor = int(st["cursor"])
            close_ns = st["close_ns"]
            n = int(st["n"])
            while cursor + 1 < n and close_ns[cursor + 1] <= target_ns:
                cursor += 1
            st["cursor"] = cursor
            ctx[tf] = frame.iloc[cursor] if cursor >= 0 else None
        return ctx

    def _states_from_ctx(self, ctx: Dict[str, Optional[pd.Series]]) -> Dict[str, int]:
        out = {}
        for tf, row in ctx.items():
            out[tf] = int(row["trend_state"]) if row is not None else 0
        return out

    def _all_required_present(self, ctx: Dict[str, Optional[pd.Series]]) -> bool:
        return all(ctx.get(tf) is not None for tf in ALL_TFS)

    def _record_fill(self, ts: datetime, symbol: str, side: str, action: str, price: float, qty: float, reason: str):
        notional = price * qty
        fee = notional * self.settings.cost.fee_taker
        slip_cost = 0.0
        self.fills.append(
            Fill(
                timestamp=ts,
                symbol=symbol,
                side=side,
                action=action,
                price=price,
                quantity=qty,
                notional=notional,
                fee=fee,
                slippage_cost=slip_cost,
                reason=reason,
            )
        )

    def _estimate_bid_ask_from_bar(self, row_15m: pd.Series) -> tuple[float, float]:
        close_px = float(row_15m["close"])
        high_px = float(row_15m["high"])
        low_px = float(row_15m["low"])
        if close_px <= 0:
            return 0.0, 0.0

        # Backtest proxy: use a fraction of realized candle range as spread estimate.
        range_bps = max((high_px - low_px) / close_px * 10000.0, 0.0)
        proxy_spread_bps = max(0.8, min(30.0, range_bps * 0.12))
        half = proxy_spread_bps / 2.0 / 10000.0
        bid = close_px * (1.0 - half)
        ask = close_px * (1.0 + half)
        return float(bid), float(ask)

    def _lookup_funding_rate_for_pretrade(self, symbol: str, ts: pd.Timestamp) -> float | None:
        if not self.funding_rates:
            return None
        return funding_rate_lookup(self.funding_rates, symbol, ts.to_pydatetime())

    def _maybe_apply_funding(self, ts: datetime, marks: Dict[str, float]):
        if not is_funding_settlement(ts, self.settings.cost.funding_settlement_hours):
            return
        for symbol in self.settings.strategy.symbols:
            pos = self.pm.positions[symbol]
            if not pos.is_open:
                continue
            rate = funding_rate_lookup(self.funding_rates, symbol, ts)
            charge = calc_funding_charge(pos, marks.get(symbol, pos.entry_price), ts, rate)
            if charge.missing_used_zero:
                self.alerts.append(f"{ts.isoformat()} {symbol} funding missing -> use 0")
            # positive amount means paid by strategy
            self.account.apply_realized(-charge.amount, 0.0)

    def run(self) -> BacktestResult:
        timeline = self._collect_timeline()
        for ts in timeline:
            blocked_symbols_this_cycle = set()
            latest_marks: Dict[str, float] = {}
            ctx_by_symbol: Dict[str, Dict[str, Optional[pd.Series]]] = {}
            for symbol in self.settings.strategy.symbols:
                ctx = self._context_rows_fast(symbol, ts)
                ctx_by_symbol[symbol] = ctx
                row15 = ctx.get("15m")
                if row15 is not None:
                    latest_marks[symbol] = float(row15["close"])

            if self.trade_start_ts is not None and ts < self.trade_start_ts:
                eq_idle = self.account.equity(self.pm.positions, latest_marks)
                self.equity_points.append((ts, eq_idle))
                continue

            eq_now = self.account.equity(self.pm.positions, latest_marks)
            self.dd_tracker.update(ts.to_pydatetime(), eq_now)
            daily_dd = self.dd_tracker.daily_drawdown()
            port_status = evaluate_portfolio_limits(
                settings=self.settings,
                equity=eq_now,
                positions=self.pm.positions,
                mark_prices=latest_marks,
                daily_dd=daily_dd,
            )

            self._maybe_apply_funding(ts.to_pydatetime(), latest_marks)

            # 1) manage existing positions
            for symbol in self.settings.strategy.symbols:
                ctx = ctx_by_symbol[symbol]
                row15 = ctx.get("15m")
                if row15 is None:
                    continue
                states = self._states_from_ctx(ctx)
                gate = direction_gate(
                    states=states,
                    weights=self.settings.strategy.weights,
                    long_gate=self.settings.strategy.r_gate_long,
                    short_gate=self.settings.strategy.r_gate_short,
                )
                events = self.pm.manage_open_position(
                    symbol=symbol,
                    ts=ts.to_pydatetime(),
                    bar_15m=row15,
                    row_30m=ctx.get("30m"),
                    trend_state_1h=states.get("1h", 0),
                    trend_state_2h=states.get("2h", 0),
                    gate_allow_long=gate.allow_long,
                    gate_allow_short=gate.allow_short,
                    portfolio_force=port_status.hard_limit_breached or port_status.daily_dd_hit,
                    fee_rate=self.settings.cost.fee_taker,
                    slippage_bps=self.settings.cost.slippage_bps,
                )
                for ev in events:
                    if ev.reason in REENTRY_BLOCK_REASONS:
                        blocked_symbols_this_cycle.add(symbol)
                    self._record_fill(
                        ts=ts.to_pydatetime(),
                        symbol=symbol,
                        side=ev.side,
                        action=ev.action,
                        price=ev.price,
                        qty=ev.quantity,
                        reason=ev.reason,
                    )

            # refresh equity after management
            eq_now = self.account.equity(self.pm.positions, latest_marks)
            self.dd_tracker.update(ts.to_pydatetime(), eq_now)
            daily_dd = self.dd_tracker.daily_drawdown()
            port_status = evaluate_portfolio_limits(
                settings=self.settings,
                equity=eq_now,
                positions=self.pm.positions,
                mark_prices=latest_marks,
                daily_dd=daily_dd,
            )

            # 2) evaluate add-on and new entries
            for symbol in self.settings.strategy.symbols:
                if symbol in blocked_symbols_this_cycle:
                    continue
                ctx = ctx_by_symbol[symbol]
                row15 = ctx.get("15m")
                if row15 is None:
                    continue

                states = self._states_from_ctx(ctx)
                gate = direction_gate(
                    states=states,
                    weights=self.settings.strategy.weights,
                    long_gate=self.settings.strategy.r_gate_long,
                    short_gate=self.settings.strategy.r_gate_short,
                )
                has_full_data = self._all_required_present(ctx)
                if not has_full_data:
                    continue

                pos = self.pm.positions[symbol]
                if pos.is_open:
                    # add-on path
                    if pos.tp1_done and pos.breakeven_active and not pos.added_once:
                        side = pos.side
                        dec = evaluate_entry(
                            side=side,
                            gate=gate,
                            trend_states=states,
                            frames={k: self.dataset[symbol][k] for k in ["15m", "30m"]},
                            decision_close_ts=ts,
                            breakout_filter=self.settings.strategy.breakout_filter,
                            min_mid_tf_confirm=self.settings.strategy.min_mid_tf_confirm,
                            short_min_mid_tf_confirm=self.settings.strategy.short_min_mid_tf_confirm,
                            require_daily_align_for_short=self.settings.strategy.require_daily_align_for_short,
                            adaptive_filter_enabled=self.settings.strategy.adaptive_filter_enabled,
                            adaptive_filter_lookback_15m=self.settings.strategy.adaptive_filter_lookback_15m,
                            adaptive_atr_q_low=self.settings.strategy.adaptive_atr_q_low,
                            adaptive_atr_q_high=self.settings.strategy.adaptive_atr_q_high,
                            adaptive_score_q=self.settings.strategy.adaptive_score_q,
                        )
                        if dec.can_enter:
                            add_qty = pos.initial_position_size * self.settings.risk.add_on_ratio
                            cst = self.constraints[symbol]
                            add_qty = _clip_qty(add_qty, cst.amount_precision)
                            bid, ask = self._estimate_bid_ask_from_bar(row15)
                            fr = self._lookup_funding_rate_for_pretrade(symbol, ts)
                            pre = check_pretrade(
                                settings=self.settings,
                                symbol=symbol,
                                side=side,
                                qty=add_qty,
                                entry_price=float(dec.trigger_price),
                                constraints=cst,
                                bid=bid,
                                ask=ask,
                                funding_rate=fr,
                                can_open_portfolio=port_status.can_open_new,
                                symbol_has_position=False,
                            )
                            if pre.allowed and add_qty > 0:
                                fill = simulate_market_fill(
                                    settings=self.settings,
                                    timestamp=ts.to_pydatetime(),
                                    symbol=symbol,
                                    position_side=side,
                                    action="add",
                                    ref_price=float(dec.trigger_price),
                                    quantity=add_qty,
                                    reason="add_once_after_tp1",
                                )
                                ok = self.pm.add_on(symbol=symbol, qty=fill.quantity, fill_price=fill.price, fee=fill.fee)
                                if ok:
                                    self.fills.append(fill)
                    continue

                if not port_status.can_open_new:
                    continue

                # New entry path
                side = "long" if gate.allow_long else "short" if gate.allow_short else ""
                if not side:
                    continue

                dec = evaluate_entry(
                    side=side,
                    gate=gate,
                    trend_states=states,
                    frames={k: self.dataset[symbol][k] for k in ["15m", "30m"]},
                    decision_close_ts=ts,
                    breakout_filter=self.settings.strategy.breakout_filter,
                    min_mid_tf_confirm=self.settings.strategy.min_mid_tf_confirm,
                    short_min_mid_tf_confirm=self.settings.strategy.short_min_mid_tf_confirm,
                    require_daily_align_for_short=self.settings.strategy.require_daily_align_for_short,
                    adaptive_filter_enabled=self.settings.strategy.adaptive_filter_enabled,
                    adaptive_filter_lookback_15m=self.settings.strategy.adaptive_filter_lookback_15m,
                    adaptive_atr_q_low=self.settings.strategy.adaptive_atr_q_low,
                    adaptive_atr_q_high=self.settings.strategy.adaptive_atr_q_high,
                    adaptive_score_q=self.settings.strategy.adaptive_score_q,
                )
                if not dec.can_enter:
                    continue

                mid_confirm = sum(
                    1
                    for tf in ["4h", "2h", "1h"]
                    if states.get(tf, 0) == (1 if side == "long" else -1)
                )
                grade = classify_entry_grade(gate.r_score, mid_confirm, side=side)
                if grade is None:
                    continue

                atr = float(row15["atr14"])
                stop = compute_initial_stop(
                    side=side,
                    bar_15m=row15,
                    frame_15m=self.dataset[symbol]["15m"][self.dataset[symbol]["15m"]["close_ts"] <= ts],
                    stop_atr_mult=self.settings.strategy.stop_atr_mult,
                    slippage_buffer_bps=self.settings.cost.slippage_buffer_bps,
                )
                base_risk_pct, _ = tier_params(self.settings, grade)
                risk_pct_adj = apply_correlation_haircut(
                    settings=self.settings,
                    symbol=symbol,
                    side=side,
                    risk_pct=base_risk_pct,
                    positions=self.pm.positions,
                )
                risk_pct_adj = apply_dynamic_risk_pct(
                    settings=self.settings,
                    base_risk_pct=risk_pct_adj,
                    side=side,
                    fr_15m=self.dataset[symbol]["15m"],
                    decision_close_ts=ts,
                )
                size_plan = build_size_plan(
                    settings=self.settings,
                    side=side,
                    grade=grade,
                    equity=eq_now,
                    entry_price=float(dec.trigger_price),
                    stop_price=stop,
                    atr=atr,
                    risk_pct_override=risk_pct_adj,
                )
                cst = self.constraints[symbol]
                qty = _clip_qty(size_plan.position_size, cst.amount_precision)
                bid, ask = self._estimate_bid_ask_from_bar(row15)
                fr = self._lookup_funding_rate_for_pretrade(symbol, ts)
                pre = check_pretrade(
                    settings=self.settings,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    entry_price=float(dec.trigger_price),
                    constraints=cst,
                    bid=bid,
                    ask=ask,
                    funding_rate=fr,
                    can_open_portfolio=port_status.can_open_new,
                    symbol_has_position=self.pm.positions[symbol].is_open,
                )
                if not pre.allowed:
                    continue

                fill = simulate_market_fill(
                    settings=self.settings,
                    timestamp=ts.to_pydatetime(),
                    symbol=symbol,
                    position_side=side,
                    action="open",
                    ref_price=float(dec.trigger_price),
                    quantity=qty,
                    reason=f"entry_{grade}_{dec.signal_timeframe}",
                )
                self.pm.open_position(
                    symbol=symbol,
                    side=side,
                    qty=fill.quantity,
                    entry_price=fill.price,
                    stop_price=size_plan.stop_price,
                    risk_amount=size_plan.risk_amount,
                    grade=grade,
                    r_score=gate.r_score,
                    timestamp=ts.to_pydatetime(),
                    fee=fill.fee,
                    stop_distance=size_plan.stop_distance,
                )
                self.fills.append(fill)

            # end-of-cycle snapshot
            eq_end = self.account.equity(self.pm.positions, latest_marks)
            self.equity_points.append((ts, eq_end))

        eq_curve = pd.Series(
            data=[x[1] for x in self.equity_points],
            index=pd.DatetimeIndex([x[0] for x in self.equity_points], tz="UTC"),
            name="equity",
        )
        return BacktestResult(fills=self.fills, equity_curve=eq_curve, alerts=self.alerts)
