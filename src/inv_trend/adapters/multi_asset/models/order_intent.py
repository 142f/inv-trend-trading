"""Explicit pending-order lifecycle and reservation accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from uuid import uuid4

from .domain import Order


@dataclass
class PendingOrderIntent:
    order: Order
    created_at: str
    signal_bar_time: str
    entry_period: int
    breakout_level: float
    requested_qty: float
    reserved_risk: float
    reserved_notional: float
    intent_id: str = field(default_factory=lambda: uuid4().hex)
    eligible_from: str | None = None
    approved_qty: float | None = None
    fill_attempts_completed: int = 0
    status: str = "pending"
    resolution: str = ""
    resolved_at: str | None = None

    def __post_init__(self) -> None:
        if self.requested_qty <= 0 or self.reserved_risk < 0 or self.reserved_notional < 0:
            raise ValueError("pending order quantities and reservations must be non-negative")


@dataclass(frozen=True)
class ReservationUsage:
    risk_total: float = 0.0
    risk_long: float = 0.0
    risk_short: float = 0.0
    notional_total: float = 0.0
    by_symbol: Mapping[str, float] = field(default_factory=dict)


class ReservationBook:
    def __init__(self) -> None:
        self.intents: dict[str, PendingOrderIntent] = {}

    def reserve(self, intent: PendingOrderIntent) -> None:
        if intent.status != "pending":
            raise ValueError("only pending intents may reserve capacity")
        self.intents[intent.intent_id] = intent

    def release(self, intent_id: str) -> None:
        self.intents.pop(intent_id, None)

    def usage_excluding(self, intent_id: str | None = None) -> ReservationUsage:
        total = long = short = notional = 0.0
        symbols: dict[str, float] = {}
        for candidate_id, intent in self.intents.items():
            if candidate_id == intent_id or intent.status != "pending":
                continue
            risk = intent.reserved_risk
            total += risk
            if intent.order.side > 0:
                long += risk
            else:
                short += risk
            notional += intent.reserved_notional
            symbols[intent.order.symbol] = symbols.get(intent.order.symbol, 0.0) + risk
        return ReservationUsage(total, long, short, notional, symbols)

    def total_usage(self) -> ReservationUsage:
        return self.usage_excluding()
