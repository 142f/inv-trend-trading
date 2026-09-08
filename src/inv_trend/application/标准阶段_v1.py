"""Seven composable run(input)->output stages; state is explicit and checkpointable."""
from __future__ import annotations

from dataclasses import asdict
import math

from inv_trend.core.阶段协议_v1 import (
    AccountSnapshot, Bar, Envelope, Evaluation, FeatureFrame, Quality, RawBar, Report,
    RunContext, Signal, StageFailure, digest, utc_now, emit, timestamp,
)
from inv_trend.core.在线策略_v1 import Candidate, OnlineFeatures, OnlineStrategy
from inv_trend.core.事件账户_v1 import CashBroker, ExecutionConfig


class StageBase:
    name = ""
    input_type = object
    previous_stage = ""

    def check(self, packet: Envelope) -> None:
        packet.validate()
        if type(packet.payload) is not self.input_type or packet.stage != self.previous_stage:
            raise ValueError(f"{self.name} requires {self.previous_stage}/{self.input_type.__name__}")
        context = getattr(self, "_context", None)
        if context is not None and context != packet.context:
            raise ValueError("stage instance cannot mix run contexts")
        self._context = packet.context

    def snapshot(self) -> dict:
        return {"stage": self.name, "context": asdict(self._context)
                if getattr(self, "_context", None) else None}

    def restore(self, state: dict) -> None:
        if state["stage"] != self.name:
            raise ValueError("stage checkpoint mismatch")
        self._context = RunContext(**state["context"]) if state["context"] else None


class AcquisitionStage:
    name = "acquire"

    def __init__(self, context: RunContext):
        self.context = context

    def run(self, raw: RawBar) -> Envelope:
        if type(raw) is not RawBar:
            raise TypeError("acquisition accepts a typed RawBar from the repository adapter")
        return emit(self.context, self.name, raw, raw.available_at,
                    quality=Quality("WARN", ("source_record_received",), (),
                                    ("source is RESEARCH_ONLY; session/adjustments not certified",)))

    def snapshot(self) -> dict:
        return {"stage": self.name, "context": asdict(self.context)}

    def restore(self, state: dict) -> None:
        if state["stage"] != self.name or RunContext(**state["context"]) != self.context:
            raise ValueError("acquisition checkpoint mismatch")


class ProcessingStage(StageBase):
    name, previous_stage, input_type = "process", "acquire", RawBar

    def __init__(self):
        self.last_time = None
        self.symbol = None

    def run(self, packet: Envelope) -> Envelope:
        self.check(packet)
        bar = Bar(**asdict(packet.payload))
        if self.last_time is not None and (bar.available_at <= self.last_time or bar.symbol != self.symbol):
            raise ValueError("one stage stream requires one symbol and strict time ordering")
        self.last_time, self.symbol = bar.available_at, bar.symbol
        return emit(packet.context, self.name, bar, bar.available_at, parent=packet,
                    quality=Quality("WARN", ("finite_ohlcv", "utc", "strict_order", "ohlc_bounds"),
                                    (), packet.quality.warnings))

    def snapshot(self) -> dict:
        return {**super().snapshot(), "last_time": self.last_time, "symbol": self.symbol}

    def restore(self, state: dict) -> None:
        super().restore(state)
        self.last_time, self.symbol = state["last_time"], state["symbol"]


class IndicatorStage(StageBase):
    name, previous_stage, input_type = "indicators", "process", Bar

    def __init__(self, candidate: Candidate):
        self.engine = OnlineFeatures(candidate)

    def run(self, packet: Envelope) -> Envelope:
        self.check(packet)
        features = self.engine.update(packet.payload)
        return emit(packet.context, self.name, features, packet.as_of, parent=packet,
                    quality=Quality("PASS" if features.ready else "WARN", ("causal_window",), (),
                                    () if features.ready else ("warmup_incomplete",)))

    def snapshot(self) -> dict:
        return {**super().snapshot(), "engine": self.engine.snapshot()}

    def restore(self, state: dict) -> None:
        super().restore(state)
        self.engine.restore(state["engine"])


class SignalStage(StageBase):
    name, previous_stage, input_type = "signal", "indicators", FeatureFrame

    def __init__(self, candidate: Candidate):
        self.engine = OnlineStrategy(candidate)

    def run(self, packet: Envelope) -> Envelope:
        self.check(packet)
        value = self.engine.update(packet.payload)
        return emit(packet.context, self.name, value, packet.as_of, parent=packet,
                    quality=Quality("WARN" if value.direction < 0 else "PASS", ("causal_signal",), (),
                                    ("cash execution contract cannot borrow; short target becomes cash",)
                                    if value.direction < 0 else ()))

    def snapshot(self) -> dict:
        return {**super().snapshot(), "engine": self.engine.snapshot()}

    def restore(self, state: dict) -> None:
        super().restore(state)
        self.engine.restore(state["engine"])


class BacktestStage(StageBase):
    name, previous_stage, input_type = "backtest", "signal", Signal

    def __init__(self, execution: ExecutionConfig):
        self.engine = CashBroker(execution)

    def run(self, packet: Envelope) -> Envelope:
        self.check(packet)
        value = self.engine.on_signal(packet.payload)
        return emit(packet.context, self.name, value, packet.as_of, parent=packet,
                    quality=Quality("PASS", ("cash_nonnegative", "long_only", "next_open",
                                             "lagged_liquidity", "fee_aware_capital")))

    def snapshot(self) -> dict:
        return {**super().snapshot(), "engine": self.engine.snapshot()}

    def restore(self, state: dict) -> None:
        super().restore(state)
        self.engine.restore(state["engine"])


