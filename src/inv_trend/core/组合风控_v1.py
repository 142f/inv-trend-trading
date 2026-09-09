"""Explicit portfolio Stage schema; marginal risk budgets measured in annual volatility."""
from dataclasses import asdict, dataclass
import math

import numpy as np

from .四资产合同_v1 import utc
from .阶段协议_v1 import digest


@dataclass(frozen=True)
class RiskConfig:
    method: str = "cluster"
    portfolio_target_vol: float = .12
    min_vol_floor: float = .04
    max_leverage: float = .95
    max_gross_exposure: float = .95
    single_asset_max_weight: float = .45
    single_asset_max_risk: float = .60
    crypto_cluster_max_risk: float = .65
    metals_cluster_max_risk: float = .65
    crypto_budget: float = .5
    lookback: int = 60
    rebalance_frequency: int = 7

    def __post_init__(self):
        if self.method not in ("equal", "inverse_vol", "cluster"):
            raise ValueError("invalid allocation method")
        for k, v in asdict(self).items():
            if k != "method" and (not math.isfinite(v) or v <= 0):
                raise ValueError("invalid risk parameter")
        if not 0 < self.crypto_budget < 1:
            raise ValueError("both clusters require positive budget")
        if self.max_leverage > 1 or self.max_gross_exposure > 1:
            raise ValueError("research funded model cannot borrow")
        if type(self.lookback) is not int or self.lookback < 3 or type(self.rebalance_frequency) is not int:
            raise ValueError("integer lookback/rebalance required")


@dataclass(frozen=True)
class PortfolioInput:
    run_id: str
    timestamp: str
    asset_signals: tuple
    asset_volatility: tuple
    correlation_matrix: tuple
    current_positions: tuple
    cash: float
    equity: float
    risk_budget: tuple
    portfolio_constraints: RiskConfig
    known_before: str
    lineage: tuple


@dataclass(frozen=True)
class PortfolioOutput:
    target_weights: tuple
    target_exposures: tuple
    risk_contributions: tuple
    cluster_exposures: tuple
    portfolio_target_volatility: float
    constraint_results: tuple
    quality: str
    lineage: tuple


def risk_contributions(weights, cov):
    vol = math.sqrt(max(0., float(weights @ cov @ weights)))
    rc = weights * (cov @ weights) / vol if vol > 1e-12 else np.zeros(len(weights))
    return vol, rc


def risk_budget_weights(cov, budget):
    # Convex long-only risk budgeting: coordinate minimization of
    # 1/2 x'Cov x - sum(b_i log(x_i)); supports negative correlations.
    x = 1 / np.sqrt(np.maximum(np.diag(cov), 1e-8))
    for _ in range(40):
        old = x.copy()
        for i in range(len(x)):
            a = cov[i, i]
            b = cov[i] @ x - a * x[i]
            x[i] = 2 * budget[i] / (math.sqrt(b * b + 4 * a * budget[i]) + b) if b >= 0 else (
                math.sqrt(b * b + 4 * a * budget[i]) - b) / (2 * a)
        if np.max(abs(x - old)) < 1e-7:
            break
    return x / x.sum()


class PortfolioAllocationStage:
    def run(self, packet: PortfolioInput, *, core_fraction=0.):
        c = packet.portfolio_constraints
        if not packet.run_id or not packet.lineage or utc(packet.known_before) >= utc(packet.timestamp):
            raise ValueError("future volatility/correlation invisible; strict prior watermark required")
        if not 0 <= core_fraction <= 1 or packet.equity <= 0:
            raise ValueError("invalid core split/equity")
        signals = np.asarray(packet.asset_signals, float)
        vol = np.maximum(np.asarray(packet.asset_volatility, float), c.min_vol_floor)
        corr = np.asarray(packet.correlation_matrix, float)
        if signals.shape != (4,) or vol.shape != (4,) or corr.shape != (4, 4):
            raise ValueError("four asset schema mismatch")
        if not all(np.isfinite(a).all() for a in (signals, vol, corr)) or (signals < 0).any() or (signals > 1).any():
            raise ValueError("invalid risk inputs")
        if not np.allclose(corr, corr.T) or not np.allclose(np.diag(corr), 1) or np.linalg.eigvalsh(corr).min() < -1e-8:
            raise ValueError("correlation matrix must be PSD with unit diagonal")
        cov = corr * np.outer(vol, vol) + np.eye(4) * 1e-10
        budget = np.asarray(packet.risk_budget, float)
        if budget.shape != (4,) or not np.isfinite(budget).all() or (budget <= 0).any() or not np.isclose(budget.sum(), 1):
            raise ValueError("invalid risk budget")
        if c.method == "equal":
            base = np.ones(4) / 4
        elif c.method == "inverse_vol":
            base = (1 / vol) / (1 / vol).sum()
        else:
            base = risk_budget_weights(cov, budget)
        tactical = base * signals
        # Combine unit-risk sleeves, NOT fixed cash fractions.
        def unit_risk(w):
            sigma, _ = risk_contributions(w, cov)
            return w / max(sigma, c.min_vol_floor)
        w = (core_fraction * unit_risk(base) +
             (1 - core_fraction) * unit_risk(tactical)) * c.portfolio_target_vol
        sigma, _ = risk_contributions(w, cov)
        if sigma > c.portfolio_target_vol:
            w *= c.portfolio_target_vol / sigma
        w = np.minimum(w, c.single_asset_max_weight)
        if w.sum() > min(c.max_gross_exposure, c.max_leverage):
            w *= min(c.max_gross_exposure, c.max_leverage) / w.sum()
        _, rc = risk_contributions(w, cov)
        # Caps are absolute contributions as fractions of TARGET risk, not a promise
        # about fractions of realized total risk (which uniform deleveraging cannot alter).
        limits = np.array([c.single_asset_max_risk] * 4 +
                          [c.crypto_cluster_max_risk, c.metals_cluster_max_risk]) * c.portfolio_target_vol
        values = np.r_[abs(rc), abs(rc[:2]).sum(), abs(rc[2:]).sum()]
        scale = min(1., float(np.min(limits / np.maximum(values, 1e-12))))
        w *= scale
        sigma, rc = risk_contributions(w, cov)
        constraints = (("gross_leverage", bool(w.sum() <= min(c.max_gross_exposure, c.max_leverage) + 1e-8)),
                       ("asset_weight", bool(w.max() <= c.single_asset_max_weight + 1e-8)),
                       ("target_vol", bool(sigma <= c.portfolio_target_vol + 1e-8)),
                       ("risk_caps", bool((values * scale <= limits + 1e-8).all())))
        if not all(v for _, v in constraints):
            raise ArithmeticError("risk constraint failure")
        return PortfolioOutput(tuple(w), tuple(w * packet.equity), tuple(rc),
                               (float(w[:2].sum()), float(w[2:].sum())), c.portfolio_target_vol,
                               constraints, "RESEARCH_ASSUMPTION", packet.lineage +
                               (("allocation_hash", digest(asdict(packet))),))
