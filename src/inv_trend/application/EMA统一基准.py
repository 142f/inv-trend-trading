"""EMA and passive order adapters sharing the maintained Turtle execution engine."""

from dataclasses import asdict, replace
import hashlib
import json

import numpy as np
import pandas as pd

from inv_trend.adapters.multi_asset.backtest.runner import TurtleBacktester
from inv_trend.adapters.multi_asset.models.domain import AssetSpec, Order, TurtleRules
from inv_trend.core.EMA趋势策略 import Config, signals, metrics
from inv_trend.core.执行约束 import ExecutionPolicy

CAPS = dict(
    max_total_leverage=0.9,
    max_direction_leverage=0.9,
    default_cluster_leverage=0.225,
    max_total_1n_risk_pct=1.0,
    max_direction_1n_risk_pct=1.0,
    default_cluster_1n_risk_pct=1.0,
)
INITIAL = 100_000.0


def utc(x):
    return pd.Timestamp(x).tz_localize("UTC") if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x)


def common_specs(symbols):
    return {
        s: AssetSpec(
            symbol=s,
            asset_class="research_proxy",
            cluster=s,
            qty_step=0.000001,
            cost_bps=5.0,
            slippage_bps=5.0,
            max_units=3,
            max_symbol_leverage=0.225,
            max_symbol_1n_risk_pct=1.0,
            can_short=True,
        )
        for s in symbols
    }


def prepare(frames):
    out = {}
    for s, frame in frames.items():
        f = signals(frame, Config())
        for days in (0, 2, 4):
            variant = signals(frame, replace(Config(), confirmation=days))
            for side in (1, -1):
                for j in range(3):
                    f[f"c{days}_{side}_{j}"] = variant[f"state_{side}_{j}"]
        f["bar_open"] = f.timestamp
        f["bar_end"] = pd.to_datetime(f.available_at, utc=True)
        f["ordinal"] = np.arange(len(f))
        out[s] = f.set_index("timestamp")
    return out