class EvaluationStage(StageBase):
    name, previous_stage, input_type = "evaluate", "backtest", AccountSnapshot

    def __init__(self, initial_equity=None, initial_fees=0.0, initial_slippage=0.0):
        self.initial_equity = initial_equity
        self.initial_fees = initial_fees
        self.initial_slippage = initial_slippage
        self.previous_equity = None
        self.peak = None
        self.n = 0
        self.average = 0.0
        self.m2 = 0.0
        self.drawdown = 0.0
        self.fills = 0
        self.first_time = None
        self.last_time = None

    def run(self, packet: Envelope) -> Envelope:
        self.check(packet)
        s = packet.payload
        if self.last_time is not None and s.as_of <= self.last_time:
            raise ValueError("evaluation cannot consume the same/before time twice")
        if self.previous_equity is None:
            self.initial_equity = self.initial_equity or s.initial_cash
            self.previous_equity = self.initial_equity
            self.peak = self.initial_equity
            self.first_time = s.as_of
        ret = s.equity / self.previous_equity - 1
        self.n += 1
        delta = ret - self.average
        self.average += delta / self.n
        self.m2 += delta * (ret - self.average)
        self.peak = max(self.peak, s.equity)
        self.drawdown = max(self.drawdown, 1 - s.equity / self.peak)
        self.fills += sum(abs(f.quantity) > 0 for f in s.fills)
        sd = math.sqrt(max(0, self.m2 / (self.n - 1))) if self.n > 1 else 0.0
        years = self.n / 252  # same D1_CLOSE_252 basis as the research summary
        metrics = {"total_return": s.equity / self.initial_equity - 1,
                   "annualized_return": (s.equity / self.initial_equity) ** (1 / years) - 1
                   if s.equity > 0 else -1.0,
                   "max_drawdown": self.drawdown,
                   "sharpe_ratio": self.average / sd * math.sqrt(252) if sd > 1e-14 else None,
                   "fill_count": self.fills, "fees": s.fees - self.initial_fees,
                   "slippage_cost": s.slippage_cost - self.initial_slippage,
                   "terminal_equity": s.equity, "open_quantity": s.quantity,
                   "pending_orders": s.pending_orders, "bars": self.n}
        self.previous_equity, self.last_time = s.equity, s.as_of
        value = Evaluation(s.as_of, tuple(metrics.items()), "RESEARCH_ONLY")
        return emit(packet.context, self.name, value, packet.as_of, parent=packet,
                    quality=Quality("WARN", ("net_equity", "streaming_moments",), (),
                                    ("risk uses D1 close marks/252 bars; no live certification",)))

    def snapshot(self) -> dict:
        keys = ("initial_equity", "initial_fees", "initial_slippage", "previous_equity", "peak", "n", "average", "m2", "drawdown", "fills",
                "first_time", "last_time")
        return {**super().snapshot(), **{k: getattr(self, k) for k in keys}}

    def restore(self, state: dict) -> None:
        super().restore(state)
        for key in ("initial_equity", "initial_fees", "initial_slippage", "previous_equity", "peak", "n", "average", "m2", "drawdown", "fills",
                    "first_time", "last_time"):
            setattr(self, key, state[key])


class ReportStage(StageBase):
    name, previous_stage, input_type = "report", "evaluate", Evaluation

    def run(self, packet: Envelope) -> Envelope:
        self.check(packet)
        value = Report(packet.as_of, "逐时点研究报告", packet.payload.metrics,
                       packet.payload.quality_gate, False)
        return emit(packet.context, self.name, value, packet.as_of, parent=packet,
                    quality=packet.quality)


def stages(context: RunContext, candidate: Candidate, execution: ExecutionConfig) -> tuple:
    return (AcquisitionStage(context), ProcessingStage(), IndicatorStage(candidate),
            SignalStage(candidate), BacktestStage(execution), EvaluationStage(), ReportStage())


def run_checked(stage, input):
    """External stage boundary: structured FAIL output, rollback state, no success-shaped errors.

    The fast composed driver fails the entire window on exceptions. Standalone CLI
    execution uses this checked boundary and persists failures in the same repository.
    """
    state = stage.snapshot()
    try:
        return stage.run(input)
    except Exception as exc:
        stage.restore(state)
        ctx = input.context if isinstance(input, Envelope) else stage.context
        as_of = input.as_of if isinstance(input, Envelope) else getattr(input, "available_at", utc_now())
        try:
            timestamp(as_of)
        except (TypeError, ValueError):
            as_of = utc_now()
        failure = StageFailure(type(exc).__name__, str(exc))
        result = Envelope(ctx, stage.name, "1.0", failure, utc_now(), as_of,
                          input.output_hash if isinstance(input, Envelope) else "",
                          digest(failure), (input.artifact_id,) if isinstance(input, Envelope) else (),
                          Quality("FAIL", (), (f"{type(exc).__name__}: {exc}",), ()))
        result.validate(allow_failed=True)
        return result
