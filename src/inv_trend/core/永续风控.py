"""Pure versioned linear-swap risk and isolated-margin state transitions."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PositionTier:
    notional_cap: float
    maintenance_rate: float
    max_leverage: float

    def __post_init__(self):
        if not all(math.isfinite(v) and v > 0 for v in
                   (self.notional_cap, self.maintenance_rate, self.max_leverage)):
            raise ValueError('invalid position tier')


@dataclass(frozen=True)
class InstrumentSpec:
    instrument: str
    instrument_spec_version: str
    position_tier_version: str
    effective_from: str
    effective_to: str | None
    source: str
    spec_status: str
    contract_value: float
    lot_size: float
    minimum_size: float
    exchange_max_leverage: float
    fee_rate: float
    liquidation_fee_rate: float
    tiers: tuple[PositionTier, ...]

    def __post_init__(self):
        if self.spec_status not in {'VERIFIED_HISTORICAL', 'SIMULATED_SPEC'}:
            raise ValueError('spec_status must be explicit')
        if not self.source or not self.instrument_spec_version or not self.position_tier_version:
            raise ValueError('spec lineage is required')
        if not self.tiers or any(a.notional_cap >= b.notional_cap for a, b in zip(self.tiers, self.tiers[1:])):
            raise ValueError('tiers must have strictly increasing caps')
        if not all(math.isfinite(v) and v > 0 for v in (self.contract_value, self.lot_size,
                                                       self.minimum_size, self.exchange_max_leverage)):
            raise ValueError('invalid contract sizing')
        if not all(math.isfinite(v) and v >= 0 for v in (self.fee_rate, self.liquidation_fee_rate)):
            raise ValueError('invalid fees')

    def tier(self, notional):
        for tier in self.tiers:
            if notional <= tier.notional_cap:
                return tier
        raise ValueError('position exceeds known historical tiers')

    def leverage(self, notional):
        return min(3., self.exchange_max_leverage, self.tier(notional).max_leverage)


def specification_at(specs, instrument, timestamp):
    t = pd.Timestamp(timestamp)
    selected = [s for s in specs if s.instrument == instrument and
                pd.Timestamp(s.effective_from) <= t and
                (s.effective_to is None or t < pd.Timestamp(s.effective_to))]
    if len(selected) != 1:
        raise ValueError('missing or overlapping historical specification')
    return selected[0]


@dataclass(frozen=True)
class IsolatedPosition:
    side: int
    contracts: float
    entry_price: float
    margin: float

    def __post_init__(self):
        if self.side not in (-1, 1) or not all(math.isfinite(x) for x in
                (self.contracts, self.entry_price, self.margin)) or self.contracts < 0 or self.entry_price <= 0:
            raise ValueError('invalid isolated position')


class MarginStateMachine:
    """Tier-reduction model. Source/version status is retained on every event.

    Reduction amounts use lower-tier caps. They are a simulation convention,
    not a reconstruction of exchange order-book auctions or ADL.
    """
    def __init__(self, warning_ratio=1.5):
        if not math.isfinite(warning_ratio) or warning_ratio <= 1:
            raise ValueError('warning ratio must exceed liquidation ratio')
        self.warning_ratio = warning_ratio

    @staticmethod
    def ratio(position, mark, spec):
        notional = position.contracts * spec.contract_value * mark
        tier = spec.tier(notional)
        balance = position.margin + position.side * position.contracts * spec.contract_value * (mark - position.entry_price)
        maintenance = notional * (tier.maintenance_rate + spec.liquidation_fee_rate)
        return balance / maintenance if maintenance > 0 else math.inf

    def process(self, position, mark, spec):
        if not math.isfinite(mark) or mark <= 0:
            raise ValueError('invalid mark price')
        events = []

        def emit(state, qty=0., realized=0., fee=0.):
            events.append(dict(state=state, contracts=qty, realized_pnl=realized, fee=fee,
                               mark_price=mark, instrument_spec_version=spec.instrument_spec_version,
                               position_tier_version=spec.position_tier_version,
                               spec_status=spec.spec_status, liquidation_model='TIER_CAP_SIMULATION'))

        if not position.contracts:
            return position, events
        ratio = self.ratio(position, mark, spec)
        if ratio > 1:
            emit('MARGIN_WARNING' if ratio <= self.warning_ratio else 'NORMAL')
            return position, events
        emit('CANCEL_OPEN_ORDERS')
        while position.contracts and self.ratio(position, mark, spec) <= 1:
            notional = position.contracts * spec.contract_value * mark
            tier = spec.tier(notional)
            tier_index = spec.tiers.index(tier)
            # The currently documented rule partially reduces tier >= 3.
            target = (spec.tiers[tier_index - 1].notional_cap / (spec.contract_value * mark)
                      if tier_index >= 2 else 0.)
            target = math.floor(target / spec.lot_size) * spec.lot_size
            if target < spec.minimum_size:
                target = 0.
            qty = position.contracts - target
            pnl = position.side * qty * spec.contract_value * (mark - position.entry_price)
            fee = qty * spec.contract_value * mark * spec.liquidation_fee_rate
            position = replace(position, contracts=target, margin=position.margin + pnl - fee)
            emit('PARTIAL_LIQUIDATION' if target else 'FULL_LIQUIDATION', qty, pnl, fee)
        return position, events


def funding_cashflow(position, rate, mark, spec):
    if not all(math.isfinite(v) for v in (rate, mark)) or mark <= 0:
        raise ValueError('invalid funding event')
    return -position.side * position.contracts * spec.contract_value * mark * rate


def settlement_position(initial_signed_contracts, fills, timestamp, *, sequence=None, tie_policy=None):
    """Actual timestamp/sequence accounting; missing tie order is never inferred.

    `signed_contract_delta` is a real fill, not a desired order quantity.
    Minute OHLC cannot supply these fills; this helper accepts event evidence.
    """
    t = pd.Timestamp(timestamp)
    qty = float(initial_signed_contracts)
    if not math.isfinite(qty):
        raise ValueError('non-finite initial position')
    for fill in fills:
        ft = pd.Timestamp(fill['timestamp'])
        include = ft < t
        if ft == t:
            if sequence is not None and fill.get('sequence') is not None:
                if fill['sequence'] == sequence:
                    raise ValueError('duplicate event sequence')
                include = fill['sequence'] < sequence
            elif tie_policy in ('FILLS_FIRST', 'FUNDING_FIRST'):
                include = tie_policy == 'FILLS_FIRST'
            else:
                raise ValueError('ambiguous funding/fill ordering')
        if include:
            delta = float(fill['signed_contract_delta'])
            if not math.isfinite(delta):
                raise ValueError('non-finite fill quantity')
            qty += delta
    return qty


def stop_loss_value(side, qty, entry, stop, contract_value, roundtrip_cost_rate):
    """Never offset another position's risk with locked-in profit."""
    return qty * contract_value * (max(side * (entry - stop), 0.) +
                                   roundtrip_cost_rate * entry)


