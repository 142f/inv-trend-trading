"""Three independent rule families, bounded online features, deterministic grid."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math
from statistics import mean, pstdev

from .阶段协议_v1 import Bar, FeatureFrame, Signal, digest


@dataclass(frozen=True, slots=True)
class Candidate:
    family: str
    lookback: int
    threshold: float = 0.0
    exit_lookback: int = 10
    max_weight: float = 0.95
    target_volatility: float = 0.15

    def __post_init__(self) -> None:
        if self.family not in ("momentum", "breakout", "reversion", "cash", "buy_hold"):
            raise ValueError("unknown strategy family")
        if type(self.lookback) is not int or self.lookback < 2:
            raise ValueError("lookback must be an integer >=2")
        if type(self.exit_lookback) is not int or not 1 <= self.exit_lookback <= self.lookback:
            raise ValueError("invalid exit window")
        for name in ("threshold", "max_weight", "target_volatility"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"invalid {name}")
        if not 0 < self.max_weight <= 1 or not 0 < self.target_volatility <= 1:
            raise ValueError("weight/volatility must lie in (0,1]")
        if self.family == "reversion" and self.threshold <= 0.2:
            raise ValueError("reversion entry must exceed fixed exit threshold 0.2")

    @property
    def id(self) -> str:
        return self.family + "-" + digest(self)[:12]


class OnlineFeatures:
    """Only already-completed bars; channel/z-score use the PRIOR lookback window."""
    def __init__(self, candidate: Candidate):
        self.candidate = candidate
        self.bars: deque[Bar] = deque(maxlen=max(candidate.lookback + 1, 22))
        self.previous_time: str | None = None

    def update(self, bar: Bar, *, trace: bool = True) -> FeatureFrame:
        if self.previous_time is not None and bar.available_at <= self.previous_time:
            raise ValueError("duplicate/out-of-order completed bar")
        c = self.candidate
        ready = len(self.bars) >= max(c.lookback, 21)
        values: dict[str, float | None] = {
            "momentum": None, "channel_high": None, "channel_low": None,
            "exit_high": None, "exit_low": None, "zscore": None, "volatility": None}
        if ready:
            history = list(self.bars)
            recent = history[-c.lookback:]
            closes = [x.close for x in recent]
            sigma = pstdev(closes)
            r = [math.log(b.close / a.close) for a, b in zip(history[-21:-1], history[-20:])]
            # Include today's completed return for sizing at today's CLOSE (never at today's open).
            r = (r + [math.log(bar.close / history[-1].close)])[-20:]
            values.update(momentum=bar.close / history[-c.lookback].close - 1,
                          channel_high=max(x.high for x in recent),
                          channel_low=min(x.low for x in recent),
                          exit_high=max(x.high for x in history[-c.exit_lookback:]),
                          exit_low=min(x.low for x in history[-c.exit_lookback:]),
                          zscore=(bar.close - mean(closes)) / sigma if sigma > 1e-12 else 0.0,
                          volatility=pstdev(r) * math.sqrt(252))
        self.bars.append(bar)
        self.previous_time = bar.available_at
        # This small digest records feature state, not any future sequence.
        state = digest({"lookback": c.lookback, "exit_lookback": c.exit_lookback,
                        "bar": asdict(bar), "values": values}) if trace else "TRAIN_CACHE"
        return FeatureFrame(bar, tuple(values.items()), ready, state,
                            (("lookback", c.lookback), ("exit_lookback", c.exit_lookback)))

    def snapshot(self) -> dict:
        return {"candidate": asdict(self.candidate), "bars": [asdict(b) for b in self.bars],
                "previous_time": self.previous_time}

    def restore(self, state: dict) -> None:
        if Candidate(**state["candidate"]) != self.candidate:
            raise ValueError("checkpoint candidate mismatch")
        bars = [Bar(**x) for x in state["bars"]]
        if len(bars) > self.bars.maxlen or any(a.available_at >= b.available_at
                                             for a, b in zip(bars, bars[1:])):
            raise ValueError("invalid feature checkpoint")
        self.bars.clear()
        self.bars.extend(bars)
        self.previous_time = state["previous_time"]


class OnlineStrategy:
    def __init__(self, candidate: Candidate):
        self.candidate = candidate
        self.direction = 0
        self.candidate_id = candidate.id

    def update(self, features: FeatureFrame) -> Signal:
        c = self.candidate
        if features.parameters != (("lookback", c.lookback), ("exit_lookback", c.exit_lookback)):
            raise ValueError("feature/strategy parameter contract mismatch")
        d = self.direction
        evidence = [f"family={c.family}", f"candidate={self.candidate_id}",
                    f"observed_through={features.bar.available_at}"]
        if c.family == "cash":
            d = 0
        elif c.family == "buy_hold":
            d = 1
        elif not features.ready:
            d = 0
            evidence.append("WARMUP: no tradable signal")
        elif c.family == "momentum":
            m = features.value("momentum")
            d = 1 if m > c.threshold else -1 if m < -c.threshold else 0
            evidence.append(f"trailing_return={m:.10g}; threshold={c.threshold}")
        elif c.family == "breakout":
            p = features.bar.close
            if p > features.value("channel_high"):
                d = 1
            elif p < features.value("channel_low"):
                d = -1
            elif (d == 1 and p < features.value("exit_low")) or (
                    d == -1 and p > features.value("exit_high")):
                d = 0
            evidence.append("close compared to prior bars only; persistent channel state")
        else:
            z = features.value("zscore")
            if z <= -c.threshold:
                d = 1
            elif z >= c.threshold:
                d = -1
            elif (d == 1 and z >= -0.2) or (d == -1 and z <= 0.2):
                d = 0
            evidence.append(f"prior-window z={z:.10g}; entry={c.threshold}; exit=0.2")
        self.direction = d
        vol = features.value("volatility")
        risk_weight = min(c.max_weight, c.target_volatility / max(vol or 0.0, 0.01))
        if c.family == "buy_hold":
            risk_weight = c.max_weight
        return Signal(features, d, d * risk_weight, self.candidate_id, tuple(evidence))

    def snapshot(self) -> dict:
        return {"candidate": asdict(self.candidate), "direction": self.direction}

    def restore(self, state: dict) -> None:
        if Candidate(**state["candidate"]) != self.candidate or state["direction"] not in (-1, 0, 1):
            raise ValueError("invalid strategy checkpoint")
        self.direction = state["direction"]


def candidate_grid() -> tuple[Candidate, ...]:
    rows = [Candidate("momentum", n, threshold=x) for n in (20, 60, 120)
            for x in (0.0, 0.01, 0.02, 0.03)]
    rows += [Candidate("breakout", n, exit_lookback=e) for n in (20, 55, 100)
             for e in (5, 10, 15, 20)]
    rows += [Candidate("reversion", n, threshold=z, exit_lookback=min(10, n))
             for n in (10, 20, 60) for z in (1.0, 1.5, 2.0, 2.5)]
    assert len(rows) == len({x.id for x in rows}) == 36
    return tuple(rows)
