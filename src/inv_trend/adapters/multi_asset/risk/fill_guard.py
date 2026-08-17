from __future__ import annotations

from typing import Mapping

from ..models.domain import PortfolioState
from ..models.order_intent import PendingOrderIntent, ReservationBook
from ..strategy.budget_policy import BudgetDecision, PortfolioBudgetPolicy


class FillRiskGuard:
    def __init__(self, policy: PortfolioBudgetPolicy) -> None:
        self.policy = policy

    def validate(
        self,
        intent: PendingOrderIntent,
        fill_price: float,
        state: PortfolioState,
        reservations: ReservationBook,
        equity: float,
        prices_at_open: Mapping[str, float],
    ) -> BudgetDecision:
        return self.policy.evaluate(
            symbol=intent.order.symbol,
            side=intent.order.side,
            price=fill_price,
            n=intent.order.n_at_signal,
            requested_qty=intent.requested_qty,
            equity=equity,
            state=state,
            prices=prices_at_open,
            reservations=reservations.usage_excluding(intent.intent_id),
        )