def risk_estimates(closes, *, covariance_days=60):
    """Input ends at the signal close; all rolling estimates are backward-looking."""
    returns = closes.pct_change(fill_method=None)
    rolling_vol = returns.rolling(covariance_days).std(ddof=1) * math.sqrt(365.25)
    current = rolling_vol.iloc[-1]
    stress = rolling_vol.iloc[:-1].tail(365).quantile(.95).combine(current, max)
    cov = returns.tail(covariance_days).cov().to_numpy() * 365.25
    if len(returns.dropna()) < 365 + covariance_days or not np.isfinite(cov).all() or not np.isfinite(stress).all():
        raise ValueError('insufficient causal volatility history')
    return cov, stress.to_numpy()


def volatility_scale(weights, covariance, stress_vol, target=.15):
    weights = np.asarray(weights, dtype=float)
    cov, stress = np.asarray(covariance, dtype=float), np.asarray(stress_vol, dtype=float)
    if cov.shape != (len(weights), len(weights)) or stress.shape != weights.shape or not all(
            np.isfinite(x).all() for x in (weights, cov, stress)) or (stress < 0).any():
        raise ValueError('invalid portfolio risk inputs')
    normal = math.sqrt(max(float(weights @ cov @ weights), 0.))
    stressed = float(np.abs(weights) @ stress)
    return min(1., target / normal if normal else 1., target / stressed if stressed else 1.)


def risk_sized_contracts(*, equity, side, price, stop, spec,
                         portfolio_stop_used=0., symbol_stop_used=0.,
                         gross_notional=0., margin_used=0., trade_risk=.005):
    values = (equity, price, stop, portfolio_stop_used, symbol_stop_used, gross_notional, margin_used)
    if not all(math.isfinite(v) for v in values) or min(equity, price, stop) <= 0 or side not in (-1, 1):
        return 0.
    if side * (price - stop) <= 0:
        return 0.
    budget = max(0., min(equity * trade_risk, equity * .02 - portfolio_stop_used,
                         equity * .01 - symbol_stop_used))
    unit_loss = stop_loss_value(side, 1., price, stop, spec.contract_value, 2 * spec.fee_rate)
    unit_notional = price * spec.contract_value
    raw = min(budget / unit_loss, max(0., 3 * equity - gross_notional) / unit_notional,
              spec.tiers[-1].notional_cap / unit_notional)
    # Conservative across tier transitions: never use a smaller-position limit
    # to admit a larger position.
    leverage = spec.leverage(raw * unit_notional)
    raw = min(raw, max(0., equity * .8 - margin_used) /
              (unit_notional * (1 / leverage + spec.fee_rate)))
    qty = math.floor(raw / spec.lot_size) * spec.lot_size
    return qty if qty >= spec.minimum_size else 0.
