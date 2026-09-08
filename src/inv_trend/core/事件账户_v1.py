"""Causal cash account: prior-close orders execute at later opens; no future volume."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math

from .阶段协议_v1 import AccountSnapshot, Bar, Fill, Signal, digest, finite, timestamp


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    initial_cash: float = 100_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 5.0
    delay_bars: int = 1
    volume_participation: float = 0.01
    lot_size: float = 1.0
    max_weight: float = 0.95

    def __post_init__(self) -> None:
        for key in ("initial_cash", "lot_size"):
            finite(key, getattr(self, key), minimum=1e-12)
        for key in ("fee_bps", "slippage_bps"):
            finite(key, getattr(self, key), minimum=0)
            if getattr(self, key) >= 10_000:
                raise ValueError(f"{key} must be below 10000")
        for key in ("volume_participation", "max_weight"):
            finite(key, getattr(self, key), minimum=1e-12)
            if getattr(self, key) > 1:
                raise ValueError(f"{key} must be <=1")
        if type(self.delay_bars) is not int or self.delay_bars < 1:
            raise ValueError("at least one completed-bar execution delay is mandatory")


@dataclass(frozen=True, slots=True)
class PendingOrder:
    id: str
    signal_at: str
    due_step: int
    target_quantity: float


class CashBroker:
    """Long-only, no leverage/borrow/funding fiction. Shorts map to cash explicitly.

    Opening execution sees only current OPEN, prior completed volume and committed
    orders. Daily high/low/close/current-day total volume never influence that fill.
    Partial orders expire; next completed signal may request the remaining quantity.
    Cash and positions persist across walk-forward boundaries; no terminal liquidation.
    """
    def __init__(self, config: ExecutionConfig):
        self.config = config
        self.cash = config.initial_cash
        self.quantity = 0.0
        self.step = 0
        self.last_bar: Bar | None = None
        self.pending: deque[PendingOrder] = deque()
        self.fees = 0.0
        self.slippage_cost = 0.0
        self.fill_count = 0
        self.short_signals_blocked = 0

    def _round(self, value: float) -> float:
        return math.floor(max(0.0, value) / self.config.lot_size + 1e-10) * self.config.lot_size

    def on_signal(self, signal: Signal, *, audit: bool = True) -> AccountSnapshot:
        bar = signal.features.bar
        if self.last_bar:
            if bar.symbol != self.last_bar.symbol or bar.available_at <= self.last_bar.available_at:
                raise ValueError("account requires one symbol in strictly increasing time")
            if bar.opened_at < self.last_bar.available_at:
                raise ValueError("overlapping sessions are unsupported")
        if not self.last_bar and self.pending:
            raise ValueError("pending orders lack completed previous session")
        self.step += 1
        fills: list[Fill] = []
        # Liquidity information is deliberately lagged one completed session.
        capacity = self._round((self.last_bar.volume if self.last_bar else 0.0)
                               * self.config.volume_participation)
        while self.pending and self.pending[0].due_step <= self.step:
            order = self.pending.popleft()
            if timestamp(order.signal_at) > timestamp(bar.opened_at):
                raise ValueError("order cannot execute before its signal exists")
            desired = order.target_quantity - self.quantity
            if abs(desired) < self.config.lot_size * (1 - 1e-8):
                continue
            side = 1 if desired > 0 else -1
            price = bar.open * (1 + side * self.config.slippage_bps / 10000)
            requested = self._round(abs(desired))
            amount = min(requested, capacity)
            reason = ""
            if side > 0:
                # Fee-aware capital and exposure caps account for overnight gaps.
                open_equity = self.cash + self.quantity * bar.open
                capital = self.cash / (price * (1 + self.config.fee_bps / 10000))
                # Exposure is measured against POST-cost opening equity, not gross equity.
                cost_per_unit = price * (1 + self.config.fee_bps / 10000)
                denominator = bar.open - self.config.max_weight * (bar.open - cost_per_unit)
                exposure = max(0.0, (self.config.max_weight * open_equity
                                    - self.quantity * bar.open) / denominator)
                amount = self._round(min(amount, capital, exposure))
            else:
                amount = self._round(min(amount, self.quantity))
            if amount < requested:
                reason = "capital/exposure/lagged_volume/lot constraint; remainder expired"
            fee = amount * price * self.config.fee_bps / 10000
            slip = amount * abs(price - bar.open)
            quantity = side * amount
            self.cash -= quantity * price + fee
            self.quantity += quantity
            self.fees += fee
            self.slippage_cost += slip
            capacity -= amount
            if amount:
                self.fill_count += 1
            if self.cash < -1e-6 or self.quantity < -1e-9:
                raise ArithmeticError("cash or long-only position constraint violated")
            fills.append(Fill(order.id, order.signal_at, bar.opened_at, side * requested,
                              quantity, price, fee, slip,
                              "FILLED" if amount == requested else "PARTIAL" if amount else "REJECTED",
                              reason))
        equity = self.cash + self.quantity * bar.close
        if signal.target_weight < 0:
            self.short_signals_blocked += 1
        weight = min(self.config.max_weight, max(0.0, signal.target_weight))
        target = self._round(weight * equity / bar.close)
        if signal.candidate_id.startswith("buy_hold-") and self.quantity > 0:
            target = self.quantity  # fixed passive reference: do not rebalance after entry
        identity = digest({"candidate": signal.candidate_id, "at": bar.available_at,
                           "target": target, "step": self.step}) if audit else f"train-{self.step}"
        self.pending.append(PendingOrder(identity, bar.available_at,
                                         self.step + self.config.delay_bars, target))
        self.last_bar = bar
        return AccountSnapshot(bar.symbol, bar.available_at, self.cash, self.quantity, bar.close,
                               equity, self.config.initial_cash, self.fees, self.slippage_cost,
                               tuple(fills), len(self.pending), digest(signal) if audit else "TRAIN_ONLY")

    def snapshot(self) -> dict:
        return {"config": asdict(self.config), "cash": self.cash, "quantity": self.quantity,
                "step": self.step, "last_bar": asdict(self.last_bar) if self.last_bar else None,
                "pending": [asdict(x) for x in self.pending], "fees": self.fees,
                "slippage_cost": self.slippage_cost, "fill_count": self.fill_count,
                "short_signals_blocked": self.short_signals_blocked}

    def restore(self, state: dict) -> None:
        if ExecutionConfig(**state["config"]) != self.config:
            raise ValueError("execution checkpoint configuration mismatch")
        for key in ("cash", "quantity", "fees", "slippage_cost"):
            finite(key, state[key], minimum=-1e-9)
        self.cash, self.quantity = state["cash"], state["quantity"]
        self.step = state["step"]
        self.last_bar = Bar(**state["last_bar"]) if state["last_bar"] else None
        self.pending = deque(PendingOrder(**x) for x in state["pending"])
        self.fees, self.slippage_cost = state["fees"], state["slippage_cost"]
        self.fill_count = state["fill_count"]
        self.short_signals_blocked = state["short_signals_blocked"]
