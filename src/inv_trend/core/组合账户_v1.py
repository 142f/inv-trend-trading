"""One fully funded multi-asset ledger. Execution only receives current opens."""
from dataclasses import asdict, dataclass
import math

import numpy as np

from .四资产合同_v1 import CORE, utc
from .阶段协议_v1 import digest


@dataclass(frozen=True)
class CostModel:
    fee_bps: float = 10
    slippage_bps: float = 10
    carry_bps_year: float = 100
    delay_bars: int = 1
    capacity_equity: float = 1.
    version: str = "research-cost-v1"

    def __post_init__(self):
        for name in ("fee_bps", "slippage_bps", "carry_bps_year"):
            if not math.isfinite(getattr(self, name)) or not 0 <= getattr(self, name) < 10000:
                raise ValueError("invalid execution costs")
        if type(self.delay_bars) is not int or self.delay_bars < 1:
            raise ValueError("next-bar execution required")
        if not math.isfinite(self.capacity_equity) or not 0 < self.capacity_equity <= 1:
            raise ValueError("invalid research capacity")


class PortfolioAccount:
    def __init__(self, contracts, *, initial_cash=100000., costs=None, mode="FORMAL"):
        if set(contracts) != set(CORE):
            raise ValueError("four core contracts required")
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("positive initial capital required")
        self.contracts, self.mode = contracts, mode
        self.costs = costs or CostModel()
        self.initial_cash = self.cash = float(initial_cash)
        self.positions = {s: 0. for s in CORE}
        self.basis = {s: 0. for s in CORE}
        self.marks = {s: 0. for s in CORE}
        self.orders, self.fills = [], []
        self.fees = self.funding = self.slippage = self.realized_pnl = 0.
        self.step = {s: 0 for s in CORE}
        self.last_open = self.last_close = None
        self.capacity_cash = initial_cash * self.costs.capacity_equity
        self.turnover_notional = 0.

    @property
    def equity(self):
        return self.cash + sum(self.positions[s] * self.marks[s] *
                               self.contracts[s].contract_multiplier for s in CORE)

    @property
    def margin_used(self):
        return self.equity - self.cash

    @property
    def available_cash(self):
        return self.cash

    @property
    def free_margin(self):
        return self.cash

    @property
    def unrealized_pnl(self):
        return sum(self.positions[s] * (self.marks[s] - self.basis[s]) *
                   self.contracts[s].contract_multiplier for s in CORE)

    def submit(self, targets, at):
        if self.last_close is None or utc(at) != utc(self.last_close):
            raise ValueError("orders require completed known close")
        if set(targets) != set(CORE) or any(not math.isfinite(w) or w < 0 for w in targets.values()):
            raise ValueError("invalid target weights")
        if sum(targets.values()) > 1 + 1e-9:
            raise ValueError("fully funded account leverage cap")
        for s in CORE:
            if self.marks[s] <= 0:
                if targets[s] > 0:
                    raise ValueError("unknown valuation price")
                continue
            target = targets[s] * self.equity / (self.marks[s] * self.contracts[s].contract_multiplier)
            # Supersede stale desired targets; preserve full audit history.
            for old in self.orders:
                if old["symbol"] == s and old["status"] == "PENDING":
                    old["status"] = "SUPERSEDED"
            order = dict(symbol=s, signal_at=at, target_quantity=target,
                         due_step=self.step[s] + self.costs.delay_bars, status="PENDING")
            order["order_id"] = digest({**order, "ordinal": len(self.orders)})
            self.orders.append(order)

    def execute_open(self, at, opens):
        if self.last_open and utc(at) <= utc(self.last_open):
            raise ValueError("non-monotonic execution clock")
        if self.last_close and utc(at) < utc(self.last_close):
            raise ValueError("open precedes known close")
        if set(opens) - set(CORE):
            raise ValueError("unknown execution instrument")
        for s, price in opens.items():
            self.contracts[s].validate(at, self.mode)
            if not math.isfinite(price) or price <= 0:
                raise ValueError("invalid open")
        for s, price in opens.items():
            self.step[s] += 1
            self.marks[s] = price
        eligible = [o for o in self.orders if o["status"] == "PENDING" and
                    o["symbol"] in opens and o["due_step"] <= self.step[o["symbol"]]]
        # Reductions release the SAME pool before increases. Deterministic symbol ordering.
        eligible.sort(key=lambda o: (o["target_quantity"] > self.positions[o["symbol"]],
                                     o["symbol"], o["order_id"]))
        capacity = {s: self.capacity_cash for s in opens}
        for order in eligible:
            if utc(order["signal_at"]) > utc(at):
                raise ValueError("future signal invisible")
            s = order["symbol"]
            spec = self.contracts[s]
            delta = order["target_quantity"] - self.positions[s]
            side = 1 if delta > 0 else -1
            base = opens[s]
            raw = base * (1 + side * self.costs.slippage_bps / 10000)
            # Round against the trader, then account for the full price impact once.
            price = (math.ceil(raw / spec.price_tick) if side > 0 else
                     math.floor(raw / spec.price_tick)) * spec.price_tick
            unit = price * spec.contract_multiplier
            requested = abs(delta)
            qty = min(requested, capacity[s] / unit)
            qty = min(qty, self.cash / (unit * (1 + self.costs.fee_bps / 10000))) if side > 0 else min(qty, self.positions[s])
            qty = math.floor(qty / spec.quantity_step) * spec.quantity_step
            if qty < spec.min_quantity:
                qty = 0.
            fee = qty * unit * self.costs.fee_bps / 10000
            slip = qty * abs(price - base) * spec.contract_multiplier
            before = self.positions[s]
            if side > 0 and qty:
                self.basis[s] = (before * self.basis[s] + qty * price) / (before + qty)
            if side < 0:
                self.realized_pnl += qty * (price - self.basis[s]) * spec.contract_multiplier
            self.positions[s] += side * qty
            self.cash -= side * qty * unit + fee
            self.fees += fee
            self.slippage += slip
            self.turnover_notional += qty * unit
            capacity[s] -= qty * unit
            order["status"] = ("FILLED" if requested - qty < spec.quantity_step else
                               "PARTIAL" if qty else "REJECTED")
            self.fills.append(dict(order_id=order["order_id"], symbol=s,
                signal_at=order["signal_at"], filled_at=at, quantity=side * qty,
                requested=side * requested, price=price, fee=fee, slippage=slip,
                status=order["status"], reason="remainder expires; shared_cash/prior_equity_capacity/lot",
                contract_version=spec.version, cost_version=self.costs.version))
        self.last_open = at
        self.check()

    def mark_close(self, at, closes):
        if self.last_close and utc(at) <= utc(self.last_close):
            raise ValueError("non-monotonic valuation clock")
        if not self.last_open or utc(at) <= utc(self.last_open):
            raise ValueError("close must follow open")
        if any(s not in CORE or not math.isfinite(p) or p <= 0 for s, p in closes.items()):
            raise ValueError("invalid close")
        # Carry bases only on previously observed marks and calendar time, never future rates.
        days = (utc(at) - utc(self.last_close)).total_seconds() / 86400 if self.last_close else 0
        charge = max(0., self.margin_used) * self.costs.carry_bps_year / 10000 * days / 365.25
        if charge > self.cash + 1e-8:
            raise ValueError("insufficient cash for carry; account requires risk reduction")
        self.cash -= charge
        self.funding += charge
        self.marks.update(closes)
        self.last_close = at
        self.capacity_cash = self.equity * self.costs.capacity_equity
        self.check()
        return self.snapshot()

    def check(self):
        if self.cash < -1e-7 or any(q < -1e-8 for q in self.positions.values()):
            raise ArithmeticError("Accounting mismatch: negative cash/position")
        expected = self.initial_cash + self.realized_pnl + self.unrealized_pnl - self.fees - self.funding
        if not np.isclose(self.equity, expected, rtol=1e-9, atol=1e-6):
            raise ArithmeticError("Accounting mismatch: conservation identity")

    def snapshot(self):
        return dict(timestamp=self.last_close, cash=self.cash, equity=self.equity,
                    available_cash=self.available_cash, margin_used=self.margin_used,
                    free_margin=self.free_margin, unrealized_pnl=self.unrealized_pnl,
                    realized_pnl=self.realized_pnl, positions=dict(self.positions),
                    fees=self.fees, funding=self.funding, slippage=self.slippage,
                    gross_exposure=self.margin_used / self.equity,
                    net_exposure=self.margin_used / self.equity,
                    turnover_notional=self.turnover_notional)

    def checkpoint(self):
        state = {k: v for k, v in vars(self).items() if k not in ("contracts", "costs")}
        state = {**state, "costs": asdict(self.costs),
                 "contracts": {s: asdict(c) for s, c in self.contracts.items()}}
        # Canonical round trip removes mutable aliasing.
        import json
        state = json.loads(json.dumps(state, allow_nan=False))
        return {"state": state, "hash": digest(state)}

    def restore(self, checkpoint):
        import copy
        state = copy.deepcopy(checkpoint["state"])
        if digest(state) != checkpoint["hash"] or state.pop("contracts") != {
                s: asdict(c) for s, c in self.contracts.items()} or state.pop("costs") != asdict(self.costs):
            raise ValueError("Checkpoint invalid")
        if state["mode"] != self.mode or state["initial_cash"] != self.initial_cash:
            raise ValueError("Checkpoint context mismatch")
        self.__dict__.update(state)
        self.check()
