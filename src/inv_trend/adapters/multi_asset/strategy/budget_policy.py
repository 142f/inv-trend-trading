"""One risk-budget calculation for signal admission and fill-time revalidation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from ..models.domain import LONG, AssetSpec, PortfolioState, TurtleRules
from ..models.order_intent import ReservationUsage


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    approved_qty: float
    reason: str = ""


class PortfolioBudgetPolicy:
    def __init__(self, rules: TurtleRules, specs: Mapping[str, AssetSpec]) -> None:
        self.rules = rules
        self.specs = dict(specs)

    def evaluate(
        self,
        *,
        symbol: str,
        side: int,
        price: float,
        n: float,
        requested_qty: float,
        equity: float,
        state: PortfolioState,
        prices: Mapping[str, float],
        reservations: ReservationUsage = ReservationUsage(),
    ) -> BudgetDecision:
        if equity <= 0 or n <= 0 or price <= 0 or requested_qty <= 0:
            return BudgetDecision(False, 0.0, "invalid_risk_inputs")
        spec = self.specs[symbol]
        if not all(math.isfinite(v) for v in (price, n, requested_qty, equity)):
            return BudgetDecision(False, 0.0, "non_finite_risk_inputs")
        usage = _position_usage(state, self.specs, prices, equity)
        risk_per_qty = n * spec.point_value / equity
        notional_per_qty = price * spec.point_value / equity
        direction = "long" if side == LONG else "short"
        cluster = spec.cluster
        risk_limits = [
            self.rules.max_total_1n_risk_pct - usage["total_risk"] - reservations.risk_total,
            self.rules.max_direction_1n_risk_pct - usage[f"{direction}_risk"] - (reservations.risk_long if side == LONG else reservations.risk_short),
            self.rules.cluster_1n_risk_pct.get(cluster, self.rules.default_cluster_1n_risk_pct) - usage["cluster_risk"].get(cluster, 0.0),
            spec.max_symbol_1n_risk_pct - usage["symbol_risk"].get(symbol, 0.0) - reservations.by_symbol.get(symbol, 0.0),
        ]
        leverage_limits = [
            self.rules.max_total_leverage - usage["total_leverage"] - reservations.notional_total,
            self.rules.max_direction_leverage - usage[f"{direction}_leverage"],
            self.rules.cluster_leverage.get(cluster, self.rules.default_cluster_leverage) - usage["cluster_leverage"].get(cluster, 0.0),
            spec.max_symbol_leverage - usage["symbol_leverage"].get(symbol, 0.0),
        ]
        risk_capacity = min(risk_limits)
        leverage_capacity = min(leverage_limits)
        if risk_capacity <= 0 or leverage_capacity <= 0:
            return BudgetDecision(False, 0.0, "risk_budget_exhausted")
        # Capacity is divided by per-unit impact.  Ratios of existing totals are
        # deliberately never used: that was the source of prior scaling errors.
        quantity = min(requested_qty, risk_capacity / risk_per_qty, leverage_capacity / notional_per_qty)
        quantity = math.floor(quantity / spec.qty_step) * spec.qty_step
        if (
            quantity <= 0
            or quantity < spec.min_qty
            or quantity * price * spec.point_value < spec.min_notional
        ):
            return BudgetDecision(False, 0.0, "minimum_size_not_met")
        return BudgetDecision(True, quantity, "scaled" if quantity < requested_qty else "approved")


def _position_usage(
    state: PortfolioState, specs: Mapping[str, AssetSpec], prices: Mapping[str, float], equity: float
) -> dict[str, object]:
    result: dict[str, object] = {
        "total_risk": 0.0, "long_risk": 0.0, "short_risk": 0.0,
        "total_leverage": 0.0, "long_leverage": 0.0, "short_leverage": 0.0,
        "cluster_risk": {}, "symbol_risk": {}, "cluster_leverage": {}, "symbol_leverage": {},
    }
    for symbol, position in state.positions.items():
        spec = specs.get(symbol)
        price = prices.get(symbol)
        if spec is None or price is None or price <= 0:
            continue
        risk = position.one_n_risk_value(spec.point_value) / equity
        leverage = abs(position.total_qty * price * spec.point_value) / equity
        direction = "long" if position.side == LONG else "short"
        result["total_risk"] += risk
        result[f"{direction}_risk"] += risk
        result["total_leverage"] += leverage
        result[f"{direction}_leverage"] += leverage
        for kind, value in (("risk", risk), ("leverage", leverage)):
            cluster_values = result[f"cluster_{kind}"]
            symbol_values = result[f"symbol_{kind}"]
            cluster_values[spec.cluster] = cluster_values.get(spec.cluster, 0.0) + value
            symbol_values[symbol] = symbol_values.get(symbol, 0.0) + value
    return result
