"""前置条件审查模块的状态模型、阈值和结果数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


# ── 状态枚举 ──────────────────────────────────────────────────────────────

class EligibilityStatus(str, Enum):
    """最终交易资格状态。"""

    NOT_READY = "NOT_READY"
    WATCH_ONLY = "WATCH_ONLY"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    RISK_REJECTED = "RISK_REJECTED"
    TRADE_ELIGIBLE = "TRADE_ELIGIBLE"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    SUSPENDED = "SUSPENDED"


class SignalReadiness(str, Enum):
    """数据就绪状态。"""

    DATA_READY = "data_ready"
    INSTRUMENT_VERIFIED = "instrument_verified"
    LIQUIDITY_PASSED = "liquidity_passed"
    MARKET_REGIME = "market_regime"
    VOLATILITY_PASSED = "volatility_passed"
    BREAKOUT_QUALITY = "breakout_quality"
    EVENT_RISK_LEVEL = "event_risk_level"
    PORTFOLIO_RISK_PASSED = "portfolio_risk_passed"
    BACKTEST_VALIDATED = "backtest_validated"
    EXECUTION_READY = "execution_ready"
    TRADE_ELIGIBLE = "trade_eligible"


class MarketRegime(str, Enum):
    """市场状态分类。"""

    TREND_FRIENDLY = "TREND_FRIENDLY"
    NEUTRAL = "NEUTRAL"
    RANGE_BOUND = "RANGE_BOUND"
    EXTREME_RISK = "EXTREME_RISK"
    WEAK_TREND = "WEAK_TREND"


class BreakoutQuality(str, Enum):
    """突破质量分类。"""

    RAW_BREAKOUT = "RAW_BREAKOUT"
    QUALIFIED_BREAKOUT = "QUALIFIED_BREAKOUT"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    OVEREXTENDED_BREAKOUT = "OVEREXTENDED_BREAKOUT"
    FALSE_BREAKOUT = "FALSE_BREAKOUT"


class RiskLevel(str, Enum):
    """事件风险等级。"""

    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class BlockedReason(str, Enum):
    """阻断原因枚举。"""

    # 数据类
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    DATA_TIMESTAMP_INVALID = "DATA_TIMESTAMP_INVALID"
    DATA_OHLCV_INVALID = "DATA_OHLCV_INVALID"
    BAR_NOT_CLOSED = "BAR_NOT_CLOSED"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_SOURCE_MISMATCH = "DATA_SOURCE_MISMATCH"
    CROSS_EXCHANGE_MIXING = "CROSS_EXCHANGE_MIXING"
    ADJUSTMENT_NOT_APPLIED = "ADJUSTMENT_NOT_APPLIED"
    CONTRACT_ROLL_NOT_HANDLED = "CONTRACT_ROLL_NOT_HANDLED"
    TIMEZONE_MISMATCH = "TIMEZONE_MISMATCH"
    PRICE_ANOMALY = "PRICE_ANOMALY"

    # 标的相关
    MARKET_CLOSED = "MARKET_CLOSED"
    TRADING_SUSPENDED = "TRADING_SUSPENDED"
    DIRECTION_NOT_ALLOWED = "DIRECTION_NOT_ALLOWED"

    # 流动性类
    INSUFFICIENT_VOLUME = "INSUFFICIENT_VOLUME"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    TURNOVER_TOO_LOW = "TURNOVER_TOO_LOW"
    ESTIMATED_SLIPPAGE_TOO_HIGH = "ESTIMATED_SLIPPAGE_TOO_HIGH"
    MARKET_DEPTH_INSUFFICIENT = "MARKET_DEPTH_INSUFFICIENT"
    EXCHANGE_RISK_HIGH = "EXCHANGE_RISK_HIGH"
    EARNINGS_RISK = "EARNINGS_RISK"

    # 市场状态类
    MARKET_RANGE_BOUND = "MARKET_RANGE_BOUND"
    MARKET_EXTREME_RISK = "MARKET_EXTREME_RISK"
    ADX_TOO_LOW = "ADX_TOO_LOW"
    TREND_WEAKENING = "TREND_WEAKENING"

    # 波动率类
    ATR_TOO_LOW = "ATR_TOO_LOW"
    ATR_TOO_HIGH = "ATR_TOO_HIGH"
    ATR_PERCENTILE_EXTREME = "ATR_PERCENTILE_EXTREME"
    GAP_EXCEEDED = "GAP_EXCEEDED"
    STOP_DISTANCE_TOO_LARGE = "STOP_DISTANCE_TOO_LARGE"
    CHASE_DISTANCE_TOO_LARGE = "CHASE_DISTANCE_TOO_LARGE"

    # 突破质量类
    NOT_CLOSE_BREAKOUT = "NOT_CLOSE_BREAKOUT"
    BREAKOUT_MAGNITUDE_WEAK = "BREAKOUT_MAGNITUDE_WEAK"
    VOLUME_NOT_CONFIRMING = "VOLUME_NOT_CONFIRMING"
    WEAK_CLOSE_LOCATION = "WEAK_CLOSE_LOCATION"
    LONG_WICK = "LONG_WICK"
    OVEREXTENDED = "OVEREXTENDED"
    RETEST_FAILED = "RETEST_FAILED"
    MULTI_TIMEFRAME_MISALIGNED = "MULTI_TIMEFRAME_MISALIGNED"

    # 风险预算类
    SINGLE_TRADE_RISK_EXCEEDED = "SINGLE_TRADE_RISK_EXCEEDED"
    SYMBOL_RISK_EXCEEDED = "SYMBOL_RISK_EXCEEDED"
    ASSET_CLASS_RISK_EXCEEDED = "ASSET_CLASS_RISK_EXCEEDED"
    PORTFOLIO_RISK_EXCEEDED = "PORTFOLIO_RISK_EXCEEDED"
    MAX_DRAWDOWN_EXCEEDED = "MAX_DRAWDOWN_EXCEEDED"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MIN_POSITION_SIZE_UNMET = "MIN_POSITION_SIZE_UNMET"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    LEVERAGE_EXCEEDED = "LEVERAGE_EXCEEDED"

    # 相关性类
    CORRELATION_OVERLAP = "CORRELATION_OVERLAP"
    RISK_GROUP_EXCEEDED = "RISK_GROUP_EXCEEDED"
    SAME_DIRECTION_CONCENTRATION = "SAME_DIRECTION_CONCENTRATION"

    # 事件风险类
    EVENT_RISK_HIGH = "EVENT_RISK_HIGH"
    FOMC_EVENT = "FOMC_EVENT"
    ECONOMIC_DATA_EVENT = "ECONOMIC_DATA_EVENT"
    EARNINGS_EVENT = "EARNINGS_EVENT"
    REGULATORY_EVENT = "REGULATORY_EVENT"
    GEOPOLITICAL_EVENT = "GEOPOLITICAL_EVENT"

    # 策略有效性
    BACKTEST_NOT_VALIDATED = "BACKTEST_NOT_VALIDATED"
    BACKTEST_PERFORMANCE_INSUFFICIENT = "BACKTEST_PERFORMANCE_INSUFFICIENT"
    OUT_OF_SAMPLE_FAILURE = "OUT_OF_SAMPLE_FAILURE"
    PARAMETER_OVERFIT = "PARAMETER_OVERFIT"

    # 执行系统
    API_NOT_CONNECTED = "API_NOT_CONNECTED"
    ACCOUNT_NOT_SYNCED = "ACCOUNT_NOT_SYNCED"
    EXECUTION_MISMATCH = "EXECUTION_MISMATCH"


# ── 阈值配置 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LiquidityThresholds:
    """流动性阈值。"""

    min_daily_volume_usd: float = 0.0
    max_spread_bps: float = 100.0
    min_turnover_ratio: float = 0.0
    max_estimated_slippage_bps: float = 50.0
    estimated_spread_bps: float = 10.0
    max_market_impact_pct: float = 0.01


@dataclass(frozen=True)
class VolatilityThresholds:
    """波动率阈值。"""

    min_atr_pct: float = 0.005
    max_atr_pct: float = 0.08
    min_atr_percentile: float = 0.10
    max_atr_percentile: float = 0.90
    max_gap_atr: float = 1.5
    max_chase_distance_atr: float = 1.0
    max_stop_distance_atr: float = 3.0


@dataclass(frozen=True)
class MarketRegimeThresholds:
    """市场状态判定阈值。"""

    trend_ma_short: int = 60
    trend_ma_long: int = 120
    adx_threshold: float = 20.0
    adx_strong_threshold: float = 30.0
    donchian_width_percentile_low: float = 0.20
    donchian_width_percentile_high: float = 0.80
    false_breakout_lookback: int = 20


@dataclass(frozen=True)
class BreakoutQualityThresholds:
    """突破质量阈值。"""

    min_close_location: float = 0.65
    max_wick_ratio: float = 0.50
    min_breakout_magnitude_atr: float = 0.10
    volume_confirmation_ratio: float = 1.0
    max_overextended_atr: float = 1.5
    retest_tolerance_atr: float = 0.25


@dataclass(frozen=True)
class RiskBudgetThresholds:
    """风险预算阈值。"""

    single_trade_risk_pct: float = 0.01
    max_symbol_risk_pct: float = 0.04
    max_asset_class_risk_pct: float = 0.12
    max_portfolio_risk_pct: float = 0.25
    max_drawdown_pct: float = 0.25
    max_consecutive_losses: int = 5
    min_position_size: float = 0.0
    risk_reduction_after_losses: float = 0.5


@dataclass(frozen=True)
class CorrelationThresholds:
    """相关性阈值。"""

    high_correlation: float = 0.70
    risk_group_max_exposure_pct: float = 0.15
    max_same_direction_signals: int = 3


@dataclass(frozen=True)
class EventRiskThresholds:
    """事件风险阈值。"""

    earnings_blackout_days: int = 3
    fomc_blackout_hours: int = 24
    economic_data_blackout_hours: int = 4
    max_funding_rate: float = 0.001
    max_open_interest_change_pct: float = 0.20


@dataclass(frozen=True)
class BacktestThresholds:
    """回测有效性阈值。"""

    min_data_years: float = 3.0
    min_trades: int = 30
    min_sharpe: float = 0.3
    max_drawdown_pct: float = 0.35
    min_calmar: float = 0.3
    max_false_breakout_rate: float = 0.40
    max_consecutive_losses: int = 8


@dataclass(frozen=True)
class EligibilityThresholds:
    """综合前置条件阈值，按资产类别分组。"""

    data: dict[str, Any] = field(default_factory=dict)
    liquidity: LiquidityThresholds = field(default_factory=LiquidityThresholds)
    volatility: VolatilityThresholds = field(default_factory=VolatilityThresholds)
    market_regime: MarketRegimeThresholds = field(
        default_factory=MarketRegimeThresholds
    )
    breakout_quality: BreakoutQualityThresholds = field(
        default_factory=BreakoutQualityThresholds
    )
    risk_budget: RiskBudgetThresholds = field(default_factory=RiskBudgetThresholds)
    correlation: CorrelationThresholds = field(
        default_factory=CorrelationThresholds
    )
    event_risk: EventRiskThresholds = field(
        default_factory=EventRiskThresholds
    )
    backtest: BacktestThresholds = field(default_factory=BacktestThresholds)


# ── 检查结果 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EligibilityResult:
    """前置条件审查完整结果。"""

    symbol: str
    timeframe: str
    status: EligibilityStatus = EligibilityStatus.NOT_READY
    trade_eligible: bool = False

    # 逐项检查结果
    data_ready: SignalReadiness = SignalReadiness.DATA_READY
    instrument_verified: SignalReadiness = SignalReadiness.INSTRUMENT_VERIFIED
    liquidity_passed: SignalReadiness = SignalReadiness.LIQUIDITY_PASSED
    market_regime: MarketRegime = MarketRegime.NEUTRAL
    volatility_passed: SignalReadiness = SignalReadiness.VOLATILITY_PASSED
    breakout_quality: BreakoutQuality = BreakoutQuality.RAW_BREAKOUT
    event_risk_level: RiskLevel = RiskLevel.NORMAL
    portfolio_risk_passed: SignalReadiness = (
        SignalReadiness.PORTFOLIO_RISK_PASSED
    )
    backtest_validated: SignalReadiness = SignalReadiness.BACKTEST_VALIDATED
    execution_ready: SignalReadiness = SignalReadiness.EXECUTION_READY

    # 阻断原因
    hard_blocks: tuple[BlockedReason, ...] = ()
    soft_warnings: tuple[BlockedReason, ...] = ()

    # 风险指标
    suggested_position_size: float = 0.0
    suggested_risk_unit: float = 0.0
    risk_group: str = ""
    regime_score: float = 0.0
    quality_score: float = 0.0
    overall_score: float = 0.0

    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status.value,
            "trade_eligible": self.trade_eligible,
            "data_ready": self.data_ready.value,
            "instrument_verified": self.instrument_verified.value,
            "liquidity_passed": self.liquidity_passed.value,
            "market_regime": self.market_regime.value,
            "volatility_passed": self.volatility_passed.value,
            "breakout_quality": self.breakout_quality.value,
            "event_risk_level": self.event_risk_level.value,
            "portfolio_risk_passed": self.portfolio_risk_passed.value,
            "backtest_validated": self.backtest_validated.value,
            "execution_ready": self.execution_ready.value,
            "hard_blocks": [b.value for b in self.hard_blocks],
            "soft_warnings": [w.value for w in self.soft_warnings],
            "suggested_position_size": self.suggested_position_size,
            "suggested_risk_unit": self.suggested_risk_unit,
            "risk_group": self.risk_group,
            "regime_score": self.regime_score,
            "quality_score": self.quality_score,
            "overall_score": self.overall_score,
            "metadata": dict(self.metadata),
        }