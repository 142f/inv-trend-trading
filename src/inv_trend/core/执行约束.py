"""可审计执行合同。默认仅用于研究，不将未认证的合约变成实盘合约。"""
from __future__ import annotations
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class ExecutionPolicy:
    event_clock: bool = True
    price_slippage: bool = True
    max_gross_leverage: float = 1.0
    financing_bps_per_year: float = 300.0
    short_borrow_bps_per_year: float = 300.0
    volume_participation: float = 0.01
    enforce_capital: bool = True
    enforce_volume: bool = True
    charge_calendar_carry: bool = True
    certified: bool = False

    def __post_init__(self):
        for key in ("max_gross_leverage", "volume_participation"):
            v = getattr(self, key)
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f"{key} 必须为有限正数")
        if self.volume_participation > 1:
            raise ValueError("成交参与率不能超过1")
        for key in ("financing_bps_per_year", "short_borrow_bps_per_year"):
            v = getattr(self, key)
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"{key} 必须为有限非负数")
        if self.certified:
            raise ValueError("当前执行合同未获市场/合约认证；不得启用实盘认证")
