from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from ..models.domain import LONG
from ..models.order_intent import PendingOrderIntent


@dataclass(frozen=True)
class ExpiryResult:
    status: str
    reason: str = ""


class ExpiryPolicy:
    def __init__(self, max_fill_attempts: int = 1, max_fill_gap_n: float = 2.0) -> None:
        if max_fill_attempts < 1 or max_fill_gap_n <= 0:
            raise ValueError("expiry limits must be positive")
        self.max_fill_attempts = max_fill_attempts
        self.max_fill_gap_n = max_fill_gap_n

    def check_before_fill(
        self,
        intent: PendingOrderIntent,
        previous_bar: Mapping[str, Any] | None,
        open_price: float | None,
    ) -> ExpiryResult:
        # Entry gap/channel filters must never cancel a risk-reducing exit.
        if intent.order.action == "exit":
            return ExpiryResult("pending")
        if intent.fill_attempts_completed >= self.max_fill_attempts:
            return ExpiryResult("expired", "maximum completed fill attempts")
        if previous_bar is not None and intent.order.metadata.get("validate_channel", True):
            close = _finite(previous_bar.get("close"))
            channel = _finite(previous_bar.get(f"high_{intent.entry_period}" if intent.order.side == LONG else f"low_{intent.entry_period}"))
            n = _finite(previous_bar.get("n"))
            if close is not None and channel is not None and n is not None and n > 0:
                invalidated = close < channel if intent.order.side == LONG else close > channel
                if invalidated and abs(close - channel) / n > self.max_fill_gap_n:
                    return ExpiryResult("cancelled", "previous completed bar invalidated channel")
        if open_price is not None and intent.order.n_at_signal > 0:
            if abs(open_price - intent.order.signal_price) / intent.order.n_at_signal > self.max_fill_gap_n:
                return ExpiryResult("cancelled", "open gap exceeds N limit")
        return ExpiryResult("pending")


def _finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None
