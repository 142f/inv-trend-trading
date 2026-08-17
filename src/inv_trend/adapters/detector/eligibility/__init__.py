"""海龟交易前置条件审查模块。

在突破信号生成后、仓位执行前，对数据、标的、市场状态、波动率、
突破质量、风险预算、相关性、事件风险、策略有效性和执行系统
进行全面审查。
"""

from .checker import TradeEligibilityChecker
from .config import load_eligibility_config
from .models import (
    BlockedReason,
    BreakoutQuality,
    EligibilityResult,
    EligibilityVerdict,
    EligibilityStatus,
    EligibilityThresholds,
    MarketRegime,
    RiskLevel,
    SignalReadiness,
)

__all__ = [
    "BlockedReason",
    "BreakoutQuality",
    "EligibilityResult",
    "EligibilityVerdict",
    "EligibilityStatus",
    "EligibilityThresholds",
    "MarketRegime",
    "RiskLevel",
    "SignalReadiness",
    "TradeEligibilityChecker",
    "load_eligibility_config",
]