class EMAOrders:
    emit_order_trace = True

    def __init__(self, specs, cfg, schedule=None, passive=False):
        self.specs, self.cfg = specs, cfg
        self.schedule = schedule or []
        previous = None
        for point in self.schedule:
            effective = utc(point["effective_at"])
            if utc(point["trained_through"]) >= effective:
                raise ValueError("training must end strictly before policy activation")
            if previous is not None and effective <= previous:
                raise ValueError("policy schedule must be strictly increasing")
            Config(**point["parameters"])
            previous = effective
        self.passive = passive
        self.last_decisions = []

    def generate_orders_for_date(self, when, rows, state, equity, tradable_symbols=None):
        cfg = self.cfg
        selected = "fixed"
        for point in self.schedule:
            if utc(point["effective_at"]) <= when:
                cfg = Config(**point["parameters"])
                selected = point["candidate_id"]
        orders = []
        self.last_decisions = []
        for s in sorted(tradable_symbols or rows):
            row, pos = rows[s], state.positions.get(s)
            price, atr = float(row["close"]), float(row["atr"])
            if int(row["ordinal"]) < 165 or atr <= 0:
                continue
            if self.passive:
                if pos is None:
                    orders.append(
                        Order(
                            s,
                            "open",
                            1,
                            equity * 0.225 / price,
                            "被动持有",
                            "passive",
                            price,
                            atr,
                            metadata={
                                "validate_channel": False,
                                "protective_stop": False,
                                "candidate_id": selected,
                                "trigger": "被动持有初始建仓",
                            },
                        )
                    )
                continue
            desired = {}
            evidence = {}
            for side in (1, -1):
                states = [
                    row[
                        f"state_{side}_{j}"
                        if cfg.confirmation == 3
                        else f"c{cfg.confirmation}_{side}_{j}"
                    ]
                    for j in range(3)
                ]
                evidence[str(side)] = states
                valid = [v in ("确认成功", "持续有效") for v in states]
                weak = (
                    (cfg.quality == "macd" and row["macd"] * side <= 0)
                    or (cfg.quality == "adx" and row["adx"] < 20)
                    or (cfg.quality == "volume" and not row["volume_ratio"] >= 1)
                )
                budget = min(cfg.cap, cfg.risk / (cfg.stop_atr * atr / price)) / len(self.specs)
                budget *= cfg.quality_scale if weak else 1
                budget *= cfg.short_scale if side < 0 else 1
                for j in range(3):
                    if (
                        valid[j]
                        and (cfg.independent or all(valid[: j + 1]))
                        and j not in cfg.disabled
                    ):
                        desired[f"EMA阶段{j + 1}"] = (side, cfg.weights[j] * budget)
            decision_payload = {
                "symbol": s,
                "observed_at": str(row["bar_end"]),
                "candidate_id": selected,
                "ema_states": evidence,
                "desired": desired,
            }
            decision_id = hashlib.sha256(
                json.dumps(
                    decision_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:32]
            self.last_decisions.append(
                dict(
                    decision_id=decision_id,
                    symbol=s,
                    ema_states=evidence,
                    selected=selected,
                    candidate_id=selected,
                    desired=desired,
                    observed_at=str(row["bar_end"]),
                )
            )
            audit_metadata = {
                "decision_id": decision_id,
                "candidate_id": selected,
                "decision_observed_at": str(row["bar_end"]),
                "ema_states": evidence,
                "desired_stages": desired,
            }
            if pos:
                remove = [
                    u.reason
                    for u in pos.units
                    if u.reason not in desired
                    or desired[u.reason][0] != pos.side
                    or desired[u.reason][1] == 0
                ]
                if remove:
                    orders.append(
                        Order(
                            s,
                            "exit",
                            pos.side,
                            pos.total_qty,
                            "EMA关系失效",
                            "ema",
                            price,
                            atr,
                            metadata={**audit_metadata, "unit_reasons": remove},
                        )
                    )
                    continue
                # This stop update occurs after the bar's range has already been processed.
                trail = price - pos.side * cfg.stop_atr * atr
                pos.stop_price = (
                    max(pos.stop_price, trail) if pos.side > 0 else min(pos.stop_price, trail)
                )
            present = {u.reason for u in pos.units} if pos else set()
            for stage, (side, weight) in desired.items():
                if stage in present or weight <= 0 or (pos and pos.side != side):
                    continue
                # Stage fills are sequential, at most one new unit per asset per real bar.
                qty = equity * weight / price
                orders.append(
                    Order(
                        s,
                        "add" if pos else "open",
                        side,
                        qty,
                        stage,
                        "ema",
                        price,
                        cfg.stop_atr * atr / 3.0,
                        risk_1n_pct=qty * (cfg.stop_atr * atr / 3.0) / equity,
                        metadata={
                            **audit_metadata,
                            "validate_channel": False,
                            "entry_stage": stage,
                            "trigger": "EMA关系确认且前缀阶段有效",
                        },
                    )
                )
                break
        return orders


def run(data, cfg, start, end, *, kind="ema", turtle_rules=None, schedule=None):
    # Physical end clipping guards callbacks against access to future bars.
    scoped = {s: f.loc[f.bar_end <= utc(end)].copy() for s, f in data.items()}
    specs = common_specs(scoped)
    rules = TurtleRules(
        **({**(turtle_rules or {}), **CAPS} if kind == "turtle" else {**CAPS, "stop_n": 3.0})
    )
    adapter = None if kind == "turtle" else EMAOrders(specs, cfg, schedule, kind == "passive")
    runner = TurtleBacktester(
        scoped,
        specs,
        rules,
        initial_equity=INITIAL,
        liquidate_at_end=False,
        cash_model="derivative",
        strategy=adapter,
        evaluation_start=utc(start),
        evaluation_end=utc(end) - pd.Timedelta(nanoseconds=1),
        signal_delay_bars=1,
        audit_execution=True,
        execution_policy=ExecutionPolicy(
            event_clock=True,
            price_slippage=True,
            max_gross_leverage=0.9,
            financing_bps_per_year=0.0,
            short_borrow_bps_per_year=300.0,
            enforce_capital=True,
            enforce_volume=False,
            certified=False,
        ),
    )
    result = runner.run()
    return summarize(result, start, end), result


def summarize(result, start, end):
    dates = pd.date_range(utc(start), utc(end), freq="D")
    equity = result.equity_curve.groupby(level=0).last().reindex(dates).ffill().fillna(INITIAL)
    # End includes the terminal close at the exclusive next-day boundary.
    returns = equity.pct_change().iloc[1:]
    units = result.trade_details
    trades = pd.DataFrame(columns=["pnl", "side", "stage"])
    if not units.empty:
        trades = units[["pnl", "side"]].copy()
        trades["pnl"] /= INITIAL
        trades["stage"] = units.entry_reason.str.extract(r"EMA阶段(\d)")[0].fillna(0).astype(int)
    m = metrics(returns, trades)
    open_units = result.open_trade_details.copy()
    if not open_units.empty:
        open_units["stage"] = (
            open_units.entry_reason.str.extract(r"EMA阶段(\d)")[0].fillna(0).astype(int)
        )
        open_units["pnl"] /= INITIAL
        m["long_pnl"] += float(open_units.loc[open_units.side == 1, "pnl"].sum())
        m["short_pnl"] += float(open_units.loc[open_units.side == -1, "pnl"].sum())
        for j in (1, 2, 3):
            m["stages"][str(j)] += float(open_units.loc[open_units.stage == j, "pnl"].sum())
    open_other = (
        float(open_units.loc[open_units.stage == 0, "pnl"].sum()) if not open_units.empty else 0.0
    )
    led = result.cash_ledger.set_index("time")
    expo = led.gross_leverage.groupby(level=0).last().reindex(dates).ffill().fillna(0)
    m.update(
        utilization=float(expo.iloc[1:].mean()),
        peak_exposure=float(expo.max()),
        ending_equity=float(equity.iloc[-1]),
        initial_equity=INITIAL,
        fees=float(led.fee_cost_cumulative.iloc[-1]),
        slippage=float(led.slippage_cost_cumulative.iloc[-1]),
        carry=result.carry_cost,
        stages={
            **m["stages"],
            "other": float(trades.loc[trades.stage == 0, "pnl"].sum()) + open_other,
        },
        open_units=len(open_units),
        open_net_pnl=float(open_units.pnl.sum()) if not open_units.empty else 0.0,
    )
    if abs(trades.pnl.sum() + m["open_net_pnl"] - m["total"]) > 1e-7:
        raise AssertionError("trade/terminal NAV reconciliation failed")
    return dict(metrics=m, returns=returns, equity=equity)


def score(m):
    return m["sharpe"] + 0.25 * m["calmar"] - 2 * m["drawdown"]


def choose_training(records):
    """Only physically clipped inner validation summaries accepted; OOS has no entry point."""
    if any(r.get("scope") != "INNER_VALIDATION" for r in records):
        raise ValueError("selection requires INNER_VALIDATION records")
    ranked = sorted(records, key=lambda x: (-score(x["metrics"]), x["candidate_id"]))
    return ranked[0]["candidate_id"]


def config_dict(cfg):
    return asdict(cfg)
